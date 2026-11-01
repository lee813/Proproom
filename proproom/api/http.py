"""propd HTTP front-end (handover §4.1, §8, §9).

Two kinds of auth on one port, no accounts:
  - management API (/v1/*): `Authorization: Bearer <instance.token>`
  - share pages (/s/<token>/*): the share's own token, checked on every request incl. media and Range requests
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import os
import logging
import threading
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from .. import __version__
from ..core import bootstrap, db, hardware, share_download, store
from ..core.ids import utcnow
from ..core.writer import Writer
from ..sdk.errors import ProproomError
from .registry import REGISTRY, OpContext

app = FastAPI(title="Proproom", version=__version__, docs_url=None, redoc_url=None)
_state: dict[str, Any] = {}
EXIT_GPU_LOST = 75  # EX_TEMPFAIL: Docker's restart policy brings the container back with its GPU


def _ctx(actor: str) -> OpContext:
    """Every request and the worker share one plugin manager, so the process holds one copy of each model."""
    return OpContext(_state["booted"], actor=actor, plugin_manager=_state["pm"])


@app.on_event("startup")
def _startup() -> None:
    booted = bootstrap.boot()
    _state["booted"] = booted
    _state["token"] = bootstrap.read_token(booted.home)
    _state["writer"] = Writer(booted.home.db)
    _state["pm"] = OpContext(booted).pm()
    _state["instance_id"] = _state["pm"].instance_id
    stop = threading.Event()
    _state["worker_stop"] = stop
    idle_s = float(booted.config["embedding"]["idle_unload_s"])
    def run_worker() -> None:
        from ..core.worker import tick
        log = logging.getLogger("proproom.worker")
        ctx = _ctx("worker")
        while not stop.is_set():
            try:
                for name in _state["pm"].release_idle(idle_s):
                    log.info("Released %s after %.0f s idle", name, idle_s)
                if not tick(ctx):
                    stop.wait(1.0)
            except ProproomError as e:
                if e.code == "gpu_lost":
                    # Running on without it would move every model to the CPU. Exit; Docker restarts the container.
                    log.critical("%s; exiting so the container restarts", e.message)
                    _state["gpu_lost"] = e.message
                    if server := _state.get("server"):
                        server.should_exit = True
                    return
                log.exception("Worker tick failed")
                stop.wait(1.0)
            except Exception:
                log.exception("Worker tick failed")
                stop.wait(1.0)
    thread = threading.Thread(target=run_worker, name="proproom-worker", daemon=True)
    _state["worker_thread"] = thread
    thread.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    if stop := _state.get("worker_stop"):
        stop.set()
    if thread := _state.get("worker_thread"):
        thread.join(timeout=10)
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
    status = {"not_implemented": 501, "unknown_operation": 404, "bad_argument": 400, "gpu_lost": 503}.get(e.code, 400)
    return JSONResponse({"error": e.to_dict()}, status_code=status)


@app.get("/healthz")
def healthz() -> JSONResponse:
    b = _state["booted"]
    blocked = hardware.gpu_lost()
    body = {"ok": not blocked, "version": __version__, "policy_version": b.policy_version,
            "instance_id": _state["instance_id"]}
    if blocked:
        body["gpu_lost"] = blocked
    return JSONResponse(body, status_code=503 if blocked else 200)


@app.get("/v1/capabilities", dependencies=[Depends(_auth)])
def capabilities() -> dict[str, Any]:
    return REGISTRY["capabilities"](_ctx("http"), {})


@app.post("/v1/ops/{name}", dependencies=[Depends(_auth)])
def call_op(name: str, body: dict[str, Any] | None = Body(default=None),
            x_prop_actor: str = Header(default="http")) -> dict[str, Any]:
    """A plain def: FastAPI runs it in its thread pool, so a long import or index call does not stall the share
    pages and /healthz. `X-Prop-Actor` keeps the caller's name when the CLI or MCP server forwards a call."""
    op = REGISTRY.get(name)
    if op is None:
        raise ProproomError("unknown_operation", f"no operation named {name!r}")
    return op(_ctx(x_prop_actor if x_prop_actor in ("cli", "mcp") else "http"), body or {})


# ───────────────────────── Share endpoints ─────────────────────────

def _get_share(token: str) -> dict[str, Any]:
    b = _state["booted"]
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = db.connect(b.home.db, readonly=True)
    try:
        row = conn.execute(
            "SELECT * FROM share WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if not row:
            raise HTTPException(404, detail="Share link not found")
        if row["revoked_at"]:
            raise HTTPException(410, detail="Share link has been revoked")
        now_iso = utcnow()
        if row["expires"] and row["expires"] < now_iso:
            raise HTTPException(410, detail="Share link has expired")
        return dict(row)
    finally:
        conn.close()


@app.get("/s/{token}", response_class=HTMLResponse)
def share_page(token: str) -> str:
    _get_share(token)
    return resources.files("proproom").joinpath("api", "share", "index.html").read_text(encoding="utf-8")


@app.get("/s/{token}/items")
def share_items(token: str) -> dict[str, Any]:
    share = _get_share(token)
    b = _state["booted"]
    conn = db.connect(b.home.db, readonly=True)
    try:
        rows = conn.execute(
            "SELECT si.pos, si.asset_id, si.representation_id, si.caption, "
            "a.title, a.description, a.kind, rep.blob_sha256, rep.width, rep.height, rep.duration, "
            "p.source_url, re.license_id, re.attribution, "
            "(SELECT verdict FROM share_feedback sf WHERE sf.share_id = si.share_id AND sf.pos = si.pos ORDER BY sf.created DESC LIMIT 1) AS verdict, "
            "(SELECT comment FROM share_feedback sf WHERE sf.share_id = si.share_id AND sf.pos = si.pos ORDER BY sf.created DESC LIMIT 1) AS comment "
            "FROM share_item si "
            "JOIN asset a ON a.id = si.asset_id "
            "JOIN representation rep ON rep.id = si.representation_id "
            # One row per item: the asset's latest provenance and its latest evidence, as `link --plan` credits it.
            "LEFT JOIN provenance p ON p.id = (SELECT id FROM provenance WHERE asset_id = a.id "
            "ORDER BY retrieved DESC LIMIT 1) "
            "LEFT JOIN rights_evidence re ON re.id = (SELECT id FROM rights_evidence WHERE provenance_id = p.id "
            "ORDER BY created DESC LIMIT 1) "
            "WHERE si.share_id = ? "
            "ORDER BY si.pos",
            (share["id"],),
        ).fetchall()

        items = []
        for r in rows:
            items.append({
                "pos": r["pos"],
                "asset_id": r["asset_id"],
                "representation_id": r["representation_id"],
                "title": r["title"],
                "description": r["description"],
                "kind": r["kind"],
                "width": r["width"],
                "height": r["height"],
                "duration": r["duration"],
                "source_url": r["source_url"],
                "license": r["license_id"],
                "artist": r["attribution"],
                "caption": r["caption"],
                "verdict": r["verdict"],
                "comment": r["comment"],
            })

        return {
            "title": share.get("title") or "Shared Selections",
            "note": share.get("note") or "",
            "items": items,
        }
    finally:
        conn.close()


@app.get("/s/{token}/media/{rep_id}")
async def share_media(token: str, rep_id: str, request: Request) -> Response:
    share = _get_share(token)
    try:
        files = share_download.prepare(_state["booted"].home, share["id"], representation_id=rep_id)
    except ProproomError as e:
        if e.code == "share_media_missing":
            raise HTTPException(404, detail=e.message) from e
        raise
    if not files:
        raise HTTPException(404, detail="Media representation not found in this share")
    file = files[0]
    file_path, file_size, mime = file.path, file.size, file.mime
    download = request.query_params.get("download") == "1"

    # Support Range header for video / audio seeking and partial requests.
    range_header = request.headers.get("Range")
    if range_header and range_header.startswith("bytes="):
        raw_range = range_header.replace("bytes=", "").strip()
        if raw_range.startswith("-"):
            suffix_len = int(raw_range[1:])
            start = max(0, file_size - suffix_len)
            end = file_size - 1
        else:
            byte_range = raw_range.split("-")
            start = int(byte_range[0])
            end = int(byte_range[1]) if byte_range[1] else file_size - 1
        start = max(0, start)
        end = min(file_size - 1, end)
        length = max(0, (end - start) + 1)

        def iter_file():
            with file_path.open("rb") as f:
                f.seek(start)
                rem = length
                while rem > 0:
                    data = f.read(min(rem, 64 * 1024))
                    if not data:
                        break
                    rem -= len(data)
                    yield data

        headers = {"Content-Range": f"bytes {start}-{end}/{file_size}", "Accept-Ranges": "bytes",
                   "Content-Length": str(length), "Content-Type": mime}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{file.name}"'
        return StreamingResponse(iter_file(), status_code=206, headers=headers)

    return FileResponse(file_path, media_type=mime, filename=file.name if download else None)


@app.get("/s/{token}/download.zip")
def share_archive(token: str, pos: list[int] | None = Query(default=None)) -> StreamingResponse:
    share = _get_share(token)
    home = _state["booted"].home
    files = share_download.prepare(home, share["id"], positions=pos)
    credits = share_download.credits_text(home, files)
    return StreamingResponse(share_download.stream(files, credits), media_type="application/zip",
                             headers={"Content-Disposition": 'attachment; filename="proproom-share.zip"',
                                      "Cache-Control": "no-store"})


@app.post("/s/{token}/feedback")
async def share_feedback_submit(token: str, request: Request) -> dict[str, Any]:
    share = _get_share(token)
    body = await request.json()
    pos = int(body.get("pos", 0))
    verdict = body.get("verdict", "want")
    comment = body.get("comment", "")

    if verdict not in ("want", "reject", "uncertain", "star"):
        raise HTTPException(400, detail=f"Invalid verdict '{verdict}'")

    b = _state["booted"]
    conn = db.connect(b.home.db)
    try:
        pos_exists = conn.execute(
            "SELECT 1 FROM share_item WHERE share_id = ? AND pos = ?", (share["id"], pos)
        ).fetchone()
        if not pos_exists:
            raise HTTPException(404, detail=f"Item at pos {pos} not found in this share")

        now = utcnow()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO share_feedback(share_id, pos, verdict, comment, created) "
            "VALUES (?, ?, ?, ?, ?)",
            (share["id"], pos, verdict, comment, now),
        )
        conn.execute("COMMIT")
        return {"status": "ok", "share_id": share["id"], "pos": pos, "verdict": verdict}
    finally:
        conn.close()


def serve(argv: list[str]) -> None:
    import uvicorn  # noqa: PLC0415

    p = argparse.ArgumentParser(prog="prop serve")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    ns = p.parse_args(argv)
    # uvicorn configures only its own loggers; without this, propd's INFO lines (model releases) go nowhere.
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
    logging.getLogger("proproom").addHandler(handler)
    logging.getLogger("proproom").setLevel(logging.INFO)
    logging.getLogger("proproom").propagate = False
    server = uvicorn.Server(uvicorn.Config(app, host=ns.host, port=ns.port, log_level="info", proxy_headers=False))
    _state["server"] = server
    server.run()
    if _state.get("gpu_lost"):
        raise SystemExit(EXIT_GPU_LOST)
