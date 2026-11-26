"""dut module, the DUT manager of a board host: the DUTs attached to it — registry, leases,
reset/FDL, fastboot boot (load / download), serial console, and auto-discovery (probe).

Each DUT (`Board`) is the host's roles bound to one board: a relay, a download port, a console and a debug path,
each filled by the provider its record names — from the modules this host carries or a site's. The DUT module owns
what is not a provider: the records, leases, slots, the boot recipes and the probe. It publishes the
`dut_attention` service (a person's requests) and builds its boards when the host starts, after every module's
setup, so a board may bind a provider any module registered.
Commands (`commands.py`) are the atomic layer under `boardctrl dut ...` / `boardctrl probe`."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from typing import Any

from core.commands import Command
from core.env import expand
from core.errors import Conflict, HardwareError, InvalidRequest, NotFound, BspError
from core.jobs import current_token
from core.hooks import HookRunner
from core.context import HostContext
from core.net import local_addresses
from core.sdk.catalog import SdkCatalog, signature
from core.dut.boot import BootRunner
from core.dut.records import DutRegistry, DutSpec
from core.dut.commands import build_commands
from core.dut.artifacts import ArtifactStore
from core.dut.mediaboot import MediaBoot
from core.dut.leases import LeaseManager

class Board:
    """Runtime handles for one DUT (built from its spec): each role filled by the provider its record names."""

    def __init__(self, spec: DutSpec, mod: "DutManager"):
        from core.providers import build
        from core.dut import services as bindings

        self.spec = spec
        self.sessions = mod.sessions
        self._ctx = mod.ctx
        self._services = mod.ctx.service
        self.bindings = bindings.bindings(spec)
        self._flash: Any = None
        from core.dut.defaults import NoDebug, NoneConsole, NoneDownload, NoRelay

        # The role whose provider this host does not carry: the board keeps the roles it does have, and says which
        # one is missing where it is used. A USB-only host has no relay at all; a record may still name one.
        def role(what: str, make, fallback):
            try:
                return make()
            except BspError as e:
                log.warning("%s: %s provider %s is not on this host (%s); the rest of the board still works",
                            spec.id, what, (self.bindings.get(what) or {}).get("provider"), e.message)
                return fallback()

        # The relay role: the record may name a provider (a built-in backend or a site plugin's), and an older
        # record states the same thing as `control.backend`, which `bindings()` reads through.
        relay = self.bindings.get("relay") or {}
        explicit_relay = (spec.raw.get("services") or {}).get("relay")
        # A record that names the role gives the provider its own block; one written the old way is configured by
        # `control:` (the provider's settings lived there before the role had a name).
        self.control = role("relay", lambda: build("relay", relay.get("provider", "gpio-relay"), mod.ctx, spec,
                                                   mod.ctx.service),
                            lambda: NoRelay(spec, mod.ctx.service, relay))
        if explicit_relay is not None and hasattr(self.control, "cfg"):
            self.control.cfg = {**relay}
        download = self.bindings.get("download") or {}
        self.fastboot = role("download", lambda: build("download", download.get("provider", "fastboot"), mod.ctx,
                                                       spec, mod.ctx.service),
                             lambda: NoneDownload(spec, download))
        console = self.bindings.get("console") or {}
        self.console = role("console", lambda: build("console", console.get("provider", "serial-mux"), mod.ctx,
                                                      spec, mod.ctx.service),
                            lambda: NoneConsole(spec, {"provider": "none"}))
        debug = self.bindings.get("debug") or {}
        self.debug = (NoDebug(spec, debug, mod.ctx.service) if debug.get("provider", "none") == "none"
                      else role("debug", lambda: build("debug", debug.get("provider"), mod.ctx, spec, mod.ctx.service),
                                lambda: NoDebug(spec, debug, mod.ctx.service)))

    @property
    def console_alias(self) -> str | None:
        return getattr(self.console, "alias", None)

    @property
    def flash_provider(self):
        """The flash role's provider: the kernel's own `hook` one (the host's flash job) unless the record names another."""
        if self._flash is None:
            from core.providers import build

            block = self.bindings.get("flash") or {}
            self._flash = build("flash", block.get("provider", "hook"), self._ctx, self.spec, self._services)
        return self._flash

    def runner(self, mod: "DutManager") -> BootRunner:
        from core.dut import services as bindings

        relay = bindings.relay_settings(self.spec)
        expect = (relay.get("fdl") or {}).get("expect") or mod.socs.download_mode(self.spec.soc)
        return BootRunner(self.spec.id, self.control, self.fastboot, self.console, mod.store, mod.hooks, expect,
                          media=self.media(mod), env=mod.ctx.env, network=mod.network(),
                          product_dir=mod.product_dir)

    def media(self, mod: "DutManager") -> MediaBoot:
        """Booting its media although its reset lands in download mode (see mediaboot.py)."""
        return MediaBoot(self.spec, mod.socs.get(self.spec.soc))


log = logging.getLogger(__name__)


class _NoConsoles:
    """A host with no serial console provider: its slots and probes find nothing and nothing is started."""

    run_dir = Path("/nonexistent/serial")

    def info(self, alias: str):
        return None

    def running(self, alias: str) -> bool:
        return False

    def socket(self, alias: str) -> Path:
        return self.run_dir / "sock" / f"{alias}.sock"

    def up(self, alias: str) -> bool:
        return False

    def ensure(self, alias: str, device: str | None = None, only_if_known: bool = False) -> str:
        return "unknown"

    def stop(self, alias: str) -> None:
        pass

    def rename(self, old: str, new: str, device: str | None) -> None:
        pass


class _NoSystemd:
    """Sessions cannot outlive a host without systemd: refuse rather than run a server in the service's cgroup."""

    def start(self, unit, command, env=None):
        raise HardwareError("this host has no session service (systemd-run)", hint="sessions need systemd on the host")

    active = lambda self, unit: False       # noqa: E731
    reset = lambda self, unit: None         # noqa: E731
    stop = lambda self, unit: None          # noqa: E731
    pid = lambda self, unit: None           # noqa: E731


TITLE = "DUTs"


class DutManager:
    """The host's DUT manager. Built by `setup`; its boards are built in `startup`."""

    name = "dut"
    title = TITLE

    def __init__(self, ctx: HostContext, config: dict[str, Any]):
        self.ctx = ctx
        self.config = config
        from core.dut.defaults import register as register_defaults

        register_defaults()      # the roles' `none` and `hook`: there whatever a host carries
        from core.site import layered

        base = ctx.config_dir
        site = (base / config.get("site", "..")).resolve()    # <site>/hosts/<id>.yaml -> <site>
        state = ctx.module_state_dir("dut")
        self.probe_cfg = config.get("probe") or {}
        # `dut flash`: packages the host takes (uploaded or downloaded), and where it unpacks them
        self.flash_cfg = config.get("flash") or {}
        # How hooks name the build output: a literal path, or a template with {sdk}/{artifacts_dir};
        # absent = the artifact directory on the build server is the product.
        self.product_dir_tpl = config.get("product_dir")
        # `network`: what hooks tell a board to use (defaults from server.bind/announce, see resolve_network)
        self.network_cfg = config.get("network") or {}
        self._network: dict[str, Any] | None = None
        # The site's catalog, under the registry's copy when this host follows one (see server/catalog_follow.py).
        self._sdk_roots = layered("sdk", site)
        self._catalog_layer = ctx.state_dir / "catalog"
        # SoC profiles and hooks: the shipped profiles, the site, then the registry's shared files above
        # them (catalog_follow.py)
        self._site = site
        self._files_layer = ctx.state_dir / "catalog-files"
        self._catalog_error: str | None = None
        self.sdks = self._load_catalog()
        ctx.bus.on("catalog.updated", lambda ev: self.reload_catalog())
        self._hook_dirs = tuple(str((base / d).resolve()) for d in config.get("hook_dirs", []))
        self._load_socs()
        self.registry = DutRegistry(ctx.host_id, site / "duts",
                                    state / "duts.json")
        self.leases = LeaseManager(state / "leases.json")
        from core.dut.cachepolicy import CachePolicy

        # The cache's size and age policy (`cache:` here); applied now, and after every file that arrives.
        self.store = ArtifactStore(state / "artifacts")
        self.store.policy = CachePolicy.from_config(self.store.root, config.get("cache"))
        self._tidy_at_start()
        self._flash_work = state / "flash"
        from core.sessions import Sessions

        self.sessions = ctx.services.get("sessions") or Sessions(state / "sessions", _NoSystemd())
        # A host's serial consoles are the `smtty` provider's service: the slots, the probe and the logins use it
        # when the host carries one. A host without it (a machine that drives a board over USB only) has none.
        # It is read through `smtty_service` (below) rather than captured here: a host builds its DUT manager before
        # the providers' setups publish their services, so reading it now would find none.
        from core.dut.slots import SlotTable

        self.slots = SlotTable(state / "slots.json", self._list_consoles,
                               lambda alias, device: self.consoles.ensure(alias, device), config.get("slots"))
        self.boards: dict[str, Board] = {}
        self._busy: dict[str, str] = {}
        self.port_watcher = None
        from core.dut.attention import Attention
        from core.dut.sshlogin import ConsoleLogins

        self.attention = Attention(lambda topic, **d: self._event(topic, **d))
        ctx.services["dut_attention"] = self.attention

        self.logins = ConsoleLogins(config.get("ssh_console"), ctx.host_id, ctx.port, self.smtty)

    # ------------------------------------------------------- serial tooling --
    @property
    def smtty_service(self) -> Any:
        """The `smtty` provider's service, when the host carries one (read when used: its setup runs after this
        manager is built)."""
        return self.ctx.services.get("smtty")

    @property
    def smtty(self) -> str:
        """The smtty client the console logins run (`smtty` from the host config, or the service's)."""
        service = self.smtty_service
        return service.smtty if service else (self.config.get("smtty") or "smtty")

    @property
    def serial_log_dir(self) -> str:
        service = self.smtty_service
        if service:
            return service.log_dir
        return os.path.expanduser(self.config.get("serial_log_dir") or "~/.serial-mux/logs")

    @property
    def consoles(self) -> Any:
        service = self.smtty_service
        return service.consoles if service else _NoConsoles()

    @property
    def fastboot_bin(self) -> str:
        """The fastboot client the identify hook runs for a board in download mode."""
        return self.config.get("fastboot") or "fastboot"

    def _rebuild(self) -> None:
        self.boards = {i: Board(s, self) for i, s in sorted(self.registry.all().items())}
        self._sync_logins()

    def _sync_logins(self) -> None:
        """Keep `ssh <prefix><dut>@host` console logins in step with the registry."""
        if not self.logins.enabled and not self.logins.last and not self._had_logins():
            return
        try:
            res = self.logins.sync({i: b.console.alias for i, b in self.boards.items()})
            if res["added"] or res["removed"] or res["renamed"]:
                self._event("logins", **{k: res[k] for k in ("added", "removed", "renamed")})
        except Exception as e:  # noqa: BLE001 - logins are a convenience; never block the registry
            self.logins.last = {"error": str(e)}
            log.warning("console logins: %s", e)

    def _load_socs(self) -> None:
        from core.site import layered
        from core.dut.soc import SocFacts

        files = [self._files_layer / k for k in ("socs", "hooks")]
        self.socs = SocFacts(layered("socs", self._site) + ([files[0]] if files[0].is_dir() else []))
        hooks = layered("hooks", self._site) + ([files[1]] if files[1].is_dir() else [])
        self.hooks = HookRunner(self._hook_dirs + tuple(str(d) for d in reversed(hooks)), env=self.ctx.env)

    def _catalog_roots(self) -> list[Path]:
        return self._sdk_roots + ([self._catalog_layer] if self._catalog_layer.is_dir() else [])

    def _disk_signature(self) -> tuple:
        """The SDK catalog's files and the SoC profiles as they are on disk (see current_catalog)."""
        from core.site import layered

        socs = layered("socs", self._site) + ([self._files_layer / "socs"] if (self._files_layer / "socs").is_dir() else [])
        out = []
        for d in socs:
            for p in sorted(d.glob("*/soc.yaml")):
                try:
                    st = p.stat()
                except OSError:
                    continue
                out.append((str(p), st.st_size, st.st_mtime_ns))
        return signature(self._catalog_roots()) + tuple(out)

    def _load_catalog(self) -> SdkCatalog:
        roots = self._catalog_roots()
        sig = self._disk_signature()
        cat = SdkCatalog(roots)
        from core.hostregistry.catalog import layer_rev

        self._catalog_sig, self.loaded_rev = sig, layer_rev(self._catalog_layer)
        return cat

    def current_catalog(self) -> SdkCatalog:
        """The SDK catalog as its files are now: a site file edited on this host (or a registry copy written behind
        the event) takes effect at the next boot without a restart. A boot already running keeps the recipe it
        started with; a catalog that no longer loads leaves the one in use, and says why."""
        if self._disk_signature() != getattr(self, "_catalog_sig", None):
            log.info("catalog: files changed on disk, reloading")
            self.reload_catalog()
        return self.sdks

    def reload_catalog(self) -> None:
        """Take the registry's new catalog; a catalog that does not load leaves the old one in place."""
        try:
            self.sdks = self._load_catalog()
            self._load_socs()
            self._catalog_error = None
            self._event("catalog", sdks=len(self.sdks.instances.names()), socs=sorted(self.socs.profiles))
        except BspError as e:
            # remember what is on disk, so a broken file is reported once, not reloaded before every use
            self._catalog_sig = self._disk_signature()
            self._catalog_error = e.message
            self._event("error", error=e.to_dict(), what="catalog reload")
            log.warning("catalog reload failed, keeping the previous one: %s", e.message)

    def reload_site(self) -> dict[str, Any]:
        """Read the site again after it changed on disk (a tool that manages it shipped a new copy): its SDK catalog,
        SoC profiles, hooks and the DUT records it declares; the sdk module follows (`site.updated`). A part that does
        not load refuses the whole site — an error like a bad catalog, the running one kept — so a host never takes
        half a site.

        Refused while a board is busy: the boards are rebuilt on the new records, and the job running on one is using
        the SoC facts and hooks this would replace under it. A lease is not in the way (a lock lives outside the
        host's memory)."""
        self._free_to_reload()
        from core.site import layered

        roots = layered("sdk", self._site)
        sdks = SdkCatalog(roots + ([self._catalog_layer] if self._catalog_layer.is_dir() else []))
        # The site's duts/ was swapped with the rest: read it before anything is replaced, so a record that does not
        # load leaves this host exactly as it was.
        registry = DutRegistry(self.ctx.host_id, self._site / "duts", self.registry.state_file)
        self._load_socs()
        self._sdk_roots, self.sdks = roots, sdks
        self._catalog_sig, self._catalog_error = self._disk_signature(), None
        self.loaded_rev = self.catalog_rev()
        self.registry = registry
        self._rebuild()
        self.ctx.bus.publish("site.updated", site=str(self._site))
        result = {"site": str(self._site), "sdks": sorted(sdks.instances.names()), "socs": sorted(self.socs.profiles),
                  "duts": sorted(self.boards)}
        self._event("catalog", sdks=len(result["sdks"]), socs=result["socs"])
        return result

    def catalog_rev(self) -> int:
        from core.hostregistry.catalog import layer_rev

        return layer_rev(self._catalog_layer)

    def network(self) -> dict[str, Any]:
        """What hooks tell a board to use: the board subnet and this host's addresses (see resolve_network)."""
        if self._network is None:
            self._network = resolve_network(self.network_cfg, bind=self.ctx.bind, announce=self.ctx.announce)
        return dict(self._network)

    def product_dir(self, sdk: str | None = None, artifacts_dir: str | None = None) -> str | None:
        """Where hooks on this host find a build's product: `core.dut.product_dir` if set (a literal
        path or a template with {sdk}/{artifacts_dir}), else the artifacts directory itself."""
        tpl = self.product_dir_tpl
        if tpl is None or tpl == "":
            return artifacts_dir
        template = str(tpl)
        values = {"sdk": sdk, "artifacts_dir": artifacts_dir}
        missing = [k for k in ("sdk", "artifacts_dir")
                   if ("{" + k + "}") in template and not values[k]]
        if missing:
            raise InvalidRequest(
                f"core.dut.product_dir uses {{{missing[0]}}}, but it is not known here",
                hint="the template may use {sdk} and {artifacts_dir} for a boot recipe, which knows both; "
                     "a package flash always passes its unpacked package as product_dir",
            )
        try:
            out = template.format(**{k: v or "" for k, v in values.items()})
        except (KeyError, IndexError, ValueError) as e:
            raise InvalidRequest(f"core.dut.product_dir: cannot format {template!r}: {e}",
                                 hint="the placeholders are {sdk} and {artifacts_dir}") from None
        return os.path.expanduser(out)

    def login_list(self) -> dict[str, Any]:
        rows = [{"login": u, "dut": d} for u, d in sorted(self.logins._ours().items())]
        return {"enabled": self.logins.enabled, "logins": rows, "error": self.logins.last.get("error")}

    def _had_logins(self) -> bool:
        try:
            return bool(self.logins._ours())
        except Exception:  # noqa: BLE001
            return False

    # -------------------------------------------------------------- slots --
    def _list_consoles(self) -> list[dict[str, Any]]:
        from pathlib import Path

        res = self.hooks.run("probe", "consoles", {"serial_mux_run": str(Path(self.serial_log_dir).parent / "run"),
                                                   "env": dict(self.ctx.env), "network": self.network()}, timeout=30)
        return res.data.get("consoles", [])

    def refresh_slots(self) -> dict[str, Any]:
        """Match the host's consoles to slots now (new adapters get a slot and a console of their own)."""
        try:
            change = self.slots.refresh(lambda alias: next((i for i, b in self.boards.items() if b.spec.console_alias == alias), None))
        except BspError as e:
            self._event("error", error=e.to_dict(), what="slot refresh")
            return {"error": e.message}
        for slot in change.get("gone", []):
            if slot.get("alias"):
                self.consoles.stop(slot["alias"])
        if change["added"] or change["failed"] or change.get("gone"):
            self._event("slots", **change)
        return change

    def dut_in(self, slot: dict[str, Any]) -> str | None:
        """The DUT registered on a slot's console."""
        return next((i for i, b in self.boards.items() if slot.get("alias") and b.spec.console_alias == slot["alias"]), None)

    def slot_of(self, dut: str) -> str | None:
        b = self.boards.get(dut)
        slot = self.slots.by_alias(b.spec.console_alias) if b else None
        return slot["id"] if slot else None

    def resolve(self, ref: str) -> str:
        """A DUT by its name, or by its slot (`s3`, `<host>/s3`)."""
        from core.dut.slots import is_slot_ref

        if ref in self.boards or not is_slot_ref(ref):
            return ref
        host, _, sid = ref.rpartition("/")
        if host and host != self.ctx.host_id:
            raise InvalidRequest(f"slot {ref} is on {host}, not on {self.ctx.host_id}")
        dut = self.dut_in(self.slots.get(sid))
        if not dut:
            raise InvalidRequest(f"slot {sid} on {self.ctx.host_id} has no board registered",
                                 hint=f"probe it (`boardctrl probe -r {self.ctx.host_id} --slots {sid}`), or open its console (`boardctrl slot console {self.ctx.host_id}/{sid}`)")
        return dut

    def slot_list(self) -> list[dict[str, Any]]:
        return [self.slot_status(s["id"]) for s in self.slots.all()]

    def slot_status(self, slot: str) -> dict[str, Any]:
        s = self.slots.view(self.slots.get(slot))
        dut = self.dut_in(s)
        return {**s, "host": self.ctx.host_id, "ref": f"{self.ctx.host_id}/{s['id']}", "dut": dut,
                "lease": self.leases.view(dut) if dut else None,
                "console_socket": str(self.consoles.socket(s["alias"])) if s.get("alias") else None,
                "console_up": bool(s.get("alias")) and self.consoles.up(s["alias"]),
                "console_attach": f"{self.smtty} {s['alias']}" if s.get("alias") else None}

    def slot_forget(self, slot: str) -> dict[str, Any]:
        out = self.slots.forget(slot, lambda alias: next((i for i, b in self.boards.items() if b.spec.console_alias == alias), None))
        if out.get("alias"):
            self.consoles.stop(out["alias"])
        self._event("slots", forgotten=out["forgotten"])
        return out

    # ------------------------------------------------------------ helpers --
    def board(self, dut: str) -> Board:
        if dut in self.boards:
            return self.boards[dut]
        import difflib

        close = difflib.get_close_matches(dut, list(self.boards), n=3, cutoff=0.4)
        from core.errors import NotFound

        raise NotFound(f"no DUT {dut!r} on host {self.ctx.host_id}",
                       hint=(f"did you mean: {', '.join(close)}? " if close else "")
                       + f"DUTs here: {', '.join(self.boards) or '(none — run `boardctrl probe`)'}")

    def _event(self, topic: str, **data: Any) -> None:
        self.ctx.bus.publish(f"dut.{topic}", **data)

    def _guard_busy(self, dut: str) -> None:
        if dut in self._busy:
            raise Conflict(f"{dut} is busy: {self._busy[dut]}", hint="wait for the running job (`boardctrl job list`)")

    def _free_to_reload(self) -> None:
        """Refuse a site reload while this host is doing something the reload would pull out from under it."""
        busy = sorted(f"{d}: {what}" for d, what in self._busy.items())
        running = [j for j in self.ctx.jobs.list() if j.status == "running"]
        if not busy and not running:
            return
        said = busy + [f"job {j.id}: {j.title}" for j in running]
        raise Conflict(f"{self.ctx.host_id} is busy ({'; '.join(said)}); the site was not reloaded",
                       hint="a reload rebuilds the boards on the site's new records and replaces the SoC facts and "
                            "hooks a running job uses: wait for it (`boardctrl job list`), then reload")

    def _start_job(self, dut: str, what: str, body, **meta):
        self._busy[dut] = what

        def wrapped(log):
            try:
                return body(log)
            finally:
                self._busy.pop(dut, None)

        # Cancelling a board's job needs the board's lease, as everything else that changes what the board does.
        return self.ctx.jobs.start(meta.pop("kind", "dut"), f"{dut}: {what}", wrapped,
                                   guard=lambda lease: self.leases.check(dut, lease), dut=dut, **meta)

    # ----------------------------------------------------------- commands --
    def health(self) -> dict[str, Any]:
        """The module's own status for `/api/v1/status`; `status(dut)` is a board's (`dut status`)."""
        return {"duts": sorted(self.boards), "hooks": {k: self.hooks.names(k) for k in ("probe", "flash")},
                "cache": self.store.policy.usage()}

    def _tidy_at_start(self) -> None:
        """Apply the cache policy once at start, in the background (a large cache takes a moment to walk)."""
        import threading

        def run() -> None:
            try:
                self.store.tidy(lambda line: log.info("%s", line))
            except Exception:                 # a cache that cannot be tidied now is tried after the next file
                log.exception("artifact cache: tidying at start failed")

        threading.Thread(target=run, name="artifact-cache-tidy", daemon=True).start()

    def status(self, dut: str) -> dict[str, Any]:
        b = self.board(dut)
        try:
            fb = b.fastboot.state()
        except BspError as e:
            fb = {"error": e.message}
        try:
            ctl = b.control.describe()
        except BspError as e:
            ctl = {"error": e.message}
        try:
            tail = b.console.tail(4)
        except (OSError, BspError):
            tail = []
        return {**b.spec.summary(), "sdks": self.effective_sdks(b.spec), "sdks_explicit": bool(b.spec.sdks),
                "origin": self.registry.origin(dut), "lease": self.leases.view(dut),
                "busy": self._busy.get(dut), "fastboot": fb,
                "stage": ("needs-human" if self.attention.view(dut)
                          else _stage(fb, self.socs.download_mode(b.spec.soc).get("fastboot"))),
                "needs_human": self.attention.view(dut),
                "control_state": ctl, "console_tail": tail,
            # Preserve which actor performs each role when a human coordinates an FDL button and a relay pulses reset.
            "control": {"backend": b.spec.control.get("backend"), "reset": dict(b.control.cfg.get("reset") or {}),
                        "fdl": dict(b.control.cfg.get("fdl") or {})},
                # interactive: run this on the host; and where `boardctrl dut pipe` connects
                "console_attach": b.console.attach() or b.console.describe().get("attach"),
                "console_socket": b.console.socket(),
                "console_up": b.console.up(), "slot": self.slot_of(dut),
                # how the console is served: a serial-mux alias speaks that protocol, another provider (a trace
                # channel over a debug adapter) serves plain bytes on the socket it reports
                "console_provider": b.console.provider_name,
                # what fills each role of this board (provider names only; the page and the CLI read them)
                "services": {role: (b.bindings.get(role) or {}).get("provider") for role in
                             ("relay", "console", "download", "flash", "debug")}
                | ({"debug": b.debug.shelf()} if b.debug.kinds else {}),
                # its slot's adapter is plugged in (False: unplugged or moved, the host may need a rebuild)
                "slot_online": self._slot_online(b),
                # how a plain reset boots its media: `reset` (FDL not strapped), the SoC's media boot, or not at all
                "media_boot": self._media_state(b)}

    def _slot_online(self, b: Board) -> bool | None:
        if not b.console_alias:
            return None
        slot = self.slots.by_alias(b.console_alias)
        return self.slots.view(slot)["online"] if slot else None

    def _media_state(self, b: Board) -> dict[str, Any]:
        if (b.control.cfg.get("fdl") or {}).get("strategy", "strapped") != "strapped":
            return {"method": "reset", "available": True}
        m = b.media(self)
        why = m.unavailable()
        return {"method": m.name, "available": why is None, **({"why": why} if why else {})}

    def list(self) -> list[dict[str, Any]]:
        return [self.status(d) for d in self.boards]

    def lease_live(self, token: str) -> bool:
        """Whether a lease token is a live one: a session started under it is still the holder's (see Sessions)."""
        return self.leases.is_live_token(token)

    def acquire(self, dut: str, holder: str, ttl: int, purpose: str, lease: str | None) -> dict[str, Any]:
        self.board(dut)
        out = self.leases.acquire(dut, holder, ttl, purpose, lease)
        self._event("leased", dut=dut, holder=holder, purpose=purpose)
        return {"dut": dut, "host": self.ctx.host_id,
                **{k: out[k] for k in ("token", "holder", "purpose", "expires_in_s")}}

    def release(self, dut: str, lease: str | None, force: bool) -> dict[str, Any]:
        self.board(dut)
        out = self.leases.release(dut, lease, force)
        # The lease is the lifetime root of the sessions run for this board: letting go of the board lets go of
        # them too, so nobody else inherits a debug server pointed at someone else's breakpoint.
        stopped = self.sessions.reap(dut) if out.get("released") else []
        self._event("released", dut=dut, forced=force, sessions=stopped or None)
        return {**out, **({"sessions_stopped": stopped} if stopped else {})}

    def reset(self, dut: str, lease: str | None, fdl: bool) -> dict[str, Any]:
        b = self.board(dut)
        self.leases.check(dut, lease)
        self._guard_busy(dut)

        # A plain reset of a board whose reset lands in download mode boots its media when the SoC has a way to.
        media = None if fdl else b.media(self)
        media = media if media and media.available() else None

        if not media:              # a media boot may find the board waiting and reset nothing
            b.control.preflight()  # a line that cannot be driven fails now, not as a job's EBUSY

        def body(log) -> dict[str, Any]:
            booted = None
            if fdl:
                b.runner(self).enter_fdl(log)
            elif media:
                booted = media.run(b.runner(self), log)
            else:
                mark = b.console.mark()
                b.control.reset(log, self._reset_evidence(b, mark))
            self._event("reset", dut=dut, fdl=fdl, media_boot=bool(booted))
            return {"dut": dut, "fdl": fdl, "fastboot": b.fastboot.state(), **({"media_boot": booted} if booted else {})}

        if b.control.needs_human or media:   # may wait minutes for a person or a boot: run as a job
            what = "reset (waiting for a person)" if b.control.needs_human else f"boot from media ({media.name})"
            job = self._start_job(dut, what, body, kind="reset")
            return {"dut": dut, "fdl": fdl, "job": job.id, "needs_human": b.control.needs_human,
                    "media_boot": media.name if media else None, "host": self.ctx.host_id}
        lines: list[str] = []
        out = body(lines.append)
        return {**out, "log": lines}

    def _reset_evidence(self, b: "Board", mark):
        """For a plain reset: the SoC's boot-ROM fingerprint appearing on the console."""
        fps = [re.compile(f) for f in (self.socs.get(b.spec.soc) or {}).get("fingerprint", [])] if b.spec.soc else []

        def seen() -> str | None:
            text = b.console.read_since(mark) if fps else ""
            hit = next((f.pattern for f in fps if f.search(text)), None)
            return f"the boot ROM on the console (/{hit}/)" if hit else None
        return seen if fps else None

    def confirm(self, dut: str, note: str, request: str | None = None) -> dict[str, Any]:
        self.board(dut)
        return self.attention.confirm(dut, note or "", request or None)

    def boot(self, dut: str, sdk: str, method: str, variant: str | None, lease: str | None, brk: str | None = None) -> dict[str, Any]:
        b = self.board(dut)
        self.leases.check(dut, lease)
        self._guard_busy(dut)
        inst = self.current_catalog().get(sdk)
        if inst.soc and b.spec.soc and inst.soc != b.spec.soc:
            raise InvalidRequest(f"sdk {sdk} is for {inst.soc}, but {dut} is a {b.spec.soc} board ({b.spec.board})",
                                 hint=f"SDKs for {b.spec.soc}: {', '.join(self.effective_sdks(b.spec)) or '(none)'}")
        if sdk not in self.effective_sdks(b.spec):
            raise InvalidRequest(f"{dut} ({b.spec.board}) is not declared to run sdk {sdk}",
                                 hint=f"supported: {', '.join(self.effective_sdks(b.spec))}; "
                                      f"change with `boardctrl dut set {dut} --sdks ...` (empty = all SDKs of its SoC)")
        plan = inst.boot_plan(method, variant, brk)  # validate first: todo methods fail here with their notes
        if any(st.get("do") == "enter-fdl" or (st.get("do") == "reset" and not st.get("when"))
               for st in plan["steps"]):
            b.control.preflight()  # before the artifacts are pulled: a line that cannot be driven fails now
        runner = b.runner(self)
        rev, broken = self.loaded_rev, self._catalog_error

        hub_follower = self.ctx.services.get("catalog_follower")
        overridden = [x for x in (hub_follower.shadowed if hub_follower else []) if x["ref"] == sdk]

        def body(log):
            log(f"recipe from catalog revision {rev}" if rev else "recipe from the site's catalog")
            if broken:
                log(f"NOTE: the catalog on disk does not load, so the last one that did is used: {broken}")
            for x in overridden:
                log(f"NOTE: {x['file']} on this host differs from the registry's {sdk} and is NOT used; "
                    f"publish it with `boardctrl catalog put {sdk} -f <file>` if it is the newer one")
            return runner.run(inst, method, variant, log, brk)

        job = self._start_job(dut, f"boot {sdk} [{method}]", body,
                              kind="boot", sdk=sdk, method=method)
        self._event("boot", dut=dut, sdk=sdk, method=method, job=job.id)
        return {"job": job.id, "dut": dut, "sdk": sdk, "method": method, "host": self.ctx.host_id}

    def cancel(self, dut: str, lease: str | None) -> dict[str, Any]:
        """Stop the job running on a DUT (a boot or a flash): `job.cancel` on each, found by the board's name. What
        the job runs is stopped now (a program with what it started, a wait at once); it ends `cancelled`."""
        b = self.board(dut)
        self.leases.check(dut, lease)
        running = [j for j in self.ctx.jobs.list() if j.status == "running" and j.meta.get("dut") == b.spec.id]
        if not running:
            # Say what the board's last job did: a caller that came to stop one learns it already ended (and how).
            last = next((j for j in self.ctx.jobs.list() if j.meta.get("dut") == b.spec.id), None)
            if last is None:
                raise InvalidRequest(f"{dut}: no job running, and the host keeps no record of one run on it",
                                     hint="`boardctrl job list` shows the host's jobs; `boardctrl job cancel <id>` stops one by id",
                                     details={"running": False, "last_job": None})
            err = last.error or {}
            ended = f"its last job {last.id} ({last.kind}) {last.status}"
            if err.get("message"):
                ended += f": {err['message']}"
            raise InvalidRequest(
                f"{dut}: no job running; {ended}",
                hint=f"nothing to cancel. `boardctrl job get {last.id}` shows that job's log",
                details={"running": False,
                         "last_job": {"id": last.id, "kind": last.kind, "title": last.title, "status": last.status,
                                      "finished": last.finished, "error": last.error}})
        for job in running:
            self.ctx.jobs.cancel(job.id, lease, reason=f"dut cancel {dut}")
        self._event("cancel", dut=dut, jobs=[j.id for j in running])
        return {"dut": dut, "cancelled": [j.id for j in running]}

    def console(self, dut: str, cmd: str, expect: str | None, timeout: float, lease: str | None) -> dict[str, Any]:
        b = self.board(dut)
        self.leases.check(dut, lease)
        self._guard_busy(dut)
        out = b.console.run(cmd, expect or self._prompt_for(b), timeout)
        self._event("console", dut=dut, cmd=cmd)
        return {"dut": dut, **out}

    # ---------------------------------------------------------------- debug
    def debug_shelf(self) -> list[dict[str, Any]]:
        """Every board's debug provider, for the page and `boardctrl debug list`."""
        out = []
        for dut, b in sorted(self.boards.items()):
            try:
                state = b.debug.state() if hasattr(b.debug, "state") else {}
            except BspError as e:
                state = {"error": e.message}
            out.append({"dut": dut, "host": self.ctx.host_id, **self._debug_words(b.debug), **b.debug.shelf(),
                        **({"state": state} if state else {})})
        return out

    def debug_status(self, dut: str) -> dict[str, Any]:
        b = self.board(dut)
        return {"dut": dut, "host": self.ctx.host_id, **self._debug_words(b.debug), **b.debug.info(),
                **({"state": b.debug.state()} if hasattr(b.debug, "state") else {})}

    @staticmethod
    def _debug_words(provider: Any) -> dict[str, Any]:
        """How a page names a debug path: the provider's title and explanation, whichever views it answers with.

        A provider gets them from where it registered (`api.provider(..., title=...)`); they are reported here so a
        provider's own `info()` never has to repeat them, and a page never falls back to the registered name.
        """
        return {key: getattr(provider, key) for key in ("title", "about") if getattr(provider, key, "")}

    def debug_start(self, dut: str, lease: str | None) -> dict[str, Any]:
        """Bring a board's debug path up (a session under the board's lease)."""
        b = self.board(dut)
        self.leases.check(dut, lease)
        self._guard_busy(dut)
        out = b.debug.start(lease=lease)
        self._event("debug", dut=dut, action="start")
        return out

    def debug_stop(self, dut: str, lease: str | None) -> dict[str, Any]:
        b = self.board(dut)
        self.leases.check(dut, lease)
        out = b.debug.stop()
        self._event("debug", dut=dut, action="stop")
        return out

    def debug_action(self, dut: str, action: str, lease: str | None) -> dict[str, Any]:
        """One action of the provider (halt, run, reset, step, regs, …); the lease is held once, end to end."""
        b = self.board(dut)
        self.leases.check(dut, lease)
        if action not in b.debug.kinds:
            raise InvalidRequest(f"{dut}: its debug provider ({b.debug.provider_name}) offers {', '.join(b.debug.kinds) or 'nothing'}",
                                 hint=f"boardctrl debug status {dut} shows what it has")
        self._guard_busy(dut)
        out = b.debug.action(action)
        self._event("debug", dut=dut, action=action)
        return {"dut": dut, "action": action, **out}

    def renew(self, dut: str, lease: str | None, ttl: int) -> dict[str, Any]:
        self.board(dut)
        out = self.leases.renew(dut, lease, ttl)
        return {"dut": dut, "host": self.ctx.host_id,
                **{k: out[k] for k in ("token", "holder", "purpose", "expires_in_s")}}

    def present(self, dut: str) -> dict[str, Any]:
        b = self.board(dut)
        fb = b.fastboot.state()
        return {"dut": dut, "usb": b.spec.usb, "present": fb is not None, "fastboot": fb}

    def boot_marks(self, dut: str) -> dict[str, Any]:
        """The console lines that start a boot stage on this board, from its SoC's facts (none for an unknown SoC)."""
        soc = self.board(dut).spec.soc
        return {"dut": dut, "soc": soc, "marks": self.socs.boot_marks(soc)}

    def stage(self, dut: str, sha256: str, lease: str | None, cont: bool, timeout: float) -> dict[str, Any]:
        """Load one uploaded file into RAM through the download port (fastboot stage [+ continue])."""
        b = self.board(dut)
        self.leases.check(dut, lease)
        self._guard_busy(dut)
        path = self.store.get(sha256)
        self._busy[dut] = f"stage {path.name}"
        try:
            out = b.fastboot.cmd("stage", str(path), timeout=timeout)
            continued = False
            if cont:
                try:
                    out += "\n" + b.fastboot.cmd("continue", timeout=30)
                except HardwareError as e:      # the responder may detach on purpose
                    out += f"\n(continue: {e.message})"
                continued = True
        finally:
            self._busy.pop(dut, None)
        self._event("staged", dut=dut, sha256=sha256, file=path.name, continued=continued)
        return {"dut": dut, "sha256": sha256, "file": path.name, "size": path.stat().st_size,
                "continued": continued, "output": out}

    def boot_files(self, dut: str, files: list[str], lease: str | None, reset: bool, first_wait: float,
                   later_wait: float, port_timeout: float) -> dict[str, Any]:
        b = self.board(dut)
        self.leases.check(dut, lease)
        self._guard_busy(dut)
        if not files:
            raise InvalidRequest("no files to boot",
                                 hint="files = sha256, host:/absolute/path or http(s) URL, in boot order")
        # `host:` is the explicit board-host reference; a bare absolute path is one too (the older form). An
        # uploaded artifact and a URL are told apart from both. The job resolves them, so a big image is fetched
        # or read under the board's busy guard and never travels through the caller.
        refs = []
        for ref in files:
            path = ref[5:] if ref.startswith("host:") else ref
            source = ("host" if ref.startswith("host:")
                      else "url" if ref.startswith(("http://", "https://"))
                      else "host" if ref.startswith("/")
                      else "sha256")
            refs.append({"source": source, "path": path})

        def resolve(ref: dict[str, str], log) -> dict[str, Any]:
            import hashlib

            from core.dut import flashpkg

            source, path = ref["source"], ref["path"]
            if source == "url":
                got = flashpkg.fetch(self.store, flashpkg.check_url(path), None, log,
                                     ttl_s=self.flash_cfg.get("url_cache_ttl_s"))
                p, sha = Path(got["path"]), got["sha256"]
            elif source == "host":
                # A build product or share already on this host; hash what is actually read.
                p = Path(path)
                if not p.is_absolute() or not p.is_file():
                    raise NotFound(f"no such file on {self.ctx.host_id}: {path}",
                                   hint="a path here, on the board host, not on the machine you are running the CLI on")
                if not p.stat().st_size:
                    raise InvalidRequest(f"empty boot file on {self.ctx.host_id}: {path}")
                h = hashlib.sha256()
                with p.open("rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                sha = h.hexdigest()
                log(f"host file {p.name}: sha256 {sha}")
            else:
                p, sha = self.store.get(path), path
            return {"sha256": sha, "path": str(p), "name": p.name, "size": p.stat().st_size}

        if reset:
            b.control.preflight()  # before any file is fetched
        runner = b.runner(self)
        names = " ".join(Path(ref["path"]).name for ref in refs)

        def body(log) -> dict[str, Any]:
            items = [resolve(ref, log) for ref in refs]
            return runner.run_files(items, log, reset=reset, first_wait=first_wait,
                                    later_wait=later_wait, port_timeout=port_timeout)

        job = self._start_job(dut, f"boot files {names}", body, kind="boot",
                              files=[ref["path"] for ref in refs])
        self._event("boot", dut=dut, files=names, job=job.id)
        return {"job": job.id, "dut": dut, "files": refs, "host": self.ctx.host_id}

    # -------------------------------------------------------------- flash --
    # The flasher a board uses when neither its record nor its SoC facts name one: the vendor-neutral package layout
    # shipped with the SpacemiT platform. A site overrides it with `<site>/socs/<soc>/flash.flasher`, a board with
    # `flash.flasher`, or the host with `providers.dut.flash.flasher`; a host that carries no such hook says so and
    # lists the ones it does carry.
    DEFAULT_FLASHER = "spacemit-pkg"

    def flasher_for(self, b: Board, flasher: str | None = None) -> tuple[str, str]:
        """Which flash hook writes this board's packages, and why: the one asked for, else the board's
        `flash.flasher`, else its SoC facts' `flash.flasher`, else the host's, else the default."""
        if flasher:
            return flasher, "asked for"
        own = (b.spec.raw.get("flash") or {}).get("flasher")
        if own:
            return own, f"{b.spec.id}'s flash.flasher"
        soc = (self.socs.get(b.spec.soc).get("flash") or {}).get("flasher") if b.spec.soc else None
        if soc:
            return soc, f"SoC {b.spec.soc}'s flash.flasher"
        host = self.flash_cfg.get("flasher")
        if host:
            return host, "the host's flash.flasher"
        return self.DEFAULT_FLASHER, "the default"

    def flasher_chain(self, b: Board) -> list[tuple[str, str]]:
        """Every flasher this board could use, in order: its own `flash.flasher`, its `flash.fallback` list, its
        SoC's, the host's, then the default. The first that is ready on this host writes the package."""
        chain: list[tuple[str, str]] = []

        def add(block: Any, where: str) -> None:
            block = block or {}
            if block.get("flasher"):
                chain.append((str(block["flasher"]), f"{where} flash.flasher"))
            fb = block.get("fallback") or []
            for n in ([fb] if isinstance(fb, str) else fb):
                chain.append((str(n), f"{where} flash.fallback"))

        add(b.spec.raw.get("flash"), f"{b.spec.id}'s")
        if b.spec.soc:
            add(self.socs.get(b.spec.soc).get("flash"), f"SoC {b.spec.soc}'s")
        add(self.flash_cfg, "the host's")
        chain.append((self.DEFAULT_FLASHER, "the default"))
        seen: set[str] = set()
        return [(n, w) for n, w in chain if not (n in seen or seen.add(n))]

    def choose_flasher(self, b: Board, asked: str | None, request: dict[str, Any],
                       log: Any = None) -> tuple[str, str, list[str]]:
        """The flasher to use and why, checked before anything is fetched: each candidate's hook must be here and,
        when it can say, ready (its tool installed, say). One asked for by name is that one or an error; otherwise
        the first ready one of `flasher_chain`, with a note for each one passed over. Raises listing what is
        ready when none is."""
        from core.errors import NotFound

        available = self.hooks.names("flash")
        notes: list[str] = []
        if asked:
            if asked not in available:
                raise NotFound(f"no flasher {asked!r} on {self.ctx.host_id}",
                               hint=f"可用 flasher（available flashers）: {', '.join(available) or '(none)'}")
            st = self.hooks.check("flash", asked, request)
            if not st["ready"]:
                ready = [n for n in available if n != asked and self.hooks.check("flash", n, request)["ready"]]
                raise HardwareError(f"flasher {asked!r} cannot run on {self.ctx.host_id}: {st['reason']}",
                                    hint=(st["hint"] + "; " if st["hint"] else "")
                                    + f"ready flashers here: {', '.join(ready) or '(none)'}")
            return asked, "asked for", notes
        tried: list[str] = []
        for name, why in self.flasher_chain(b):
            if name not in available:
                tried.append(f"{name} ({why}): not on this host")
                continue
            st = self.hooks.check("flash", name, request)
            if st["ready"]:
                if tried:
                    notes = [f"passed over {t}" for t in tried]
                    why = f"{why}; " + "; ".join(t.split(" (")[0] + " unavailable" for t in tried)
                return name, why, notes
            tried.append(f"{name} ({why}): {st['reason']}" + (f" — {st['hint']}" if st["hint"] else ""))
        raise HardwareError(f"no flasher can write {b.spec.id}'s package on {self.ctx.host_id}",
                            hint="; ".join(tried) + f". flashers here: {', '.join(available) or '(none)'}",
                            details={"tried": tried})

    def flash(self, dut: str, package: str, sha256: str | None, flasher: str | None, lease: str | None,
              timeout: float, reset: bool, refresh: bool = False, plan: bool = False,
              partitions: list[str] | str | None = None) -> dict[str, Any]:
        """Write a package to the board.

        With the flash role's built-in `hook` provider: enter download mode, then run the flash hook on the unpacked
        package. With another provider (a site plugin's image writer): the file is resolved and verified here, and
        the provider writes it; what needs download mode, a hook or a package layout is that provider's business.

        `plan`: fetch and unpack the package as a flash would, then ask the flash hook what it would write (its
        media and partitions), touching no board; the job's result is `{"plan": ...}`. `partitions`: write only these
        (names as the plan gives them), passed to the hook as `args.only`; a hook that does not declare `only`
        refuses before anything is fetched.
        """
        import shutil
        import tempfile
        from pathlib import Path

        from core.dut import flashpkg
        from core.dut.defaults import HookFlash

        b = self.board(dut)
        self.leases.check(dut, lease)
        self._guard_busy(dut)
        provider = b.flash_provider
        hook_based = isinstance(provider, HookFlash)
        only = _partition_names(partitions)
        if (plan or only) and not hook_based:
            raise InvalidRequest(f"{dut}'s flash provider {provider.provider_name} writes the whole file",
                                 hint="it cannot say what a package holds or write only some partitions")
        usb = str(b.spec.fastboot.get("usb") or "")
        if hook_based and not plan and (not usb or "UNKNOWN" in usb.upper()):
            raise InvalidRequest(f"{dut} has no download port (fastboot.usb) recorded",
                                 hint=f"set it: `boardctrl dut set {dut} --usb usb:<bus>-<port>` "
                                      "(the path `fastboot devices -l` shows in download mode)")
        images = {k: v["path"] for k, v in (self.socs.get(b.spec.soc).get("images") or {}).items() if v.get("path")}
        passed_over: list[str] = []
        if hook_based:
            # Before any transfer: a flasher that cannot run here is known now, not after a 2 GB download.
            name, why, passed_over = self.choose_flasher(
                b, flasher, {"dut": dut, "usb": b.spec.usb, "fastboot": b.fastboot.binary, "images": images,
                             "args": expand(dict(self.flash_cfg.get("args") or {}), self.ctx.env)})
        else:
            name, why = provider.provider_name, f"{dut}'s flash provider"
        if hook_based and (plan or only):
            # Before any transfer: the hook must know how to plan, or to write only some partitions.
            need = "plan" if plan else "only"
            if not self.hooks.declares("flash", name, need):
                raise InvalidRequest(f"flasher {name} cannot {'say what a package would write' if plan else 'write only some partitions'}",
                                     hint=f"it does not declare `boardctrl-hook: {need}`"
                                          + ("" if plan else "; flash the whole package"))
        if plan:
            pass                    # a plan touches no board: no reset, no download port
        elif hook_based and reset:
            b.control.preflight()  # the reset this flash starts with can happen: known before the download
        elif hook_based:
            b.runner(self).require_download_port()   # --no-reset: the board is in download mode now, or no download
        max_bytes = int(self.flash_cfg.get("max_package_bytes") or flashpkg.DEFAULT_MAX_BYTES)
        package = (package or "").strip()
        url = None
        if flashpkg.is_url(package):
            # A build server's file (ssh://) is checked against the site's build servers before anything is pulled.
            if package.lower().startswith("ssh://"):
                flashpkg.check_ssh(package, self.sdks.servers)
                url = package
            else:
                url = flashpkg.check_url(package)
            if sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
                raise InvalidRequest(f"not a sha256: {sha256!r}", hint="64 hex digits")
            label = package.rsplit("/", 1)[-1] or package
        else:
            if not re.fullmatch(r"[0-9a-f]{64}", package):
                raise InvalidRequest(f"package {package!r} is neither an http(s) URL nor the sha256 of an uploaded file",
                                     hint="`boardctrl dut flash DUT --package PATH` uploads a local file or directory for you")
            if sha256 and sha256.lower() != package:
                raise InvalidRequest("sha256 does not match the uploaded package's")
            label = self.store.get(package).name
        runner = b.runner(self)

        def body(log) -> dict[str, Any]:
            import time

            t0 = time.monotonic()
            for note in passed_over:
                log(f"flash {dut}: {note}")
            log(f"flash {dut} with {name} ({why}): package {label}")
            token = current_token()
            token.at("fetch the package")
            if url and url.lower().startswith("ssh://"):
                got = flashpkg.pull_ssh(self.store, url, self.sdks.servers, sha256, log, max_bytes)
                path = Path(got["path"])
            elif url:
                got = flashpkg.fetch(self.store, url, sha256, log, max_bytes, ttl_s=self.flash_cfg.get("url_cache_ttl_s"),
                                     refresh=refresh)
                path = Path(got["path"])
            else:
                path = self.store.get(package)
                self.store.hold(package)              # kept while this job writes it
            token.check()
            self._flash_work.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix=f"{dut}-", dir=self._flash_work))
            try:
                if not hook_based:
                    # The provider writes the file itself: no unpacking, no download mode, no hook.
                    log(f"{dut}: {name} writes {path.name}")
                    token.at(f"{name} writes {path.name}")
                    res = provider.flash(path, log=log, timeout=timeout)
                    token.check()
                    out = {"dut": dut, "flasher": name, "package": path.name,
                           "elapsed_s": round(time.monotonic() - t0, 1), **dict(res or {})}
                    log(f"{dut} written by {name} in {out['elapsed_s']}s")
                    self._event("flashed", dut=dut, flasher=name, package=path.name)
                    return out
                token.at(f"unpack {path.name}")
                # Only some partitions: the rest of the package is read past, not unpacked (a whole image can be
                # several gigabytes). A directory uploaded as it is comes whole.
                pkg_dir = flashpkg.unpack(path, work / "pkg", max_bytes=4 * max_bytes,
                                          want=flashpkg.only_entries(path, only) if only else None)
                files = sum(1 for p in pkg_dir.rglob("*") if p.is_file())
                log(f"unpacked {path.name}: {files} file(s)")
                if plan:
                    token.at(f"flash/{name} reads the package")
                    got = self.hooks.run("flash", name, {"plan": True, "dut": dut, "package_dir": str(pkg_dir),
                                                         "images": images, "env": dict(self.ctx.env),
                                                         "args": expand(dict(self.flash_cfg.get("args") or {}), self.ctx.env)},
                                         timeout=min(timeout, 300), log=log)
                    if got.code != 0 or got.data.get("error") or not isinstance(got.data.get("plan"), dict):
                        err = got.data.get("error") or "the flasher gave no plan"
                        raise HardwareError(f"flash/{name}: {err}", hint=got.data.get("hint") or "")
                    out_plan = {**got.data["plan"], "package": path.name}
                    log(f"{path.name}: {sum(len(m.get('partitions') or []) for m in out_plan.get('media') or [])} "
                        f"partition(s) on {len(out_plan.get('media') or [])} medium/media; nothing was written")
                    return {"dut": dut, "flasher": name, "package": path.name, "plan": out_plan,
                            "elapsed_s": round(time.monotonic() - t0, 1)}
                token.at("enter download mode")
                token.check()
                if reset:
                    runner.enter_fdl(log)
                else:
                    runner.require_download_port()   # still there, and still in download mode
                token.at(f"flash/{name}")
                token.check()
                artifacts = {"package": str(path)}
                if flashpkg.kind_of(path) == "zip":
                    artifacts["flash-zip"] = str(path)
                req = {"dut": dut, "usb": b.spec.usb, "console": b.console.alias, "fastboot": b.fastboot.binary,
                       "package_dir": str(pkg_dir), "artifacts": artifacts, "images": images, "timeout": timeout,
                       "product_dir": str(pkg_dir),      # the unpacked package is the product here
                       "env": dict(self.ctx.env),
                       "network": {**self.network(), "console": b.console.alias},
                       "args": expand(dict(self.flash_cfg.get("args") or {}), self.ctx.env)}
                if only:
                    req["args"]["only"] = only
                try:
                    res = self.hooks.run("flash", name, req, timeout=timeout, log=log,
                                         progress=getattr(log, "progress", None))
                except BspError as e:
                    if e.code == "hardware_timeout":
                        e.hint = "give it more time with --timeout, and check what the flasher printed last"
                    raise
                if res.code != 0 or res.data.get("error"):
                    err = res.data.get("error") or "the flasher did not flash"
                    if isinstance(err, dict):
                        err = err.get("message") or json.dumps(err)
                    raise HardwareError(f"flash/{name}: {err}", hint=res.data.get("hint") or "",
                                        details={"result": res.data, "stdout": res.lines[-20:]})
            finally:
                shutil.rmtree(work, ignore_errors=True)
            out = {"dut": dut, "flasher": name, "package": path.name, "elapsed_s": round(time.monotonic() - t0, 1)}
            if "flashed" in res.data:
                out["flashed"] = res.data["flashed"]
            log(f"flashed {dut} with {name} in {out['elapsed_s']}s"
                + (f": {len(out['flashed'])} partition(s)" if isinstance(out.get("flashed"), list) else ""))
            self._event("flashed", dut=dut, flasher=name, package=path.name)
            return out

        if plan:
            # Reading a package is not an operation on the board: it does not make the board busy.
            job = self.ctx.jobs.start("flash-plan", f"{dut}: plan {label} [{name}]", body,
                                      guard=lambda lease_: self.leases.check(dut, lease_), dut=dut, flasher=name)
            return {"job": job.id, "dut": dut, "flasher": name, "package": label, "host": self.ctx.host_id, "plan": True}
        what = f"flash {label} [{name}]" + (f" only {','.join(only)}" if only else "")
        job = self._start_job(dut, what, body, kind="flash", flasher=name)
        self._event("flash", dut=dut, flasher=name, package=label, job=job.id)
        return {"job": job.id, "dut": dut, "flasher": name, "package": label, "host": self.ctx.host_id,
                **({"partitions": only} if only else {})}

    def artifact_has(self, sha256: str) -> dict[str, Any]:
        p = self.store.find(sha256)
        return {"sha256": sha256, "present": p is not None, "size": p.stat().st_size if p else None,
                "file": p.name if p else None}

    def effective_sdks(self, spec: DutSpec) -> list[str]:
        """Declared sdks, else every SDK instance for the board's SoC."""
        if spec.sdks:
            return list(spec.sdks)
        return sorted(i for i, inst in self.sdks.instances.items() if spec.soc and inst.soc == spec.soc)

    def _prompt_for(self, b: Board) -> str:
        explicit = b.spec.console.get("prompt")
        if explicit:
            return explicit
        prompts = {self.sdks.get(s).prompt for s in self.effective_sdks(b.spec) if s in self.sdks.instances}
        return "|".join(sorted(prompts)) or "# $"

    def log(self, dut: str, lines: int, grep: str | None) -> dict[str, Any]:
        return {"dut": dut, "lines": self.board(dut).console.tail(lines, grep)}

    def logs(self, dut: str, since: str | None = None, until: str | None = None, grep: str | None = None,
             regex: bool = False, limit: int = 20000, first: bool = False, at: str | None = None,
             before: int = 0, after: int = 0, hits: bool = False, max_hits: int = 200000, marks: Any = None,
             max_marks: int = 100000, from_line: int = 0) -> dict[str, Any]:
        """The console's log over a time range, searched, or a window of it around a line (see SerialConsole.range)."""
        if regex and grep:
            try:
                re.compile(grep)
            except re.error as e:
                raise InvalidRequest(f"not a valid regex: {e}") from None
        if at is not None and not _LOG_AT.match(at):
            raise InvalidRequest(f"not a log position: {at!r}",
                                 hint="`start`, `end`, `line:<n>` or `<YYYY-MM-DD>:<byte offset>` as a line's position")
        for name, v in (("before", before), ("after", after), ("max_hits", max_hits), ("max_marks", max_marks),
                        ("from_line", from_line)):
            if int(v) < 0:
                raise InvalidRequest(f"`{name}` must not be negative, got {v}")
        if isinstance(marks, str):
            try:
                marks = json.loads(marks) if marks.strip() else None
            except ValueError as e:
                raise InvalidRequest(f"marks is not JSON: {e}", hint='a list: [{"id": "spl", "pattern": "U-Boot SPL", "flags": ""}]') from None
        if marks is not None and (not isinstance(marks, list) or not all(
                isinstance(m, dict) and isinstance(m.get("id"), str) and isinstance(m.get("pattern"), str)
                and m.get("flags", "") in ("", "i", None) for m in marks)):
            raise InvalidRequest("marks must be a list of {id, pattern, flags}",
                                 hint='flags is "" or "i": [{"id": "spl", "pattern": "U-Boot SPL", "flags": ""}]')
        if marks:
            for m in marks:
                try:
                    re.compile(m["pattern"])
                except re.error as e:
                    raise InvalidRequest(f"mark {m['id']!r}: not a valid regex: {e}") from None
        extra: dict[str, Any] = {}
        if at is not None:
            extra = {"at": at, "before": min(int(before), 5000), "after": min(int(after), 5000)}
        elif hits or marks or from_line:
            extra = {"hits": bool(hits), "max_hits": max(1, min(int(max_hits), 1000000)), "marks": marks or None,
                     "max_marks": max(1, min(int(max_marks), 1000000)), "from_line": int(from_line)}
        # Only what the call asked for reaches the provider, so one that predates windows still answers a search.
        return {"dut": dut, **self.board(dut).console.range(since, until, grep, regex, max(1, min(int(limit), 200000)),
                                                             first, **extra)}

    def _check_providers(self, raw: dict[str, Any]) -> None:
        """Refuse a record naming a provider no one registered, before it is written: a name saved now would be a
        host that cannot start next time, and the person is here to be told the names that do exist."""
        from core.dut import services as bindings
        from core.dut.records import spec_from_dict

        try:
            spec = spec_from_dict({**raw, "host": self.ctx.host_id})
        except InvalidRequest:
            return                       # the record's own shape is checked where it is written
        bad = bindings.unknown_providers({k: v for k, v in bindings.bindings(spec).items() if k in
                                          (raw.get("services") or {})})
        if bad:
            raise InvalidRequest(f"{raw.get('id')}: {bindings.unknown_message(bad)}",
                                 hint="`boardctrl status` lists the providers this host carries, by role")

    def add(self, spec: Any, replace: bool) -> dict[str, Any]:
        import json

        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except ValueError as e:
                raise InvalidRequest(f"spec is not valid JSON: {e}") from None
        if not isinstance(spec, dict):
            raise InvalidRequest("spec must be a JSON object (see tests/fixtures/site/duts/*.yaml for the shape)")
        self._check_providers(spec)
        con = spec.get("console") or {}
        start = con.pop("start", False)
        s = self.registry.add(spec, replace)
        if start or con.get("device"):
            self.consoles.ensure(con["alias"], con.get("device"))
        self._rebuild()
        self.boards[s.id].control.setup()
        if not self.slots.by_alias(s.console_alias):
            self.refresh_slots()
        self._note_slot(s, "added")
        self._event("added", dut=s.id, slot=self.slot_of(s.id))
        return self.status(s.id)

    def _note_slot(self, s: DutSpec, event: str) -> None:
        reset = (s.control.get("reset") or {}).get("pin")
        self.slots.note(s.console_alias, event, dut=s.id, board=s.board or None, soc=s.soc or None,
                        reset=reset, control=s.control.get("backend"),
                        usb=s.usb if s.usb and "UNKNOWN" not in s.usb else None)

    def _free_for_edit(self, dut: str, lease: str | None) -> None:
        cur = self.leases.view(dut)
        if cur:
            self.leases.check(dut, lease)        # only the holder may edit a leased DUT
        self._guard_busy(dut)

    def rename(self, dut: str, new: str, lease: str | None, console: bool) -> dict[str, Any]:
        import re

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", new or ""):
            raise InvalidRequest(f"invalid DUT name {new!r}", hint="letters, digits, _ . - (max 63)")
        self._free_for_edit(dut, lease)
        old = self.board(dut).spec
        raw = {**old.raw, "id": new}
        renamed_console = None
        if console or old.console_alias == old.id:
            raw["console"] = {**old.console, "alias": new}
            renamed_console = (old.console_alias, new)
        self.registry.update(dut, raw)
        self._drop_aliases(dut)
        if renamed_console:
            self.consoles.rename(*renamed_console, old.console.get("device"))
            self.slots.rename_alias(*renamed_console)
        lease_info = self.leases.view(dut)
        if lease_info:
            self.leases.release(dut, lease, force=True)
        self._rebuild()
        self.boards[new].control.setup()
        self.slots.note(self.boards[new].spec.console_alias, "renamed", dut=new, old=dut)
        self._event("renamed", dut=new, old=dut)
        return {**self.status(new), "renamed_from": dut,
                "note": "lease dropped by rename; acquire the new name" if lease_info else None}

    def set_fields(self, dut: str, lease: str | None, **fields: Any) -> dict[str, Any]:
        from core.dut import services as bindings

        self._free_for_edit(dut, lease)
        spec = self.board(dut).spec
        raw = {**spec.raw, "console": dict(spec.console), "fastboot": dict(spec.fastboot),
               "control": {**spec.control, "reset": dict(spec.control.get("reset") or {}),
                           "fdl": dict(spec.control.get("fdl") or {})}}
        changed = []
        for key in ("board", "soc", "description"):
            if fields.get(key) is not None:
                raw[key] = fields[key]
                changed.append(key)
        if fields.get("sdks") is not None:
            raw["sdks"] = [x for x in fields["sdks"].split(",") if x.strip()]
            unknown = [x for x in raw["sdks"] if x not in self.sdks.instances]
            if unknown:
                raise InvalidRequest(f"unknown sdk(s) {unknown}", hint=f"known: {', '.join(self.sdks.instances.names())}")
            changed.append("sdks")
        if fields.get("usb"):
            raw["fastboot"]["usb"] = fields["usb"]
            changed.append("usb")
        for role in ("reset", "fdl"):
            if fields.get(f"{role}_actor") != "human":
                continue
            raw["control"].pop(role, None)
            if role == "fdl":
                raw["control"]["backend"] = "human"
                raw["control"]["fdl"]["strategy"] = "button"
            changed.append(f"{role}_actor")
        if fields.get("reset_pin"):
            raw["control"]["reset"]["pin"] = fields["reset_pin"]
            # Both roles relay-driven means no human coordinator; otherwise a person handles FDL and this pin resets.
            if spec.control.get("backend") == "human" and (raw["control"].get("fdl") or {}).get("pin"):
                raw["control"]["backend"] = "gpio-relay"
            elif spec.control.get("backend") != "human":
                raw["control"]["backend"] = "gpio-relay"
            changed.append("reset_pin")
        if fields.get("pulse_ms"):
            raw["control"]["reset"]["pulse_ms"] = int(fields["pulse_ms"])
            changed.append("pulse_ms")
        if fields.get("fdl"):
            raw["control"]["fdl"]["strategy"] = fields["fdl"]
            changed.append("fdl")
        if fields.get("fdl_pin"):
            raw["control"]["fdl"]["pin"] = fields["fdl_pin"]
            changed.append("fdl_pin")
        # The roles' providers: each field writes the record's `services` block, so what a host runs is what the
        # record says (`bindings()` reads it back).
        block = bindings.services_from_fields(spec, **fields)
        if block is not None:
            raw["services"] = block
            changed.append("services")
        if not changed:
            raise InvalidRequest("nothing to change", hint="see `boardctrl dut set --help`")
        self._check_providers(raw)
        self.registry.update(dut, raw)
        self._drop_aliases(dut)
        self._rebuild()
        self.boards[dut].control.setup()
        self._event("updated", dut=dut, fields=changed)
        return {**self.status(dut), "changed": changed}

    def export(self) -> dict[str, Any]:
        keep = ("id", "host", "board", "soc", "description", "console", "fastboot", "control", "sdks", "identity")
        return {"duts": [{k: s.raw[k] for k in keep if k in s.raw} for s in self.registry.all().values()]}

    def _drop_aliases(self, dut: str) -> None:
        try:
            self.ctx.service("gpio").alias_delete(dut, prefix=True)
        except BspError:
            pass

    def remove(self, dut: str, force: bool) -> dict[str, Any]:
        if self.leases.view(dut) and not force:
            raise Conflict(f"{dut} is leased", hint="release it first or pass force=true")
        s = self.registry.remove(dut)
        self._drop_aliases(dut)
        self._rebuild()
        self.slots.note(s.console_alias, "removed", dut=s.id, board=s.board or None)
        self._event("removed", dut=dut)
        return {"removed": s.id}

    def probe(self, pins: str | None, consoles: str | None, identify: bool, sdks: str | None,
              dry_run: bool, human: bool = False, rounds: int = 8, wait_s: float = 120,
              register_found: bool = False, known: bool = False, prefer: str | None = None) -> dict[str, Any]:
        from core.dut.probe import Prober

        prober = Prober(self, pins=pins, consoles=consoles, identify=identify,
                        default_sdks=[s for s in (sdks or "").split(",") if s] or None,
                        human=human, rounds=rounds, wait_s=wait_s, register=register_found, known=known,
                        prefer=prefer)
        if dry_run:
            return prober.plan()
        job = self.ctx.jobs.start("probe", f"{self.ctx.host_id}: probe", prober.run, host=self.ctx.host_id)
        return {"job": job.id, "host": self.ctx.host_id}

    def rebuild(self, identify: bool = True, sdks: str | None = None, dry_run: bool = False,
                keep_used: bool = False) -> dict[str, Any]:
        """Start the host's wiring over after it was re-cabled, then rediscover its boards.

        The whole host (default): forget every probed DUT and every slot, give each adapter plugged in now a fresh
        slot and console, then probe them all (reset lines boards used before first, the boards' old names kept
        when identify reads the same board) and register each board as found. Nothing starts while a DUT is leased
        or busy.

        `keep_used`: only what moved. DUTs that are leased or busy keep their slots and wiring untouched; a DUT
        whose console is gone (its adapter unplugged or moved) is forgotten with its slot; every adapter plugged in
        now gets a slot; then the free consoles are probed on reset lines no kept DUT uses, so boards in use are
        never reset. It runs while others hold boards.

        DUTs from the site's catalog stay either way."""
        used = {d for d in self.boards if self.leases.view(d) or self._busy.get(d)}
        if used and not keep_used:
            held = sorted(used)
            raise Conflict(f"{', '.join(held)} {'is' if len(held) == 1 else 'are'} leased or busy",
                           hint="a rebuild resets every board on the host: release them first, or rediscover "
                                "with keep_used to keep the boards in use")
        probed = sorted(d for d in self.boards if self.registry.origin(d) == "probed")
        if keep_used:
            forget = [d for d in probed if d not in used and self._slot_online(self.boards[d]) is False]
        else:
            forget = probed
        keep_aliases = {self.boards[d].spec.console_alias for d in self.boards if d not in forget}
        plan = {"host": self.ctx.host_id, "mode": "keep-used" if keep_used else "all", "forget_duts": forget,
                "keep_duts": sorted(set(self.boards) - set(forget)), "in_use": sorted(used),
                "slots": [s["id"] for s in self.slots.all()]}
        if dry_run:
            return plan

        def keep_slot(slot: dict[str, Any]) -> bool:
            if slot.get("alias") in keep_aliases:
                return True
            # Rediscovering keeps every slot whose adapter is still here; only the gone ones are dropped.
            return keep_used and bool(self.slots.view(slot).get("online"))

        def body(log) -> dict[str, Any]:
            from core.dut.probe import Prober

            for dut in forget:
                s = self.registry.remove(dut)
                self._drop_aliases(dut)
                self.slots.note(s.console_alias, "removed", dut=s.id, board=s.board or None)
                log(f"forgot {dut} (was on console {s.console_alias})")
                self._busy.pop(dut, None)     # a forgotten name is not busy: the host no longer knows it
            if keep_used and used:
                log(f"kept in use: {', '.join(sorted(used))}")
            self._rebuild()
            dropped = self.slots.rebuild(keep_slot)["dropped"]
            for slot in dropped:
                if slot.get("alias"):
                    self.consoles.stop(slot["alias"])
            log(f"dropped {len(dropped)} slot(s); matching the adapters plugged in now")
            change = self.refresh_slots()
            log(f"slots now: {', '.join(s['id'] + ('' if s['online'] else ' (offline)') for s in self.slots.all()) or 'none'}"
                + (f"; consoles that did not start: {change.get('failed')}" if change.get("failed") else ""))
            self._event("rebuilt", forgot=forget, kept=sorted(used), slots=[s["id"] for s in self.slots.all()])
            # The prober skips the consoles, reset lines and download ports of registered DUTs, so the boards kept
            # in use are never touched.
            prober = Prober(self, pins=None, consoles=None, identify=identify,
                            default_sdks=[s for s in (sdks or "").split(",") if s] or None, register=True)
            result = prober.run(log)
            return {**result, "forgot": forget, "kept": sorted(used)}

        def run_body(log) -> dict[str, Any]:
            try:
                return body(log)
            finally:
                # The boards this job held busy are free once it ends, whether it finished or stopped: a forgotten
                # board the prober registered again is a board someone may reset the moment this job is over.
                for d in forget:
                    if self._busy.get(d) == "rebuild":
                        self._busy.pop(d, None)

        for d in forget:
            self._busy[d] = "rebuild"
        try:
            job = self.ctx.jobs.start("probe", f"{self.ctx.host_id}: {'rediscover' if keep_used else 'rebuild'}",
                                      run_body, host=self.ctx.host_id)
        except BaseException:
            for d in forget:
                self._busy.pop(d, None)
            raise
        return {"job": job.id, **plan}

    def refresh(self, dut: str, lease: str | None, name: str | None = None, rename: bool = False,
                board: str | None = None, soc: str | None = None) -> dict[str, Any]:
        """Re-read the board in a DUT's slot, as a job (see `_refresh`)."""
        if name and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
            raise InvalidRequest(f"invalid DUT name {name!r}", hint="letters, digits, _ . - (max 63)")
        if name and name != dut and name in self.boards:
            raise Conflict(f"DUT {name} already exists on {self.ctx.host_id}")
        self.leases.check(dut, lease)
        self._guard_busy(dut)
        job = self._start_job(dut, "refresh", lambda log: self._refresh(dut, name, rename, board, soc, log))
        return {"job": job.id, "host": self.ctx.host_id, "dut": dut}

    def _usb_now(self) -> dict[str, dict[str, Any]]:
        vids = self.probe_cfg.get("usb_vids") or self.socs.usb_vids()
        res = self.hooks.run("probe", "usb-devices", {"vids": vids, "env": dict(self.ctx.env),
                                                     "network": self.network()}, timeout=30)
        return {d["usb"]: d for d in res.data.get("devices", [])}

    def _refresh(self, dut: str, name: str | None, rename: bool, board_name: str | None, soc_given: str | None,
                 log) -> dict[str, Any]:
        import copy
        import time

        from core.dut.probe import identify_board

        b = self.board(dut)
        spec = b.spec
        taken = {x.spec.usb for i, x in self.boards.items() if i != dut}
        before = self._usb_now()
        mark = b.console.mark()
        log(f"refresh {dut}: reset into download mode")
        b.control.enter_fdl(log, b.runner(self).download_evidence(mark))
        # The board's download port: where it was, or, after re-cabling, the one port that just enumerated.
        deadline, usb, text, products = time.monotonic() + 30, None, "", ""
        while time.monotonic() < deadline:
            text = b.console.read_since(mark)
            after = self._usb_now()
            fresh = [u for u, d in after.items() if u not in taken and
                     (u not in before or before[u].get("devnum") != d.get("devnum"))]
            products = " ".join(d.get("product", "") for d in after.values())
            if spec.usb in after:
                usb = spec.usb
                break
            if len(fresh) == 1:
                usb = fresh[0]
                log(f"download port moved: {spec.usb} -> {usb}")
                break
            current_token().sleep(0.5)
        soc = self.socs.detect(text)[0] or soc_given or spec.soc
        fdl = self.socs.in_download_mode(text, products) or bool(usb)
        if not usb:
            log(f"no download-mode USB device appeared (was {spec.usb}): plug its download cable into this host")
        ident = identify_board(self, usb, fdl, soc, str(self.consoles.run_dir.parent / "logs" / spec.console_alias), log)
        fields = ident.get("fields") or {}
        raw = copy.deepcopy(spec.raw)
        changed = []

        def put(key, value):
            if value and raw.get(key) != value:
                raw[key] = value
                changed.append(key)
        put("board", board_name or fields.get("product"))
        put("soc", soc)
        put("description", fields.get("model"))
        if ident:
            raw["identity"] = ident
        if usb and usb != spec.usb:
            raw["fastboot"] = {**spec.fastboot, "usb": usb}
            changed.append("usb")
        new = name or (ident.get("name_hint") if rename else None) or dut
        if new != dut:
            if new in self.boards:
                new = f"{new}-{dut}"
            raw["id"] = new
            changed.append("id")
        raw["refreshed"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "identified": bool(ident)}
        renamed_console = None
        if new != dut and spec.console_alias == dut:
            raw["console"] = {**spec.console, "alias": new}
            renamed_console = (dut, new)
        self.registry.update(dut, raw)
        if new != dut:
            self._drop_aliases(dut)
            self.leases.move(dut, new)
            if renamed_console:
                self.consoles.rename(*renamed_console, spec.console.get("device"))
                self.slots.rename_alias(*renamed_console)
        self._rebuild()
        self.boards[new].control.setup()
        self._note_slot(self.boards[new].spec, "refreshed")
        self._event("refreshed", dut=new, old=dut if new != dut else None, changed=changed)
        log(f"refreshed {new}: " + (", ".join(f"{k} {raw.get(k) if k != 'usb' else usb}" for k in changed) or "nothing changed"))
        return {"dut": new, "renamed_from": dut if new != dut else None, "changed": changed,
                "board": raw.get("board"), "soc": raw.get("soc"), "usb": raw["fastboot"]["usb"],
                "identified": bool(ident), "slot": self.slot_of(new)}

    # ----------------------------------------------------------- plumbing --
    def ports(self) -> dict[str, list[str]]:
        """Serial and USB devices on this host right now (as the port watcher last saw them)."""
        return self.port_watcher.current if self.port_watcher else {}

    def usb_ports(self) -> list[dict[str, Any]]:
        """USB devices that may be a board's download port, as `probe --dry-run` reports them."""
        from core.dut.probe import Prober
        return list(Prober(self, pins=None, consoles=None, identify=False, default_sdks=None).usb_snapshot().values())

    def download_ports(self) -> list[dict[str, str]]:
        """Known download-port candidates from the existing watcher snapshot; no board discovery or reset.

        Registration needs only these selectors, not a full probe plan. The watcher keeps vendor/product ids; the
        configured SoC vendor filter removes ordinary USB devices without running a hook for every page view.
        """
        vids = {str(v).lower() for v in (self.probe_cfg.get("usb_vids") or self.socs.usb_vids())}
        out = []
        for entry in self.ports().get("usb", []):
            usb, _, ids = str(entry).partition(" ")
            vid, _, pid = ids.partition(":")
            if vids and vid.lower() not in vids:
                continue
            out.append({"usb": usb, "vid": vid, "pid": pid})
        return out

    def _ports_changed(self, change: dict, now: dict) -> None:
        # A new adapter is a new slot with its own console; one that went away drops its slot, or leaves it offline
        # (and the card greyed) while a board is registered there.
        if any(str(x).startswith("/dev/") for x in change["added"] + change["removed"]):
            self.refresh_slots()
        self._event("ports", added=change["added"], removed=change["removed"])

    def startup(self) -> None:
        """Build every board of the site's records, then start the port watcher and the consoles it has.

        Boards are built here, not in `__init__`: a record may bind a provider any module registered in its setup,
        and every module's setup has run by the time the host starts.
        """
        # A session recorded before a crash or a reboot, whose unit and process are both gone, is not running: forget
        # it, so a board does not look debugged by something that is not there.
        try:
            self.sessions.reap_stale()
        except Exception as e:  # noqa: BLE001 - a stale record is housekeeping; it never stops the host
            log.warning("could not check the sessions left from a previous run: %s", e)
        self._rebuild()
        from core.dut.ports import PortWatcher

        cfg = self.config.get("watch_ports", True)
        if cfg:
            interval = cfg.get("interval_s", 2.0) if isinstance(cfg, dict) else 2.0
            self.port_watcher = PortWatcher(self._ports_changed, interval).start()
        for b in self.boards.values():
            try:
                b.control.setup()
            except BspError as e:
                self._event("error", dut=b.spec.id, error=e.to_dict())
            # A console provider that is not served by the slots (a trace channel, a network console) starts what it
            # needs itself; the slot table starts the serial adapters, one slot each.
            if not b.console_alias:
                continue
            try:
                self.consoles.ensure(b.spec.console_alias, b.spec.console.get("device"), only_if_known=True) \
                    if b.console.served_by_slots else b.console.ensure()
            except BspError as e:
                self._event("error", dut=b.spec.id, error=e.to_dict())
        self.refresh_slots()

    def shutdown(self) -> None:
        if self.port_watcher:
            self.port_watcher.stop()

    def commands(self) -> list[Command]:
        return build_commands(self)


# =================================================================== views --
def resolve_network(cfg: dict[str, Any] | None, bind: str = "127.0.0.1",
                    announce: dict[str, Any] | None = None, local=None) -> dict[str, Any]:
    """The network facts a hook may need, with defaults from `server.bind`/`server.announce`.

    `core.dut.network` wins; then an address the host announces to its registry; then a
    concrete `server.bind`; then this machine's own addresses. `board_subnet` has no default:
    boardctrl does not run the boards' DHCP server and cannot know it, so it is null unless set.
    """
    cfg = dict(cfg or {})
    announced = [str(a) for a in ((announce or {}).get("addresses") or []) if a]
    host = str(cfg.get("host_address") or "") or (announced[0] if announced else "")
    if not host and bind and bind not in ("0.0.0.0", "::", "", "localhost", "127.0.0.1", "::1"):
        host = bind
    if not host:
        found = local() if callable(local) else (local if local is not None else local_addresses())
        host = found[0] if found else None
    export = str(cfg.get("export_address") or "") or (announced[0] if announced else host)
    return {"board_subnet": cfg.get("board_subnet") or None,
            "host_address": host, "export_address": export}


def _stage(fb: dict | None, download_rx: str | None) -> str:
    """Coarse stage from the fastboot view only: download mode / fastboot / not on the bus."""
    import re

    if not fb or fb.get("error"):
        return "off-bus"
    if download_rx and re.search(download_rx, fb.get("product", "")):
        return "download-mode"
    return "fastboot"


_PARTITION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")
# Where a log window starts: either end, a line number, or a line's position (`<day>:<byte offset>`)
_LOG_AT = re.compile(r"^(start|end|line:\d+|\d{4}-\d\d-\d\d:\d+)$")


def _partition_names(value: list[str] | str | None) -> list[str]:
    """`partitions` as given (a list, or `a,b`): the names, checked, in order, once each."""
    if not value:
        return []
    items = value.split(",") if isinstance(value, str) else [str(v) for v in value]
    names: list[str] = []
    for item in (i.strip() for i in items):
        if not item:
            continue
        if not _PARTITION.fullmatch(item):
            raise InvalidRequest(f"not a partition name: {item!r}", hint="letters, digits, . _ + -")
        if item not in names:
            names.append(item)
    return names
