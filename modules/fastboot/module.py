"""`fastboot` module: the `download` role over the host's fastboot CLI.

`fastboot` stages files into RAM and flashes partitions on the board's USB download port; `none` says a board has
no download port, so it reports that it is not on the bus rather than pretending to be reachable. A site module may
register another provider for a vendor's own downloader.
"""

from __future__ import annotations

from typing import Any

TITLE = "Fastboot"


def setup(ctx: Any, api: Any, config: dict[str, Any]) -> None:
    from modules.fastboot.cli import FastbootDownload

    def download(_ctx: Any, spec: Any, services: Any) -> Any:
        role = (spec.raw.get("services") or {}).get("download") or {}
        binary = config.get("fastboot") or config.get("binary", "fastboot")
        return FastbootDownload(spec, {"usb": role.get("usb") or getattr(spec, "usb", ""), **role}, binary)

    api.provider("download", "fastboot", download, title="the host's fastboot",
                 about="The board's USB download port, staged and flashed with the fastboot on this host")
    api.provider("download", "none", download, title="no download port",
                 about="The board has no download port: it is reported as not on the bus")
