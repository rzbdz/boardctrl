"""Boot executor: runs an SDK boot recipe (load | download) against one DUT."""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from core.env import expand
from core.errors import HardwareError, HardwareTimeout, InvalidRequest
from core.jobs import current_token
from core.sdk.catalog import Instance
from core.dut.control import ControlBackend
from core.dut.artifacts import ArtifactStore

Log = Callable[[str], None]

# Hook argument names whose values never reach a boot log.
_SECRET = re.compile(r"token|secret|passw|credential|private|auth(?!or)|(^|[_-])key$|api[_-]?key", re.I)


def redact(value: Any) -> Any:
    """A hook's arguments as a boot log may show them: values under secret-like names hidden."""
    if isinstance(value, dict):
        return {k: "***" if _SECRET.search(str(k)) and v not in (None, "") else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class _BackInDownload(Exception):
    """The board showed its boot ROM again in the middle of a load: a person pressed its download button a
    second time, most likely. The boot ROM is where a load starts, so the load can be replayed from there."""


class BootRunner:
    # How many times a load is replayed after the board went back to its boot ROM; a person pressing the
    # download button twice needs one, and a third only hides a board that will not come up.
    MAX_RESTARTS = 2

    def __init__(self, dut_id: str, control: ControlBackend, fastboot: Any, console: Any,
                 store: ArtifactStore, hooks=None, download_mode: dict | None = None, media=None,
                 env: dict[str, Any] | None = None, network: dict[str, Any] | None = None,
                 product_dir: Callable[[str | None, str | None], str | None] | None = None):
        self.dut_id = dut_id
        self.control = control
        self.fb = fastboot
        self.console = console
        self.store = store
        self.hooks = hooks
        # what download (FDL) mode looks like: {console: regex, fastboot: regex}; empty = any fastboot device
        self.download_mode = download_mode or {}
        # how it boots its media when FDL is strapped (mediaboot.MediaBoot), if its SoC profile says
        self.media = media
        # Host values for hook requests: the host config's `env:` map, its derived network, and how a
        # product directory is resolved (SDK id, remote artifact directory) -> path on this host.
        self.env = dict(env or {})
        self.network = dict(network or {})
        self.product_dir = product_dir

    # ------------------------------------------------------------ waits --
    def wait_fastboot(self, product: str, timeout: float, log: Log,
                      back: "Callable[[dict[str, str] | None], str | None] | None" = None) -> dict[str, str]:
        """Wait for a fastboot device whose product matches `product`.

        `back` says when the board is in its boot ROM again (see `rom_watch`): the wait then ends at once with
        `_BackInDownload` instead of running out, so a load cut short by a second press of the download button is
        noticed in seconds."""
        rx = re.compile(product)
        deadline = time.monotonic() + timeout
        last = None
        token = current_token()
        while time.monotonic() < deadline:
            token.check()
            last = self.fb.state()
            if last and rx.search(last["product"]):
                return last
            if back is not None:
                why = back(last)
                if why:
                    raise _BackInDownload(why)
            token.sleep(0.3)
        raise HardwareTimeout(
            f"fastboot /{product}/ did not appear on {self.fb.usb} within {timeout:g}s (last: {last})",
            hint="check USB cabling and `bsp dut status`; the console tail is in details",
            details={"console_tail": "\n".join(self.console.tail(15))},
        )

    def _port(self) -> dict[str, str] | None:
        try:
            return self.fb.state()
        except Exception:  # noqa: BLE001 - no fastboot yet is just "not seen"
            return None

    def _enumeration(self) -> str | None:
        try:
            return self.fb.enumeration() if hasattr(self.fb, "enumeration") else None
        except Exception:  # noqa: BLE001 - cannot tell is not a change
            return None

    def download_evidence(self, mark, fresh: bool = False) -> "Callable[[], str | None]":
        """A check that reports how the board shows it is in download mode (console or USB).

        `fresh`: only what follows the action counts. A download device that was on the bus before it is evidence
        only once it re-enumerated (its enumeration changed, or it was seen gone and back); the console's download
        line since `mark` always counts. Without it, a board the reset never reached passes for one it put in
        download mode. Taken before the action, since it looks at the bus as it is now."""
        con_rx = re.compile(self.download_mode["console"]) if self.download_mode.get("console") else None
        fb_rx = re.compile(self.download_mode.get("fastboot") or ".")
        before = self._port() if fresh else None
        before_enum = self._enumeration() if before else None
        # Nothing else could ever count when neither the console nor the port can say: then the device as it is.
        if before and before_enum is None and con_rx is None:
            before = None
        gone = [False]

        def seen() -> str | None:
            if con_rx and con_rx.search(self.console.read_since(mark)):
                return f"download mode on the console (/{con_rx.pattern}/)"
            st = self._port()
            if before and not gone[0]:
                if st is None:
                    gone[0] = True          # it left the bus: whatever comes back is the board's answer
                    return None
                now = self._enumeration() if before_enum is not None else None
                if now is None or now == before_enum:
                    return None             # the same device as before the action: not evidence by itself
                gone[0] = True
            if st and fb_rx.search(st.get("product", "")):
                return f"download-mode device on {self.fb.usb} ({st['product']})" + (" (re-enumerated)" if before else "")
            return None
        seen.stale = lambda: bool(before) and not gone[0]   # type: ignore[attr-defined]
        return seen

    def require_download_port(self) -> dict[str, str]:
        """The board's download device as it is now, for work that does not reset it first (`--no-reset`): on the
        bus, and in the download mode its SoC declares when it declares one."""
        st = self._port()
        if not st:
            raise HardwareError(f"{self.dut_id}: no download port on {self.fb.usb}",
                                hint=f"put it in download mode first (`bsp dut reset {self.dut_id}`), or drop --no-reset")
        want = self.download_mode.get("fastboot")
        if want and not re.search(want, st.get("product", "")):
            raise HardwareError(
                f"{self.dut_id}: {self.fb.usb} has {st.get('product') or '(no product)'!r} on it "
                f"({st.get('serial') or 'no serial'}), not the download mode /{want}/",
                hint=f"the board is in another stage: put it in download mode first (`bsp dut reset {self.dut_id}`), "
                     "or drop --no-reset", details={"device": st, "download_mode": want})
        return st

    def enter_fdl(self, log: Log, timeout: float = 30) -> None:
        mark = self.console.mark()
        human = getattr(self.control, "needs_human", False)
        # A relay's pulse must show: a device already on the bus counts only once it re-enumerated. A person may
        # find the board already waiting, and the device that is there is then the answer.
        seen = self.download_evidence(mark, fresh=not human)
        started = time.monotonic()
        self.control.enter_fdl(log, seen)
        # Either sign will do: the boot ROM's banner on the console, or its download device on USB. A board that was
        # already in download mode prints no new banner, so waiting for the console alone failed on it.
        # A relay switches at once, so its board shows download mode within `timeout`. A person may confirm before
        # the board is there (or press it a moment later): the board gets the rest of the person's window, and at
        # least a minute after they confirmed.
        if human:
            confirmed = time.monotonic()
            deadline = max(started + self.control.human_timeout(), confirmed + 60)
        else:
            deadline = time.monotonic() + timeout
        token = current_token()
        while not seen():
            token.check()
            if time.monotonic() > deadline:
                if human:
                    raise HardwareTimeout(
                        f"a person confirmed {self.dut_id} is in download mode, but it did not show it within "
                        f"{deadline - confirmed:.0f}s of that: neither the boot ROM's banner on the console"
                        + (f" (/{self.download_mode['console']}/)" if self.download_mode.get("console") else "")
                        + f" nor its download device on {self.fb.usb}",
                        hint="this board is reset by a person (no relay). It may have booted normally (download "
                             "button not held), or its download cable may be out; the console tail shows which. "
                             "Ask the person to try again, then retry",
                        details={"console_tail": "\n".join(self.console.tail(15))})
                if getattr(seen, "stale", lambda: False)():
                    raise HardwareTimeout(
                        f"{self.dut_id}'s download device on {self.fb.usb} was already there before the reset and "
                        f"did not re-enumerate within {timeout:g}s, and the console showed no boot-ROM banner"
                        + (f" (/{self.download_mode['console']}/)" if self.download_mode.get("console") else "")
                        + ": the reset may not have reached the board",
                        hint="check the reset wiring (`bsp dut status <dut>` shows the reset line); if the board is "
                             "already in download mode, run with --no-reset",
                        details={"console_tail": "\n".join(self.console.tail(15)), "device": self._port()})
                raise HardwareTimeout(
                    f"no sign of download mode within {timeout:g}s: neither the boot ROM's banner on the console"
                    + (f" (/{self.download_mode['console']}/)" if self.download_mode.get("console") else "")
                    + f" nor its download device on {self.fb.usb}",
                    hint="if the host was re-cabled lately (consoles or reset lines moved), its wiring is stale: "
                         "rebuild it (`bsp probe --rebuild -r <host> --yes`); otherwise see "
                         "`bsp dut log <dut>`, the board may be in another stage",
                    details={"console_tail": "\n".join(self.console.tail(15))})
            token.sleep(0.3)
        dev = self.wait_fastboot(self.download_mode.get("fastboot") or ".", timeout, log)
        log(f"board in download mode on {dev['usb']} ({dev['product']})")

    # ------------------------------------------------------------ steps --
    def strapped(self) -> bool:
        cfg = self.control.cfg if hasattr(self.control, "cfg") else {}
        return (cfg.get("fdl") or {}).get("strategy", "strapped") == "strapped"

    def rom_watch(self, mark) -> "Callable[[dict[str, str] | None], str | None]":
        """A check for "the board is in its boot ROM again, though nothing asked it to be": its download banner
        on the console since `mark`, or its download device leaving the bus and coming back. Returns how it
        says so, or None. The console is read at most once a second (the caller reads the port itself, on
        every poll)."""
        fb_rx = re.compile(self.download_mode["fastboot"]) if self.download_mode.get("fastboot") else None
        left, last_read = False, 0.0

        def check(state: dict[str, str] | None) -> str | None:
            nonlocal left, last_read
            if time.monotonic() - last_read >= 1.0:
                last_read = time.monotonic()
                why = self._download_banner(mark)
                if why:
                    return why
            if fb_rx is not None:
                if not state:
                    left = True                     # the boot ROM's device left the bus: the load got going
                elif left and fb_rx.search(state.get("product") or ""):
                    return f"the boot ROM's download device is back on {self.fb.usb} ({state.get('product')})"
            return None

        return check

    def _download_banner(self, mark) -> str | None:
        """The boot ROM's download banner on the console since `mark` (None when the console cannot show one)."""
        rx = self.download_mode.get("console")
        if not rx:
            return None
        try:
            text = self.console.read_since(mark) or ""
        except Exception:  # noqa: BLE001 - a console with no log says nothing, which is not a restart
            return None
        return f"the console printed its boot ROM's download banner again (/{rx}/)" if re.search(rx, text) else None

    def _with_rom_fail(self, fail: str | None) -> str | None:
        """A recipe's `fail` pattern with the boot ROM's banner added: a console wait then ends when the board
        goes back to its boot ROM, instead of waiting out a stage that is gone."""
        rx = self.download_mode.get("console")
        if not rx:
            return fail
        return f"(?:{fail})|(?:{rx})" if fail else rx

    def _console_wait(self, step: dict[str, Any], mark, timeout: float, watch: bool) -> str:
        """A `wait` step's console wait; with `watch`, the boot ROM's banner ends it as `_BackInDownload` — unless
        the step's own `fail` pattern is what ended it, which is the answer the recipe asked for."""
        try:
            return self.console.wait(step["serial"], self._with_rom_fail(step.get("fail")) if watch
                                     else step.get("fail"), timeout, mark=mark,
                                     stall=step.get("stall"), nudge=step.get("nudge"))
        except HardwareError:
            if watch:
                self._raise_if_rom(step, mark)
            raise

    def _console_send(self, step: dict[str, Any], timeout: float, watch: bool) -> dict[str, Any]:
        """A `send` step's console send; with `watch`, as `_console_wait`."""
        mark = self.console.mark()
        try:
            return self.console.send(step["text"], step.get("serial"),
                                     self._with_rom_fail(step.get("fail")) if watch else step.get("fail"), timeout,
                                     stall=step.get("stall"), nudge=step.get("nudge"))
        except HardwareError:
            if watch:
                self._raise_if_rom(step, mark)
            raise

    def _raise_if_rom(self, step: dict[str, Any], mark) -> None:
        """Raise `_BackInDownload` when what ended a console wait is the boot ROM coming back; a failure the
        recipe named itself (`fail`) is left to be raised as it is."""
        rx = self.download_mode.get("console")
        if not rx:
            return
        try:
            text = self.console.read_since(mark) or ""
        except Exception:  # noqa: BLE001 - a console with no log cannot say what ended the wait
            return
        if step.get("fail") and re.search(step["fail"], text):
            return
        if re.search(rx, text):
            raise _BackInDownload(f"the console printed its boot ROM's download banner again (/{rx}/)") from None

    def run_steps(self, steps: list[dict[str, Any]], log: Log, arts: dict[str, Any] | None = None,
                  images: dict[str, Any] | None = None, what: str = "step", since=None,
                  sdk: str | None = None, artifacts_dir: str | None = None) -> None:
        """Run boot steps (an SDK recipe's, or a media boot's) in order. `arts` are pulled artifacts by role,
        `images` the SoC profile's images by key (each with a verified local `path`); `since` a console mark taken
        before what led up to the steps (a reset), which the first `wait` and `check` look from. `sdk` and
        `artifacts_dir` name the build output a hook step works on, for its request.

        A board a person puts into download mode can go back to its boot ROM mid-load: their finger slips on the
        download button a second time, while the load is already staging. The boot ROM is where a load starts, so
        for such a board the load is replayed from its first `stage` step (the board is already in download mode;
        nobody is asked again). The watch costs nothing on the way that works: it runs inside waits only, and
        only when the board is a person's to reset."""
        first_stage = next((i for i, s in enumerate(steps, 1) if s.get("do") == "stage"), None)
        watch = first_stage is not None and getattr(self.control, "needs_human", False)
        begin, restarts = 1, 0
        while True:
            try:
                self._run_steps(steps, log, arts, images, what, since, sdk, artifacts_dir,
                                begin=begin, watch=watch)
                return
            except _BackInDownload as e:
                if restarts >= self.MAX_RESTARTS:
                    raise HardwareError(
                        f"{what}: the board went back to its boot ROM {restarts + 1} times while loading "
                        f"({e}); nothing is loaded",
                        hint="something puts it there mid-load: the download button pressed or held too long, "
                             "a loose reset line, or a stage that does not start. The console shows which",
                        details={"console_tail": "\n".join(self.console.tail(20))}) from None
                restarts += 1
                log(f"{what}: {e}; the board is in its boot ROM, so the load starts over from step {first_stage}")
                dev = self.wait_fastboot(self.download_mode.get("fastboot") or ".", 30, log)
                log(f"{what}: download mode on {dev['usb']} ({dev['product']}); replaying (attempt "
                    f"{restarts + 1} of {self.MAX_RESTARTS + 1})")
                begin, since = first_stage, None

    def _run_steps(self, steps: list[dict[str, Any]], log: Log, arts, images, what, since, sdk, artifacts_dir,
                   begin: int = 1, watch: bool = False) -> None:
        arts, images = arts or {}, images or {}
        mark = self.console.mark() if since is None else since   # console output since the last action, for `wait`
        action = mark                                             # ... and since it began, for `check`
        strapped = self.strapped()
        media = self.media if self.media is not None and strapped else None
        token = current_token()
        # Where a restart is looked for from: the initial run starts it after its first step, so the banner that put
        # the board in download mode in the first place is not mistaken for one. A replay starts here, since the
        # banner it is recovering from is already behind this mark.
        watch_mark = mark if watch and begin > 1 else None
        for i, step in enumerate(steps, 1):
            if i < begin:
                continue
            kind = step["do"]
            label = f"{what} {i}/{len(steps)} {kind}"
            token.at(label)                               # where a cancelled job says it stopped
            token.check()                                 # a cancelled job ends before its next step
            if watch_mark is not None:
                # A press of the download button during the step before shows its banner now: the load is gone,
                # whatever this step was about to do to it.
                fresh = self._download_banner(watch_mark)
                if fresh:
                    raise _BackInDownload(fresh)
            # Steps that need the board to boot its own media: on a strapped board they run only when it has a
            # media boot (then `reset` boots through it); otherwise they are skipped and the log says why.
            if step.get("when") == "fdl-not-strapped" and strapped and not (media and media.available()):
                why = media.unavailable() if media is not None else "the board has no media boot"
                log(f"{label}: skipped: FDL is strapped on this board ({step.get('why', 'cold boot')}; {why})")
                continue
            if kind in ("enter-fdl", "reset", "boot-media", "continue", "stage", "oem", "flash", "hook", "send"):
                action = self.console.mark()
            if kind == "boot-media" and not strapped:
                mark = self.console.mark()
                self.control.reset(log)
            elif kind == "boot-media" or (kind == "reset" and strapped and step.get("when") == "fdl-not-strapped"):
                if media is None or not media.available():
                    raise InvalidRequest(f"{label}: this board cannot boot its media "
                                         f"({media.unavailable() if media is not None else 'no media boot'})")
                mark = self.console.mark()
                media.run(self, log)
            elif kind == "enter-fdl":
                self.enter_fdl(log, float(step.get("timeout", 30)))
                mark = self.console.mark()
            elif kind == "reset":
                mark = self.console.mark()
                self.control.reset(log)
            elif kind == "stage":
                path, name = self._file(step, arts, images, label)
                ts = time.monotonic()
                self.fb.cmd("stage", path, timeout=float(step.get("timeout", 300)))
                log(f"{label}: staged {name} in {time.monotonic() - ts:.1f}s")
            elif kind == "continue":
                mark = self.console.mark()
                try:
                    self.fb.cmd("continue", timeout=30)
                except HardwareError:
                    pass  # the responder detaches on purpose; the next wait is the real check
                log(f"{label}")
            elif kind == "oem":
                self.fb.cmd("oem", step["cmd"], timeout=60)
                log(f"{label}: oem {step['cmd']} OKAY")
            elif kind == "getvar":
                val = self.fb.getvar(step["name"])
                if step.get("match") and not re.search(step["match"], val):
                    raise InvalidRequest(f"{label}: {step['name']} = {val!r} does not match /{step['match']}/",
                                         hint=step.get("why") or "the board is not one these steps are for")
                log(f"{label}: {step['name']} = {val}")
            elif kind == "check":
                if action is None:
                    raise InvalidRequest(
                        f"{label}: this board's console keeps no log, so /{step['serial']}/ cannot be checked",
                        hint="the recipe's `check` step reads what the console printed; a console provider that "
                             "cannot read back (no log) can only run steps that do not inspect the board's output")
                text = self.console.read_since(action)
                if not re.search(step["serial"], text):
                    raise InvalidRequest(f"{label}: the console since the last action does not match /{step['serial']}/",
                                         hint=step.get("why") or "the board is not one these steps are for",
                                         details={"console_tail": "\n".join(self.console.tail(15))})
                log(f"{label}: /{step['serial']}/ seen")
            elif kind == "flash":
                path, name = self._file(step, arts, images, label)
                self.fb.cmd("flash", step["partition"], path, timeout=float(step.get("timeout", 900)))
                log(f"{label}: flashed {step['partition']} <- {name}")
            elif kind == "settle":
                log(f"{label}: {step['seconds']}s ({step.get('why', 'hardware timing')})")
                token.sleep(float(step["seconds"]))
            elif kind == "wait":
                timeout = float(step.get("timeout", 60))
                ts = time.monotonic()
                if step.get("serial"):
                    self._console_wait(step, mark, timeout, watch)
                if step.get("fastboot"):
                    self.wait_fastboot(step["fastboot"], max(5.0, timeout - (time.monotonic() - ts)), log,
                                       back=self.rom_watch(mark) if watch else None)
                log(f"{label}: {step.get('serial') or step.get('fastboot')} after {time.monotonic() - ts:.1f}s")
            elif kind == "send":
                mark = self.console.mark()
                timeout = float(step.get("timeout", 60))
                ts = time.monotonic()
                out = self._console_send(step, timeout, watch)
                log(f"{label}: sent {step['text']!r}"
                    + (f", /{step['serial']}/ seen" if step.get("serial") else "")
                    + (f" after {time.monotonic() - ts:.1f}s" if step.get("serial") else ""))
                if log is not None and out.get("output"):
                    for line in str(out["output"]).rstrip("\n").splitlines()[-10:]:
                        log(f"  {line}")
            elif kind == "hook":
                if self.hooks is None:
                    raise InvalidRequest("hook steps need a hook runner on this host")
                req = {"dut": self.dut_id, "usb": self.fb.usb, "console": self.console.alias,
                       "fastboot": self.fb.binary, "args": expand(step.get("args") or {}, self.env),
                       "artifacts": {r: a["path"] for r, a in arts.items()},
                       "images": {k: v["path"] for k, v in images.items() if v.get("path")},
                       "env": dict(self.env),
                       "network": {**self.network, "console": self.console.alias}}
                if sdk is not None:
                    req["sdk"] = sdk
                if artifacts_dir is not None:
                    req["artifacts_dir"] = artifacts_dir
                if callable(self.product_dir):
                    try:
                        product_dir = self.product_dir(sdk, artifacts_dir)
                    except InvalidRequest:
                        if sdk is not None or artifacts_dir is not None:
                            raise
                        product_dir = None          # e.g. a media boot: no SDK context for a {sdk} template
                    if product_dir is not None:
                        req["product_dir"] = product_dir
                elif self.product_dir is not None:
                    req["product_dir"] = self.product_dir
                elif artifacts_dir is not None:      # nothing configured: the artifact directory is the product
                    req["product_dir"] = artifacts_dir
                # what the hook really gets, after ${VAR} expansion (secrets hidden)
                log(f"{label}: {step.get('kind', 'flash')}/{step['name']} args {redact(req['args'])}")
                res = self.hooks.run(step.get("kind", "flash"), step["name"], req,
                                     timeout=float(step.get("timeout", 1800)), log=log,
                                     progress=getattr(log, "progress", None))
                brief = {k: v for k, v in res.data.items() if k != "flashed"}
                brief["partitions"] = [":".join(str(x) for x in (f.get("layout"), f.get("partition")) if x)
                                       if isinstance(f, dict) else str(f) for f in res.data.get("flashed", [])]
                log(f"{label}: {step.get('kind', 'flash')}/{step['name']} -> {brief}")
                # keep the console mark: the next wait can check what the bootloader printed while flashing
            else:
                raise InvalidRequest(f"unknown boot step {kind!r}")
            if watch and watch_mark is None:
                watch_mark = self.console.mark()

    @staticmethod
    def _file(step: dict[str, Any], arts: dict[str, Any], images: dict[str, Any], label: str) -> tuple[str, str]:
        """The local file a stage/flash step names: an SDK artifact (`artifact`) or a SoC profile image (`image`)."""
        if "image" in step:
            img = images.get(step["image"]) or {}
            if not img.get("path"):
                raise InvalidRequest(f"{label}: image {step['image']!r} is not on this host",
                                     hint="list it under the SoC profile's images (with sha256 and source) and run "
                                          "`bsp images sync` where the site is")
            return img["path"], f"image {step['image']}"
        a = arts[step["artifact"]]
        return a["path"], f"{step['artifact']} ({a['size']:,} B)"

    # ------------------------------------------------------- file boot --
    def run_files(self, files: list[dict[str, Any]], log: Log, reset: bool = True, first_wait: float = 8.0,
                  later_wait: float = 3.0, port_timeout: float = 20.0, stage_timeout: float = 300.0,
                  poll: float = 0.5) -> dict[str, Any]:
        """Boot from RAM with files given in order (e.g. FSBL, U-Boot/OpenSBI, kernel FIT): for each one
        `stage` + `continue`, then wait for the download port to come back before the next. The first
        wait is longer (the boot ROM hands over to FSBL, which re-enumerates). Never flashes.

        On a board a person resets, a second press of its download button sends it back to the boot ROM
        mid-load: the load then starts over from the first file, as `run_steps` does for a recipe."""
        t0 = time.monotonic()
        log(f"boot {self.dut_id} from {len(files)} file(s)" + ("" if reset else " (board already in download mode)"))
        for i, f in enumerate(files, 1):
            log(f"file {i}/{len(files)}: {f['name']} ({f['size']:,} B, sha256 {f['sha256']})")
        if reset:
            self.enter_fdl(log)
        else:
            self.require_download_port()
        watch = getattr(self.control, "needs_human", False)
        restarts = 0
        while True:
            beginning = self.console.mark() if watch else None
            try:
                self._load_files(files, log, first_wait=first_wait, later_wait=later_wait,
                                 port_timeout=port_timeout, stage_timeout=stage_timeout, poll=poll,
                                 watch=beginning)
                break
            except _BackInDownload as e:
                if restarts >= self.MAX_RESTARTS:
                    raise HardwareError(
                        f"boot: the board went back to its boot ROM {restarts + 1} times while loading ({e}); "
                        "nothing is loaded",
                        hint="something puts it there mid-load: the download button pressed or held too long, "
                             "a loose reset line, or a file that does not start. The console shows which",
                        details={"console_tail": "\n".join(self.console.tail(20))}) from None
                restarts += 1
                log(f"boot: {e}; the board is in its boot ROM, so the load starts over from file 1")
                dev = self.wait_fastboot(self.download_mode.get("fastboot") or ".", 30, log)
                log(f"boot: download mode on {dev['usb']} ({dev['product']}); replaying (attempt "
                    f"{restarts + 1} of {self.MAX_RESTARTS + 1})")
        log(f"done in {time.monotonic() - t0:.1f}s")
        return {"dut": self.dut_id, "files": [{k: f[k] for k in ("name", "sha256", "size")} for f in files],
                "elapsed_s": round(time.monotonic() - t0, 1), "restarts": restarts}

    def _load_files(self, files: list[dict[str, Any]], log: Log, first_wait: float,
                    later_wait: float, port_timeout: float, stage_timeout: float, poll: float,
                    watch=None) -> None:
        """The load `run_files` runs, once: `watch` is the console mark a restart is looked for since (None: the
        board is a relay's, and a board back in its boot ROM is reported, not replayed)."""
        token = current_token()
        for i, f in enumerate(files, 1):
            label = f"file {i}/{len(files)} {f['name']}"
            token.at(label)
            token.check()
            if i > 1:
                settle = first_wait if i == 2 else later_wait
                token.sleep(settle)
                deadline = time.monotonic() + port_timeout
                while not self.fb.state():
                    fresh = self._download_banner(watch) if watch is not None else None
                    if fresh:
                        raise _BackInDownload(fresh)
                    if time.monotonic() > deadline:
                        raise HardwareTimeout(
                            f"download port {self.fb.usb} did not come back within {settle + port_timeout:g}s "
                            f"after file {i - 1}; it may not have started",
                            hint=f"see `bsp dut log {self.dut_id}`", details={"console_tail": "\n".join(self.console.tail(15))})
                    token.sleep(poll)
            # The port can be there and be the boot ROM again (a second press of the download button): the console
            # says so, and staging the next file onto a load that is gone would only waste the file.
            fresh = self._download_banner(watch) if watch is not None else None
            if fresh:
                raise _BackInDownload(fresh)
            ts = time.monotonic()
            self.fb.cmd("stage", f["path"], timeout=stage_timeout)
            try:
                self.fb.cmd("continue", timeout=30)
            except HardwareError:
                pass  # the responder detaches on purpose; the next wait is the real check
            log(f"{label}: staged and continued in {time.monotonic() - ts:.1f}s")

    # ------------------------------------------------------------- run --
    def run(self, sdk: Instance, method: str, variant: str | None, log: Log, brk: str | None = None) -> dict[str, Any]:
        plan = sdk.boot_plan(method, variant, brk)
        roles = sorted({s["artifact"] for s in plan["steps"] if "artifact" in s}
                       | {r for s in plan["steps"] for r in s.get("artifacts", [])})
        log(f"boot {self.dut_id} with {sdk.id} [{method}{'/' + variant if variant else ''}]: {len(plan['steps'])} steps"
            + (f", stopping at {brk}" if brk else ""))
        # The SDK's build jobs live in its tree (`.bsp/jobs`): each artifact then says which build wrote it.
        arts = self.store.pull(sdk.server.ssh, {r: sdk.artifacts()[r] for r in roles}, log,
                               jobs=f"{sdk.path.rstrip('/')}/.bsp/jobs" if getattr(sdk, "path", None) else None)
        t0 = time.monotonic()
        # The board's SoC images too, so `stage image:` and hook steps (a flasher shipped as an image) find theirs.
        images = (self.media.profile.get("images") or {}) if self.media is not None else {}
        artifacts_dir = sdk.artifact_dir() if hasattr(sdk, "artifact_dir") else None
        self.run_steps(plan["steps"], log, arts=arts, images=images, sdk=sdk.id, artifacts_dir=artifacts_dir)
        log(f"done in {time.monotonic() - t0:.1f}s")
        return {"dut": self.dut_id, "sdk": sdk.id, "method": method, "variant": variant,
                "artifacts": {r: {k: a.get(k) for k in ("remote", "sha256", "size", "age_s", "mtime", "built", "source",
                                                        "build")} for r, a in arts.items()},
                "elapsed_s": round(time.monotonic() - t0, 1)}
