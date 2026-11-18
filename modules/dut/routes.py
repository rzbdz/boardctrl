"""Routes of the dut module beyond its commands: the artifact cache (file bodies are not JSON).

No ``from __future__ import annotations`` here: FastAPI resolves the endpoint annotations at runtime.
"""

from pathlib import Path

from fastapi import APIRouter, Request


def artifact_router(mod) -> APIRouter:
    r = APIRouter(tags=["dut"])

    @r.put("/api/v1/artifacts/{sha256}", summary="Upload a file into the host's artifact cache (body = the file)")
    async def put_artifact(sha256: str, request: Request, name: str = "file"):
        up = mod.store.upload(sha256, name)
        try:
            async for chunk in request.stream():
                up.write(chunk)
            res = up.commit()
        finally:
            up.abort()
        if res["stored"]:
            mod._event("artifact", sha256=sha256, file=Path(res["path"]).name, size=res["size"])
        return {"ok": True, "result": res}

    @r.get("/api/v1/artifacts/{sha256}", summary="Whether a file with this sha256 is in the artifact cache")
    async def get_artifact(sha256: str):
        return {"ok": True, "result": mod.artifact_has(sha256)}

    @r.post("/api/v1/artifacts/ticket", summary="A path on this host to scp a file to, before adopting it")
    async def ticket_artifact(sha256: str, name: str = "file"):
        return {"ok": True, "result": mod.store.ticket(sha256, name)}

    @r.post("/api/v1/artifacts/adopt", summary="Check a file copied to a ticket path and put it in the cache")
    async def adopt_artifact(ticket: str, sha256: str, name: str = "file"):
        res = mod.store.adopt_ticket(ticket, sha256, name)
        if res["stored"]:
            mod._event("artifact", sha256=sha256, file=Path(res["path"]).name, size=res["size"])
        return {"ok": True, "result": res}

    return r
