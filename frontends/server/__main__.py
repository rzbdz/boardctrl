"""`boardctrld --config <site>/hosts/<host>.yaml` """

from __future__ import annotations

import argparse
import logging
import sys

# How long a stop waits for requests in flight before cancelling them. Streams and long polls end at once when
# the stop begins (`Hub.stopping`), so this is only for ordinary requests; the unit's TimeoutStopSec (20 s, in
# deploy/install.sh) leaves room for it and for the service's own shutdown.
GRACEFUL_STOP_S = 5


def build_server(app, host: str, port: int, log_level: str = "info"):
    """The uvicorn server for `app`: on SIGTERM it ends the app's streams and long polls at once
    (`Hub.stopping`), and cancels other requests after GRACEFUL_STOP_S."""
    import uvicorn

    stopping = app.state.hub.stopping

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame) -> None:
            # uvicorn waits for open connections before it runs the app's shutdown: end the streams now.
            stopping.set()
            super().handle_exit(sig, frame)

    return Server(uvicorn.Config(app, host=host, port=port, log_level=log_level, access_log=False,
                                 timeout_graceful_shutdown=GRACEFUL_STOP_S))


def main(argv: list[str] | None = None) -> None:
    from frontends.server.app import create_app
    from frontends.server.config import HostConfig

    ap = argparse.ArgumentParser(prog="boardctrld", description="board controller daemon (the old name, bsp-hostd, still works)")
    ap.add_argument("--config", "-c", required=True, help="host config YAML")
    ap.add_argument("--bind", help="override server.bind")
    ap.add_argument("--port", type=int, help="override server.port")
    ap.add_argument("--log-level", default="info")
    a = ap.parse_args(argv)

    logging.basicConfig(level=a.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = HostConfig.load(a.config)
    server = build_server(create_app(cfg), a.bind or cfg.bind, a.port or cfg.port, a.log_level)
    server.run()
    if not server.started:
        sys.exit(3)


if __name__ == "__main__":
    main()
