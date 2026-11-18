"""`fastboot` module: the `download` role over the host's fastboot CLI.

`fastboot` stages files into RAM and flashes partitions on the board's USB download port. `none` (the kernel's own,
`core/dut/defaults.py`) is a board with no download port; this provider does not register over it. A site provider
may register another downloader for a vendor's own tool.
"""

from __future__ import annotations

from typing import Any

TITLE = "Fastboot"


def setup(ctx: Any, api: Any, config: dict[str, Any]) -> None:
    from providers.fastboot.cli import FastbootDownload

    def download(_ctx: Any, spec: Any, services: Any) -> Any:
        role = (spec.raw.get("services") or {}).get("download") or {}
        binary = config.get("fastboot") or config.get("binary", "fastboot")
        return FastbootDownload(spec, {"usb": role.get("usb") or getattr(spec, "usb", ""), **role}, binary)

    api.provider("download", "fastboot", download, title="the host's fastboot",
                 about="The board's USB download port, staged and flashed with the fastboot on this host")
