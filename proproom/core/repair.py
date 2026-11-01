"""Repair: an inventory of known library defects, and an `apply` that fixes only the mechanical ones.

`apply` fixes blob MIME types recorded as application/octet-stream when the bytes sniff as a known type (a bug in
early imports). It backs up the database first and changes nothing else. Orphaned assets and stray projects are
listed for a person to decide; repair never invents provenance or deletes a project.
"""
from __future__ import annotations

from typing import Any

from . import db, media, store
from .ids import utcnow


def _mime_changes(conn, home) -> list[dict[str, Any]]:
    changes = []
    for row in conn.execute("SELECT sha256, mime FROM blob WHERE stored = 1 AND "
                            "(mime = 'application/octet-stream' OR mime IS NULL)"):
        path = store.blob_abspath(home, row["sha256"])
        if path.is_file():
            mime, _ = media.sniff_mime(path)
            if mime and mime != "application/octet-stream":
                changes.append({"type": "blob_mime", "sha256": row["sha256"], "from": row["mime"], "to": mime})
    return changes


def repair_library(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    home = ctx.booted.home
    conn = db.connect(home.db, readonly=not args.get("apply"))
    try:
        changes = _mime_changes(conn, home)
        orphans = [dict(r) for r in conn.execute(
            "SELECT id, slug FROM asset WHERE NOT EXISTS (SELECT 1 FROM provenance WHERE provenance.asset_id = asset.id)")]
        projects = [dict(r) for r in conn.execute("SELECT id, path, name FROM project ORDER BY path")]
        result = {"status": "dry_run", "changes": changes, "orphaned_assets_requiring_review": orphans,
                  "projects": projects, "apply_fixes": ["blob_mime"]}
        if not args.get("apply") or not changes:
            return result
        backup = home.dir("backups") / f"db-repair-{utcnow().replace(':', '').replace('-', '')}.sqlite"
        conn.execute("VACUUM INTO ?", (str(backup),))
        conn.execute("BEGIN IMMEDIATE")
        try:
            applied = 0
            for ch in changes:
                applied += conn.execute("UPDATE blob SET mime = ? WHERE sha256 = ? AND mime IS ?",
                                        (ch["to"], ch["sha256"], ch["from"])).rowcount
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return {**result, "status": "applied", "applied": applied, "backup": str(backup)}
    finally:
        conn.close()
