"""Repair lists defects; apply fixes only blob MIME types, after a backup."""
from proproom.api.registry import REGISTRY
from proproom.core import dedupe, jobs
from proproom.sdk.errors import ProproomError
import pytest


def test_repair_lists_orphans_and_does_not_modify_them(ctx, conn):
    conn.execute("BEGIN IMMEDIATE")
    asset, _ = dedupe.register_asset(conn, slug="orphan", kind="photo", title="Orphan")
    jid = jobs.create_job(conn, "import_project", {}, state="running")
    conn.execute("COMMIT")
    result = REGISTRY["repair"](ctx, {})
    assert result["status"] == "dry_run"
    assert asset in [item["id"] for item in result["orphaned_assets_requiring_review"]]
    assert conn.execute("SELECT state FROM job WHERE id = ?", (jid,)).fetchone()[0] == "running"
    assert not conn.execute("SELECT 1 FROM provenance WHERE asset_id = ?", (asset,)).fetchone()
    applied = REGISTRY["repair"](ctx, {"apply": True})
    assert conn.execute("SELECT state FROM job WHERE id = ?", (jid,)).fetchone()[0] == "running"
    assert not conn.execute("SELECT 1 FROM provenance WHERE asset_id = ?", (asset,)).fetchone()
    assert applied["status"] == "dry_run"  # nothing mechanical to fix


def test_repair_apply_fixes_octet_stream_mime_after_a_backup(ctx, conn, home):
    from proproom.core import store
    jpeg = bytes.fromhex("ffd8ffe000104a46494600010100000100010000") + b"\x00" * 64
    conn.execute("BEGIN IMMEDIATE")
    sha, size, rel = store.put_bytes(home, jpeg, "application/octet-stream")
    dedupe.register_blob(conn, sha, size, "application/octet-stream", stored=True, storage_path=rel)
    conn.execute("COMMIT")
    dry = REGISTRY["repair"](ctx, {})
    assert [c["to"] for c in dry["changes"]] == ["image/jpeg"]
    assert conn.execute("SELECT mime FROM blob WHERE sha256 = ?", (sha,)).fetchone()[0] == "application/octet-stream"
    res = REGISTRY["repair"](ctx, {"apply": True})
    assert res["status"] == "applied" and res["applied"] == 1
    assert conn.execute("SELECT mime FROM blob WHERE sha256 = ?", (sha,)).fetchone()[0] == "image/jpeg"
    from pathlib import Path
    assert Path(res["backup"]).is_file()
