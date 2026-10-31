"""propd HTTP front-end (handover §4.1, §8, §9).

Two kinds of auth on one port, no accounts:
  - management API (/v1/*): `Authorization: Bearer <instance.token>`
  - share pages (/s/<token>/*): the share's own token, checked on every request incl. media and Range requests
"""
from __future__ import annotations

import argparse
import hmac
from importlib import resources
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import __version__
from ..core import bootstrap
from ..core.writer import Writer
from ..sdk.errors import ProproomError
from .registry import REGISTRY, OpContext

app = FastAPI(title="Proproom", version=__version__, docs_url=None, redoc_url=None)
_state: dict[str, Any] = {}


@app.on_event("startup")
def _startup() -> None:
    booted = bootstrap.boot()
    _state["booted"] = booted
    _state["token"] = bootstrap.read_token(booted.home)
    _state["writer"] = Writer(booted.home.db)
    # TODO(stage 2): recover planned/running operations from journal, start job runner and event delivery.


@app.on_event("shutdown")
def _shutdown() -> None:
    if w := _state.get("writer"):
        w.close()


def _auth(authorization: str = Header(default="")) -> None:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), _state["token"]):
        raise HTTPException(401, detail={"code": "unauthorized", "message": "instance token required",
                                         "hint": "send Authorization: Bearer <instance.token>",
                                         "retryable": False})


@app.exception_handler(ProproomError)
def _proproom_error(_: Request, e: ProproomError) -> JSONResponse:
    status = {"not_implemented": 501, "unknown_operation": 404, "bad_argument": 400}.get(e.code, 400)
    return JSONResponse({"error": e.to_dict()}, status_code=status)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    b = _state["booted"]
    return {"ok": True, "version": __version__, "policy_version": b.policy_version}


@app.get("/v1/capabilities", dependencies=[Depends(_auth)])
def capabilities() -> dict[str, Any]:
    return REGISTRY["capabilities"](OpContext(_state["booted"], actor="http"), {})


@app.post("/v1/ops/{name}", dependencies=[Depends(_auth)])
async def call_op(name: str, request: Request) -> dict[str, Any]:
    op = REGISTRY.get(name)
    if op is None:
        raise ProproomError("unknown_operation", f"no operation named {name!r}")
    body = await request.json() if await request.body() else {}
    return op(OpContext(_state["booted"], actor="http"), body)


@app.get("/s/{token}", response_class=HTMLResponse)
def share_page(token: str) -> str:
    # TODO(stage 4): sha256(token) -> share row, check expiry/revocation, render items.
    return resources.files("proproom").joinpath("api", "share", "index.html").read_text(encoding="utf-8")


def serve(argv: list[str]) -> None:
    import uvicorn  # noqa: PLC0415

    p = argparse.ArgumentParser(prog="prop serve")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    ns = p.parse_args(argv)
    uvicorn.run(app, host=ns.host, port=ns.port, log_level="info", proxy_headers=False)
