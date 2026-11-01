"""Share link operations: create, feedback, revoke (handover §9)."""
from __future__ import annotations

import datetime
import hashlib
import secrets
from typing import Any

from . import db
from .ids import ulid, utcnow
from ..sdk.errors import ProproomError


def share(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Create a share link for assets or a selection."""
    b = ctx.booted
    token = secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    share_id = ulid()
    now = utcnow()
    expires_arg = args.get("expires")
    if expires_arg:
        if isinstance(expires_arg, str) and expires_arg.endswith("d"):
            days = float(expires_arg[:-1])
            expires_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
        elif isinstance(expires_arg, str) and expires_arg.endswith("h"):
            hours = float(expires_arg[:-1])
            expires_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)
        else:
            try:
                expires_dt = datetime.datetime.fromisoformat(expires_arg.replace("Z", "+00:00"))
            except Exception:
                expires_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
    else:
        expires_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
    expires = expires_dt.isoformat()

    conn = db.connect(b.home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO share(id, token_hash, selection_id, title, note, created, expires) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (share_id, token_hash, args.get("selection_id"), args.get("title"), args.get("note"), now, expires),
        )

        # Populate share_items from selection_id or assets
        pos = 1
        if sel_id := args.get("selection_id"):
            rows = conn.execute(
                "SELECT * FROM selection_item WHERE selection_id = ? ORDER BY idx", (sel_id,)
            ).fetchall()
            for r in rows:
                conn.execute(
                    "INSERT INTO share_item(share_id, pos, asset_id, representation_id, segment_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (share_id, pos, r["asset_id"], r["representation_id"], r["segment_id"]),
                )
                pos += 1
        elif asset_list := args.get("assets"):
            for aid in asset_list:
                rep = conn.execute(
                    "SELECT id FROM representation WHERE asset_id = ? ORDER BY role='original' DESC LIMIT 1",
                    (aid,),
                ).fetchone()
                if rep:
                    conn.execute(
                        "INSERT INTO share_item(share_id, pos, asset_id, representation_id, segment_id) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (share_id, pos, aid, rep["id"], None),
                    )
                    pos += 1

        conn.execute("COMMIT")

        public_url = b.config["server"]["public_url"] or ""
        share_url = f"{public_url}/s/{token}" if public_url else f"/s/{token}"
        warnings = [] if public_url else ["public_url_unknown"]

        return {
            "token": token,
            "url": share_url,
            "expires": expires,
            "warnings": warnings,
        }
    finally:
        conn.close()


def share_feedback(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Read want/reject verdicts and comments from a share."""
    b = ctx.booted
    share_token = args["share"]
    token_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()

    conn = db.connect(b.home.db, readonly=True)
    try:
        share_row = conn.execute("SELECT id FROM share WHERE token_hash = ?", (token_hash,)).fetchone()
        if not share_row:
            raise ProproomError("share_not_found", "Share link not found or expired")
        sid = share_row["id"]
        rows = conn.execute(
            "SELECT pos, verdict, comment, created FROM share_feedback WHERE share_id = ? ORDER BY pos, created",
            (sid,),
        ).fetchall()
        return {"share_id": sid, "feedback": [dict(r) for r in rows]}
    finally:
        conn.close()


def share_revoke(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Revoke a share link."""
    b = ctx.booted
    share_token = args["share"]
    token_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()

    conn = db.connect(b.home.db)
    try:
        now = utcnow()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE share SET revoked_at = ? WHERE token_hash = ?", (now, token_hash))
        conn.execute("COMMIT")
        return {"revoked": True}
    finally:
        conn.close()
