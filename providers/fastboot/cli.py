"""The host's `fastboot` CLI, bound to one USB path (multi-DUT safe: always `-s usb:<path>`)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from core import proc
from core.errors import HardwareError, HardwareTimeout
from core.dut.contracts import DownloadPort, Log


def _run(argv: list[str], timeout: float):
    """Run fastboot in its own process group: a timeout or the job's cancel stops it (and what it started)."""
    try:
        return proc.run(argv, timeout=timeout)
    except proc.TimedOut:
        raise HardwareTimeout(f"`{' '.join(argv[:4])} ...` timed out after {timeout:g}s") from None
    except FileNotFoundError:
        raise HardwareError(f"{argv[0]} not found on this host",
                            hint="install it or set its path in the host config") from None


class Fastboot:
    """fastboot CLI bound to one USB path (multi-DUT safe: always `-s usb:<path>`)."""

    def __init__(self, usb: str, binary: str = "fastboot"):
        self.usb = usb
        self.binary = binary

    def devices(self) -> list[dict[str, str]]:
        r = _run([self.binary, "devices", "-l"], 10)
        out, entries, cur = (r.stdout + r.stderr).splitlines(), [], None
        for line in out:
            if not line.strip():
                continue
            if line.startswith(" ") and cur is not None and "usb:" in line:
                cur["usb"] = line.strip().split()[0]
                continue
            parts = line.split(None, 1)
            cur = {"serial": parts[0], "product": parts[1].strip() if len(parts) > 1 else "", "usb": ""}
            m = re.search(r"(usb:\S+)", line)
            if m:
                cur["usb"] = m.group(1)
            entries.append(cur)
        return entries

    def state(self) -> dict[str, str] | None:
        """This DUT's fastboot entry, or None if it is not on the bus."""
        for d in self.devices():
            if d["usb"] == self.usb:
                return d
        return None

    def cmd(self, *args: str, timeout: float = 120) -> str:
        r = _run([self.binary, "-s", self.usb, *args], timeout)
        out = (r.stdout + r.stderr).strip()   # fastboot reports on stderr
        if r.returncode != 0 or "FAILED" in out:
            raise HardwareError(f"fastboot {' '.join(args)} failed: {out[-300:]}",
                                hint="check `boardctrl dut status` (is the board in the expected stage?)")
        return out

    def getvar(self, name: str) -> str:
        out = self.cmd("getvar", name, timeout=15)
        m = re.search(rf"^{re.escape(name)}:\s*(.*)$", out, re.M)
        return m.group(1).strip() if m else out


# serial-mux's log lines start with the time they were read
_STAMP = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]")





class FastbootDownload(DownloadPort):
    """The `download` role over the host's fastboot: stage a file into RAM, flash a partition, report the port."""

    provider_name = "fastboot"

    def __init__(self, spec, config=None, binary: str = "fastboot"):
        super().__init__(spec, config)
        self.fb = Fastboot(str(self.config.get("usb") or getattr(spec, "usb", "") or ""), binary)

    @property
    def usb(self) -> str:
        return self.fb.usb

    @property
    def binary(self) -> str:
        return self.fb.binary

    def state(self):
        return self.fb.state()

    # Where Linux lists the bus's devices: a device's `devnum` there is new each time it enumerates.
    USB_DEVICES = Path("/sys/bus/usb/devices")

    def enumeration(self) -> str | None:
        port = self.fb.usb[4:] if self.fb.usb.startswith("usb:") else ""
        if not port:
            return None
        try:
            return (self.USB_DEVICES / port / "devnum").read_text().strip() or None
        except OSError:
            return None

    def cmd(self, *args: str, timeout: float = 120) -> str:
        return self.fb.cmd(*args, timeout=timeout)

    def getvar(self, name: str) -> str:
        return self.fb.getvar(name)
