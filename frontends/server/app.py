"""FastAPI application factory.

Every module command is exposed four ways from one CommandSpec:
  REST     POST /api/v1/<group>/<target>/<verb>   (GET too for read-only commands)
  AI tool  GET /api/v1/tools  +  POST /api/v1/tools/<tool_name>
  CLI      `boardctrl <group> <verb> ...` (thin client builds argparse from /api/v1/tools)
  console  POST /api/v1/cli {"line": "gpio set 33 low"} (web remote CLI)
"""

from __future__ import annotations

import difflib
import hashlib
import importlib
import inspect
import json
import re
import socket
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Body, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, create_model

from frontends import __version__
from core.cmdspec import CommandSpec, Param
from core.commands import Command, CommandRegistry
from core.errors import Forbidden, BspError
from core.events import EventBus
from core.jobs import JobManager
from core.context import HostContext
from core.loader import Providers
from frontends.server.config import HostConfig

WEB_DIR = Path(__file__).parent / "web"
_PYTYPES = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list[str] | str}


class CliLine(BaseModel):
    line: str = Field(..., examples=["gpio set 33 low"])


class Hub:
    """Runtime container: config, bus, the DUT manager, the loaded providers and the command registry."""

    def __init__(self, config: HostConfig):
        self.config = config
        self.bus = EventBus()
        self.started = time.time()
        # Set when the service is told to stop: streams and long polls end at once, so the process stops within
        # its stop timeout instead of waiting on clients (see frontends/server/__main__.py).
        self.stopping = threading.Event()
        config.state_dir.mkdir(parents=True, exist_ok=True)
        # Job records outlive the process: after a restart, a job it was running reads `interrupted`.
        self.jobs = JobManager(self.bus, record_dir=config.state_dir / "jobs")
        ctx = HostContext(config.host_id, self.bus, config.state_dir, config.config_dir, jobs=self.jobs,
                          port=config.port, auth_token=config.auth_token, env=dict(config.env),
                          bind=config.bind, announce=config.announce)
        self.ctx = ctx
        # Sessions (console, trace, debug servers) are transient units: the DUT manager starts them under a lease.
        from core.sessions import Sessions, Systemd

        self.sessions = Sessions(config.state_dir / "sessions", Systemd(),
                                 held=self._lease_held)
        ctx.services["sessions"] = self.sessions
        # The DUT manager is the kernel's own: a host controls boards, always. It builds its boards when the host
        # starts, which is after every provider's setup, so a record may bind any provider a host carries.
        from core.dut.manager import DutManager

        self.dut = DutManager(ctx, config.providers.get("dut") or {})
        ctx.services["dut_manager"] = self.dut
        # The SDK catalogue and the host registry are the kernel's services too (their commands and HTTP routes);
        # their sections of the host config configure them.
        from core.hostregistry.service import setup as registry_setup
        from core.sdk.service import setup as sdk_setup
        # One loader, one contract: the providers this repository ships (`providers/<name>/provider.py`) and a
        # site's own (`<site>/providers/<name>/provider.py`), the site's copy of a name replacing the shipped one.
        # A provider the host names is set up with its section of that map; `enabled: false` turns it off. Every
        # setup runs before any startup, so a provider may read another's service when it starts, in any order.
        from core.site import REPO, layers

        self.follower = None
        site = (config.config_dir / "..").resolve()
        self.providers = Providers([REPO / "providers"]
                                   + [root / "providers" for root in layers(site)]
                                   + [site / "modules"])
        providers_cfg = _with_role_providers(config.providers)
        self.providers.load(ctx, providers_cfg)
        if "sdk" in providers_cfg:
            self.providers.add("sdk", sdk_setup, ctx, providers_cfg["sdk"], title="SDK catalogue")
        if "registry" in providers_cfg:
            self.providers.add("registry", registry_setup, ctx, providers_cfg["registry"], title="Host registry")
        # A host announcing itself to a registry follows the registry's SDK catalog.
        if config.announce:
            from frontends.server.catalog_follow import CatalogFollower

            from core.site import layered

            site = (config.config_dir / "..").resolve()
            import os

            self.follower = CatalogFollower(config.announce["url"], config.state_dir / "catalog", self.bus,
                                            layered("sdk", site),
                                            config.announce.get("token") or os.environ.get("BSP_REGISTRY_TOKEN"))
        if self.follower:
            ctx.services["catalog_follower"] = self.follower
        self.registry = CommandRegistry()
        self.registry.extend(self._core_commands())
        self.registry.extend(self.dut.commands())
        self.registry.extend(self.providers.commands())

    @property
    def modules(self) -> "_Managers":
        """The managers by name — the DUT's and every provider's — for a helper or a test that reaches one."""
        return _Managers(self)

    def module(self, name: str) -> Any:
        """A manager by name — the DUT's, or a provider's — for a test or a helper that drives it directly."""
        if name == "dut":
            return self.dut
        loaded = self.providers.loaded.get(name)
        return getattr(loaded, "manager", None) if loaded else None

    announcer: Any = None

    def announcement(self) -> dict[str, Any]:
        dut = self.dut
        return {"host": self.config.host_id, "port": self.config.port, "description": self.config.description,
                "version": __version__, "duts": sorted(getattr(dut, "boards", {}) or {}),
                # compact, so a registry notices a slot coming online or a board registered into it
                "slots": [f"{s['id']}:{'on' if s['online'] else 'off'}:{s.get('dut') or ''}" for s in dut.slot_list()]
                if dut and hasattr(dut, "slot_list") else [],
                "ports": dut.ports() if dut else {}, "catalog_rev": self.follower.rev if self.follower else 0,
                "files_digest": self.follower.files_digest if self.follower else "",
                "catalog_shadowed": self.follower.shadowed if self.follower else []}

    def _lease_held(self, token: str) -> bool:
        """Whether a lease token is the live one on its board: asked of the DUT module, which owns the leases (and
        is built after this, so it is looked up when asked, not held)."""
        return self.dut.lease_live(token)

    def status(self) -> dict[str, Any]:
        return {
            "host": self.config.host_id,
            "description": self.config.description,
            "hostname": socket.gethostname(),
            "version": __version__,
            "uptime_s": round(time.time() - self.started, 1),
            "auth": bool(self.config.auth_token),
            # What this host runs, by name: the kernel's DUT manager and every provider it loaded. The key is
            # `modules` because that is what the page, `boardctrl hosts` and the fleet view have read since before
            # providers were called providers; it is not a directory of modules.
            "modules": {"dut": {"title": self.dut.title, **self.dut.health()}} | self.providers.view(),
            "providers": _providers_summary(),
            "sessions": [s.to_dict() for s in self.sessions.list()],
            **({"announce": {"url": self.announcer.url, **self.announcer.last}} if self.announcer else {}),
            **({"catalog": {"rev": self.follower.rev, **self.follower.last, "shadowed": self.follower.shadowed}}
               if self.follower else {}),
        }

    def _core_commands(self) -> list[Command]:
        def render_status(s: dict[str, Any]) -> str:
            lines = [f"{s['host']} ({s['hostname']}) boardctrl {s['version']} up {s['uptime_s']}s"]
            for n, m in s["modules"].items():
                be = m.get("backend") or {}
                lines.append(f"  module {n}: {m.get('title')} board={m.get('board')} backend={be.get('name')}"
                             f" degraded={be.get('degraded')} pinmux={(m.get('pinmux') or {}).get('state')}")
            return "\n".join(lines)

        def render_events(evs: list[dict[str, Any]]) -> str:
            return "\n".join(
                f"#{e['seq']} {time.strftime('%H:%M:%S', time.localtime(e['ts']))} {e['topic']} "
                + " ".join(f"{k}={v}" for k, v in e["data"].items() if k not in ("error",) and v is not None)
                + (f" error={e['data']['error'].get('message')}" if e["data"].get("error") else "")
                for e in evs
            ) or "(no events)"

        follow = []
        if self.follower:
            def shown(r: dict[str, Any]) -> str:
                text = f"catalog revision {r.get('rev')}" + ("" if r.get("ok") else f" (registry unreachable: {r.get('error')})")
                if r.get("load_error"):
                    text += f"\nNOT IN EFFECT: the catalog does not load, the last one that did is used: {r['load_error']}"
                for x in r.get("shadowed") or []:
                    text += f"\nNOT IN EFFECT: {x['file']} differs from the registry's {x['ref']}; publish it: boardctrl catalog put {x['ref']} -f <file>"
                return text

            def sync(reset: bool) -> dict[str, Any]:
                # The modules reload on `catalog.updated` while the sync publishes it: say what they now use.
                r = dict(self.follower.sync(reset))
                r["in_effect"] = self.dut.loaded_rev
                r["load_error"] = self.dut._catalog_error
                return r

            follow = [Command(CommandSpec("catalog.sync", "Fetch the registry's SDK catalog now (the registry asks after changes)",
                                          [Param("reset", "boolean", "throw the local copy away and fetch it again",
                                                 required=False, default=False)]),
                              sync, shown)]
        return follow + [
            Command(CommandSpec("status", "Service, module and backend health", [], mutating=False),
                    self.status, render_status),
            Command(CommandSpec("job.get", "Status, progress lines and result of a background job (boot, flash)",
                                [Param("id", help="job id"),
                                 Param("since", "integer", "only lines after this index", required=False, default=0),
                                 Param("wait", "number", "block up to N seconds while it runs", required=False, default=0)],
                                mutating=False),
                    lambda id, since, wait: (self.jobs.wait(id, wait, self.stopping) if wait
                                             else self.jobs.get(id)).view(since),
                    _render_job),
            Command(CommandSpec("job.cancel", "Stop a running job (boot, flash, probe, rebuild): the program it runs is "
                                              "stopped with what it started, a wait ends at once, and the job ends "
                                              "`cancelled` saying where it stopped",
                                [Param("id", help="job id"),
                                 Param("lease", help="the board's lease token, for a job a board runs (the CLI "
                                                     "fills it in / $BSP_LEASE)", required=False, positional=False),
                                 Param("reason", help="why, kept with the job", required=False, positional=False)]),
                    lambda id, lease, reason: self.jobs.cancel(id, lease, reason),
                    lambda r: f"job {r['id']}: " + ("cancelling; it ends `cancelled`" if r["cancelled"]
                                                    else r.get("message") or r["status"])),
            Command(CommandSpec("job.list", "Recent background jobs", [], mutating=False),
                    lambda: [{k: v for k, v in j.view().items() if k != "lines"} for j in self.jobs.list()],
                    lambda js: "\n".join(f"{j['id']:<28} {j['status']:<10} "
                                         f"{str(j['progress']) + '%' if j.get('progress') is not None else '':>4} "
                                         f"{j['elapsed_s']:>7}s  {j['title']}" for j in js) or "(no jobs)"),
            Command(CommandSpec("events.recent", "Recent events (status monitor history)",
                                [Param("limit", "integer", "max events", required=False, default=30)], mutating=False),
                    lambda limit: [e.to_dict() for e in self.bus.recent(limit)], render_events),
        ]


class _Managers:
    """The managers a host has, by name: the DUT manager and what each provider set up (a read-only view)."""

    def __init__(self, hub: "Hub"):
        self._hub = hub

    def _all(self) -> dict[str, Any]:
        return {"dut": self._hub.dut} | {n: m.manager for n, m in self._hub.providers.loaded.items()
                                         if m.manager is not None}

    def __getitem__(self, name: str) -> Any:
        try:
            return self._all()[name]
        except KeyError:
            raise KeyError(name) from None

    def __contains__(self, name: str) -> bool:
        return name in self._all()

    def __iter__(self):
        return iter(self._all())

    def keys(self):
        return self._all().keys()

    def values(self):
        return self._all().values()

    def items(self):
        return self._all().items()


# The providers every host of boards carries: the tools a record's roles are filled by unless it names another.
# A host's config lays its own settings per provider; the DUT manager's old `modules.dut.{smtty,serial_mux,
# serial_log_dir,fastboot}` are read as each one's fallback, so a site written before the tools had providers of
# their own keeps working.
ROLE_PROVIDERS = {"smtty": ("smtty", "serial_mux", "serial_log_dir"),
                  "fastboot": ("fastboot",),
                  "human": ()}
# Sections a host file may still call a provider by its old name: `gpio_relay:` was the service before GPIO had a role,
# `serial` the console's; each is the same settings under the name that loads it now.
PROVIDER_ALIASES = {"gpio_relay": "gpio", "serial": "smtty"}


def _with_role_providers(config: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """A host's provider sections, with the boards' own providers added and the DUT's old keys folded in."""
    dut = config.get("dut") or {}
    out = dict(config)
    for old, name in PROVIDER_ALIASES.items():
        if old in out:
            section = out.pop(old)
            # An explicit `gpio:` is the same settings named the current way; it wins where both are written.
            out.setdefault(name, section if isinstance(section, dict) else {})
    for name, old_keys in ROLE_PROVIDERS.items():
        section = dict(out.get(name) or {})
        for key in old_keys:
            if key in dut and key not in section:
                section[key] = dut[key]
        out.setdefault(name, section)
    return out


def _providers_summary() -> dict[str, Any]:
    """What providers this host has, by role (the built-ins are registered when the host's modules import)."""
    from core.providers import describe

    return describe()


def _render_job(j: dict[str, Any]) -> str:
    pct = f" {j['progress']}%" if j.get("progress") is not None else ""
    head = f"job {j['id']} [{j['status']}{pct}] {j['title']} ({j['elapsed_s']}s)"
    body = "\n".join(j["lines"])
    tail = ""
    if j.get("error"):
        tail = f"\nerror[{j['error'].get('code')}]: {j['error'].get('message')}" + (
            f"\nhint: {j['error']['hint']}" if j["error"].get("hint") else "")
    return "\n".join(x for x in (head, body) if x) + tail


# ====================================================================== app --
def create_app(config: HostConfig) -> FastAPI:
    hub = Hub(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await run_in_threadpool(hub.dut.startup)
        await run_in_threadpool(hub.providers.startup)
        hub.bus.publish("service.started", version=__version__)
        announcer = None
        if config.announce:
            from frontends.server.announce import Announcer

            announcer = Announcer(config.announce, hub.announcement)
            hub.announcer = announcer
            if hub.follower:
                announcer.on_reply = lambda result: hub.follower.check(result.get("catalog_rev"), result.get("files_digest"))
                # tell the registry at once which catalog revision this host now has
                hub.bus.on("catalog.updated", lambda ev: announcer.poke())
                await run_in_threadpool(hub.follower.sync)
            hub.bus.on("dut.ports", lambda ev: announcer.poke())     # tell the registry about new cables at once
            # ... and about boards and slots, so front ends (boardlab) list a board the moment it is registered
            for topic in ("dut.added", "dut.removed", "dut.renamed", "dut.updated", "dut.refreshed", "dut.slots"):
                hub.bus.on(topic, lambda ev: announcer.poke())
            announcer.start()
        yield
        hub.stopping.set()
        if announcer:
            announcer.stop()
        hub.providers.shutdown()
        hub.dut.shutdown()

    app = FastAPI(
        title=f"boardctrld · {config.host_id}",
        version=__version__,
        description="boardctrl dev-loop host controller. AI agents: start at GET /llms.txt or GET /api/v1/tools.",
        lifespan=lifespan,
        root_path_in_servers=False,  # a proxy's prefix stays out of the schema boardctrld serves to everyone
    )
    app.state.hub = hub
    app.add_middleware(_ForwardedPrefix)

    # ---- every error is a self-explaining envelope: what failed, why, and what to do next
    def envelope(request: Request, status: int, err: dict[str, Any]) -> JSONResponse:
        err.setdefault("request", f"{request.method} {request.url.path}")
        err.setdefault("docs", "/llms.txt")
        return JSONResponse({"ok": False, "error": err}, status_code=status)

    @app.exception_handler(BspError)
    async def _bsp_error(request: Request, exc: BspError):
        return envelope(request, exc.status, exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        problems = [f"{'.'.join(str(x) for x in e.get('loc', []))}: {e.get('msg')}" for e in exc.errors()]
        return envelope(request, 400, {
            "code": "invalid_request", "message": "; ".join(problems),
            "hint": "send arguments as a JSON object body (Content-Type: application/json) or as query parameters; "
                    "schemas: GET /api/v1/tools",
            "details": exc.errors(),
        })

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            templates = sorted({r.path for r in app.routes if getattr(r, "path", "").startswith("/api/")})
            close = difflib.get_close_matches(request.url.path, templates, n=5, cutoff=0.5)
            return envelope(request, 404, {
                "code": "not_found", "message": f"no route {request.url.path}",
                "hint": (f"similar routes: {', '.join(close)}" if close else "routes follow /api/v1/<group>/<target>/<verb>")
                        + "; full list: GET /api/v1/tools or /docs",
                "details": {"did_you_mean": close},
            })
        if exc.status_code == 405:
            allowed = exc.headers.get("allow", "") if exc.headers else ""
            return envelope(request, 405, {
                "code": "method_not_allowed", "message": f"{request.method} not allowed on {request.url.path}",
                "hint": f"use {allowed or 'POST'} (mutating commands are POST-only; read-only commands accept GET and POST)",
            })
        return envelope(request, exc.status_code, {"code": f"http_{exc.status_code}", "message": str(exc.detail)})

    if config.auth_token:
        open_paths = tuple(hub.providers.open_paths())

        @app.middleware("http")
        async def _auth(request: Request, call_next):
            path = request.scope["path"]  # without a proxy's prefix
            if path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS") \
                    and not path.startswith(open_paths or ("\0",)):
                if _token(request) != config.auth_token:
                    e = Forbidden("missing or wrong token", hint="send 'Authorization: Bearer <token>' or 'X-BSP-Token: <token>'; "
                                  "the token is server.auth_token in the host config (or env BSP_TOKEN)")
                    return envelope(request, e.status, e.to_dict())
            return await call_next(request)

    _core_routes(app, hub)
    for spec in hub.registry.specs():
        _command_routes(app, hub, spec)
    from frontends.server.routes.artifacts import artifact_router
    from frontends.server.routes.registry import registry_router

    app.include_router(artifact_router(hub.dut))
    registry = hub.module("registry")
    if registry is not None:
        app.include_router(registry_router(registry))
    for r in hub.providers.routers():
        app.include_router(r)
    from frontends.server.streams import include_session_routes

    include_session_routes(app, lambda: app.state.hub.sessions, hub.stopping)

    # Served by a route of its own: a mounted StaticFiles takes the proxy's `root_path` off the path again and would
    # look the file up in the wrong place under a prefix.
    @app.get("/static/{name:path}", include_in_schema=False)
    async def static_file(name: str):
        target = (WEB_DIR / name).resolve()
        if WEB_DIR.resolve() not in target.parents or not target.is_file():
            raise StarletteHTTPException(404)
        return FileResponse(target, headers={"Cache-Control": "no-cache"})

    # cache-busting: asset URLs carry a hash of the web files, so a redeploy is picked up without Ctrl+F5
    web_version = hashlib.sha1(b"".join(f.read_bytes() for f in sorted(WEB_DIR.iterdir()) if f.is_file())).hexdigest()[:10]
    index_html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__V__", web_version)

    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        # A page requested with the token comes through a proxy that adds it to every request: it needs none of its own.
        authed = bool(config.auth_token) and _token(request) == config.auth_token
        return HTMLResponse(index_html.replace("__AUTHED__", "1" if authed else ""), headers={"Cache-Control": "no-cache"})

    return app


def _core_routes(app: FastAPI, hub: Hub) -> None:
    @app.get("/api/v1/health", tags=["core"])
    async def health():
        return {"ok": True, "version": __version__}

    @app.get("/api/v1/tools", tags=["core"], summary="Command catalogue / AI tool definitions")
    async def tools(format: Literal["bsp", "anthropic", "openai"] = "bsp"):
        specs = hub.registry.specs()
        if format == "anthropic":
            return [{"name": s.tool_name, "description": _tool_desc(s), "input_schema": s.input_schema()} for s in specs]
        if format == "openai":
            return [{"type": "function", "function": {"name": s.tool_name, "description": _tool_desc(s),
                                                      "parameters": s.input_schema()}} for s in specs]
        return {"ok": True, "tools": [s.to_dict() for s in specs]}

    @app.post("/api/v1/tools/{name}", tags=["core"], summary="Execute a tool / command by name with JSON args")
    async def call_tool(name: str, args: dict[str, Any] = Body(default_factory=dict)):
        result, text = await run_in_threadpool(hub.registry.execute, name, args)
        return {"ok": True, "command": hub.registry.get(name).spec.name, "result": result, "text": text}

    @app.post("/api/v1/cli", tags=["core"], summary="Remote CLI: run one `boardctrl ...` command line")
    async def cli(body: CliLine):
        code, output, result = await run_in_threadpool(hub.registry.run_line, body.line)
        return {"ok": code == 0, "exit_code": code, "output": output, "result": result}

    @app.get("/api/v1/events", tags=["core"], summary="Server-sent event stream (status monitor)")
    async def events(request: Request, since: Optional[int] = None):
        async def gen():
            yield "retry: 2000\n\n"
            with hub.bus.subscribe(since) as sub:
                idle = 0.0
                while not hub.stopping.is_set() and not await request.is_disconnected():
                    ev = await sub.get(timeout=0.5)
                    if ev is None:
                        idle += 0.5
                        if idle >= 15:
                            idle = 0.0
                            yield ": ping\n\n"
                        continue
                    idle = 0.0
                    # no `event:` field: topic is in the payload, so EventSource.onmessage sees everything
                    yield f"id: {ev.seq}\ndata: {json.dumps(ev.to_dict(), default=str)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/llms.txt", include_in_schema=False, response_class=PlainTextResponse)
    async def llms(request: Request):
        return _llms_txt(hub, str(request.base_url).rstrip("/"))


def _token(request: Request) -> str:
    return request.headers.get("x-bsp-token") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()


class _ForwardedPrefix:
    """Served under a path prefix by a reverse proxy (`X-Forwarded-Prefix: /some/prefix`): the links boardctrld writes out
    (/docs, llms.txt) carry the prefix. The page itself uses relative URLs and needs nothing."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            prefix = dict(scope["headers"]).get(b"x-forwarded-prefix", b"").decode("latin-1").rstrip("/")
            if re.fullmatch(r"(/[A-Za-z0-9._~%:@+-]+)+", prefix):
                # Routes match on the path without the prefix; `root_path` makes the links boardctrld writes (/docs,
                # llms.txt) carry it.
                path = scope.get("path", "")
                if path == prefix or path.startswith(prefix + "/"):
                    path = path[len(prefix):] or "/"
                scope = {**scope, "root_path": scope.get("root_path", "") + prefix, "path": path, "raw_path": path.encode()}
        await self.app(scope, receive, send)


def _tool_desc(s: CommandSpec) -> str:
    return f"{s.summary}. {s.description}".strip() if s.description else s.summary


def _command_routes(app: FastAPI, hub: Hub, spec: CommandSpec) -> None:
    body_params = [p for p in spec.params if p.name != "target"]
    model = None
    if body_params:
        fields = {}
        for p in body_params:
            t = Literal[tuple(p.choices)] if p.choices else _PYTYPES[p.type]  # type: ignore[valid-type]
            fields[p.name] = (Optional[t], Field(default=None, description=p.help or None))
        model = create_model(f"{spec.tool_name}_args", **fields)

    def make(with_body: bool):
        async def endpoint(request: Request, **kw):
            args: dict[str, Any] = dict(request.query_params)
            if kw.get("body") is not None:
                args.update(kw["body"].model_dump(exclude_none=True))
            if "target" in kw:
                args["target"] = kw["target"]
            result, _ = await run_in_threadpool(hub.registry.execute, spec.name, args)
            return {"ok": True, "command": spec.name, "result": result}

        params = [inspect.Parameter("request", inspect.Parameter.KEYWORD_ONLY, annotation=Request)]
        if spec.has_target:
            params.append(inspect.Parameter("target", inspect.Parameter.KEYWORD_ONLY, annotation=str))
        if with_body and model is not None:
            params.append(inspect.Parameter("body", inspect.Parameter.KEYWORD_ONLY,
                                            annotation=Optional[model], default=Body(None)))
        endpoint.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
        endpoint.__name__ = spec.tool_name
        return endpoint

    doc = f"{spec.description}\n\nCLI: `{spec.cli_usage}`. Args may be sent as JSON body or query string."
    tag = spec.group
    path = spec.http_path.replace("{target}", "{target:path}")  # aliases like DUT3/reset contain '/'
    if not spec.mutating:
        app.add_api_route(path, make(False), methods=["GET"], tags=[tag], summary=spec.summary,
                          description=doc, operation_id=f"{spec.tool_name}_get")
    app.add_api_route(path, make(True), methods=["POST"], tags=[tag], summary=spec.summary,
                      description=doc, operation_id=spec.tool_name)


def _llms_txt(hub: Hub, base: str) -> str:
    lines = [
        f"# boardctrld · {hub.config.host_id}",
        "",
        f"> {hub.config.description or 'boardctrl dev-loop host controller'}. HTTP API, CLI and AI tools share one command set.",
        "",
        "## How to call",
        f"- Tool catalogue (JSON schema): GET {base}/api/v1/tools  (?format=anthropic|openai)",
        f"- Execute a tool: POST {base}/api/v1/tools/<tool_name>  body = JSON args",
        f"- REST: POST {base}/api/v1/<group>/<target>/<verb>  e.g. curl -X POST '{base}/api/v1/gpio/P33/set?level=low'",
        f"- Remote CLI line: POST {base}/api/v1/cli  {{\"line\": \"gpio list\"}}",
        f"- Live events (SSE): GET {base}/api/v1/events ; history: GET {base}/api/v1/events/recent",
        f"- OpenAPI: {base}/openapi.json ; docs UI: {base}/docs",
        "- Responses: {ok: true, result: ...} or {ok: false, error: {code, message, hint}}",
        "- Pin targets: 33 | P33 | J40:33 (physical header pin) | GPIO127 (Linux gpio number) | alias such as DUT3/reset",
        "- Aliases: downstream layers bind meaning (gpio_alias_add name=DUT3/reset target=P11) and then call verbs on the alias,",
        "  e.g. POST /api/v1/button/DUT3/reset/click. Delete a DUT's whole tree with gpio_alias_delete name=DUT3 prefix=true.",
        "- Errors always carry code, message, hint (next step) and details (e.g. did_you_mean, valid actions); read the hint first.",
        "",
        "## Tools",
    ]
    for s in hub.registry.specs():
        lines.append(f"- {s.tool_name}: {s.summary}. CLI `{s.cli_usage}`; HTTP {'POST' if s.mutating else 'GET'} {s.http_path}")
    return "\n".join(lines) + "\n"
