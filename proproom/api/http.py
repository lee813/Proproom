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
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from .. import __version__
from ..core import bootstrap, db, store
from ..core.ids import utcnow
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
            "LEFT JOIN provenance p ON p.asset_id = a.id "
            "LEFT JOIN rights_evidence re ON re.provenance_id = p.id "
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
    b = _state["booted"]
    conn = db.connect(b.home.db, readonly=True)
    try:
        row = conn.execute(
            "SELECT rep.blob_sha256, b.mime, b.bytes, b.storage_path "
            "FROM share_item si "
            "JOIN representation rep ON rep.id = si.representation_id "
            "JOIN blob b ON b.sha256 = rep.blob_sha256 "
            "WHERE si.share_id = ? AND si.representation_id = ?",
            (share["id"], rep_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, detail="Media representation not found in this share")

        file_path = b.home.root / row["storage_path"]
        if not file_path.is_file():
            raise HTTPException(404, detail="Underlying media blob missing")

        file_size = row["bytes"]
        mime = row["mime"] or "application/octet-stream"

        # Support Range header for video / audio seeking
        range_header = request.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            byte_range = range_header.replace("bytes=", "").split("-")
            start = int(byte_range[0])
            end = int(byte_range[1]) if byte_range[1] else file_size - 1
            length = (end - start) + 1

            def iter_file():
                with open(file_path, "rb") as f:
                    f.seek(start)
                    rem = length
                    while rem > 0:
                        chunk_size = min(rem, 64 * 1024)
                        data = f.read(chunk_size)
                        if not data:
                            break
                        rem -= len(data)
                        yield data

            headers = {
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Content-Type": mime,
            }
            return StreamingResponse(iter_file(), status_code=206, headers=headers)

        return FileResponse(file_path, media_type=mime)
    finally:
        conn.close()


@app.post("/s/{token}/feedback")
async def share_feedback_submit(token: str, request: Request) -> dict[str, Any]:
    share = _get_share(token)
    body = await request.json()
    pos = int(body["pos"])
    verdict = body.get("verdict", "want")
    comment = body.get("comment", "")

    b = _state["booted"]
    conn = db.connect(b.home.db)
    try:
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
    uvicorn.run(app, host=ns.host, port=ns.port, log_level="info", proxy_headers=False)
