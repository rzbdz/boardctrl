"""gpio module: header GPIOs as raw GPIO / relay / button channels.

It publishes the host's `gpio` service, which the DUT module drives a board's reset and FDL lines through, and
offers the `gpio-relay` relay provider (the alias `gpio_relay` is the module's name before GPIO became a service).
"""

from __future__ import annotations

from typing import Any

from core.context import HostContext
from providers.gpio.backends import create_backend
from providers.gpio.board import BoardCatalog
from providers.gpio.commands import build_commands
from providers.gpio.pinmux import PinmuxInspector
from providers.gpio.service import ConfigStore, GpioService

TITLE = "GPIO"


def setup(ctx: HostContext, api: Any, config: dict[str, Any]) -> None:
    from core.site import layered

    board = BoardCatalog(layered("boards", (ctx.config_dir / config.get("site", "..")).resolve())).get(config["board"])
    backend = create_backend(
        config.get("backend", "sysfs"), config.get("backend_options"), timeout=float(config.get("hw_timeout_s", 3.0))
    )
    debugfs = board.pinctrl.get("debugfs") if config.get("pinmux", True) else None
    service = GpioService(
        board,
        backend,
        ctx.bus,
        baseline=config.get("channels") or {},
        aliases=config.get("aliases") or {},
        store=ConfigStore(ctx.module_state_dir("gpio") / "channels.json"),
        alias_path=ctx.module_state_dir("gpio") / "aliases.json",
        protected=config.get("protected") or [],
        pinmux=PinmuxInspector(debugfs),
        max_pulse_ms=int(config.get("max_pulse_ms", 60_000)),
        claims_path=ctx.module_state_dir("gpio") / "claims.json",
    )
    api.service("gpio", service)
    api.add_commands(build_commands(service))
    api.on_startup(service.startup)
    api.on_shutdown(service.shutdown)
    api.status(lambda: {"board": service.board.id, "backend": service.backend.info(),
                        "pinmux": service.pinmux.summary()})

    from providers.gpio.relay import GpioRelayControl

    api.provider("relay", "gpio-relay", _relay(GpioRelayControl), title="a GPIO relay line",
                 about="The host's gpio service: a header pin switched by this host")
    api.provider("relay", "gpio_relay", _relay(GpioRelayControl))     # the name records used before the role had one
    return service


def _relay(cls):
    def make(ctx: Any, spec: Any, services: Any) -> Any:
        return cls(spec, services)
    return make
