"""Tests for operations idempotency, journal, and delivery execution."""
import json
from pathlib import Path
import pytest
from proproom.core import bootstrap, db, dedupe, delivery, ops, store
from proproom.core.paths import Home
from proproom.sdk.errors import ProproomError


def test_ops_idempotency_and_journal(tmp_path):
    home = Home(tmp_path / "lib", tmp_path, "env", {})
    bootstrap.boot(home)
    conn = db.connect(home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        req1 = {"project": "ep01", "assets": ["a1"]}
        op_id, is_new, res = ops.claim_operation(conn, home, "link", "key-1", req1)
        assert is_new
        assert res is None

        # Finish op
        ops.complete_operation(conn, home, op_id, {"status": "done"})
        conn.execute("COMMIT")

        # Second call with same key and request -> returns previous result
        conn.execute("BEGIN IMMEDIATE")
        op_id2, is_new2, res2 = ops.claim_operation(conn, home, "link", "key-1", req1)
        assert not is_new2
        assert op_id2 == op_id
        assert res2 == {"status": "done"}
        conn.execute("COMMIT")

        # Third call with same key but DIFFERENT request -> raises idempotency_conflict
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(ProproomError) as exc:
            ops.claim_operation(conn, home, "link", "key-1", {"project": "different"})
        assert exc.value.code == "idempotency_conflict"
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def test_delivery_plan_and_execute(tmp_path):
    home = Home(tmp_path / "lib", tmp_path, "env", {})
    bootstrap.boot(home)
    conn = db.connect(home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Ingest test asset
        data = b"image content test"
        sha, size, rel = store.put_bytes(home, data)
        dedupe.register_blob(conn, sha, size, "image/jpeg", stored=True, storage_path=rel)

        aid, rid = dedupe.register_asset(conn, slug="portrait-1", kind="photo", title="Portrait 1")
        rep_id = dedupe.register_representation(conn, asset_id=aid, blob_sha256=sha, role="original", width=1920, height=1080)
        prov_id = dedupe.register_provenance(conn, asset_id=aid, source="manual", external_id="file1")
        dedupe.register_rights(conn, provenance_id=prov_id, asset_id=aid, policy_version=1,
                               policy_rules={"rights": {"allow": ["cc-by"]}}, basis="license", license_id="cc-by-4.0",
                               attribution="Author X")

        # Delivery plan
        proj_id, proj_dir, proj_rel = delivery.resolve_project(conn, home, "project/ep01")
        items = [{"asset_id": aid, "representation_id": rep_id, "as_name": "hero-bg"}]
        op_id, _, _ = ops.claim_operation(conn, home, "link", "op-link-1", {"project": proj_rel, "items": items})
        plan = delivery.prepare_delivery_plan(conn, home, proj_dir, proj_id, items)
        assert len(plan["planned_files"]) == 1
        assert "attribution" in plan["obligations"]

        # Execute delivery
        res = delivery.execute_delivery(conn, home, proj_dir, proj_id, op_id, plan)
        assert len(res["delivered"]) == 1

        delivered_path = proj_dir / "assets" / "hero-bg.jpg"
        assert delivered_path.is_file()
        assert delivered_path.read_bytes() == data

        # Check manifest
        manifest_path = proj_dir / "assets.json"
        assert manifest_path.is_file()
        m_data = json.loads(manifest_path.read_text())
        assert m_data["assets"][0]["id"] == "hero-bg"

        # Check credits
        credits_info = delivery.get_project_credits(conn, proj_id)
        assert "Author X" in credits_info["formatted"]

        conn.execute("COMMIT")
    finally:
        conn.close()
