"""§10 delivery: plan, atomic placement, usage, manifest, credits."""
from __future__ import annotations

import json
import os

import pytest
from support import gap, ingest

from proproom.api.registry import REGISTRY
from proproom.core import db, store
from proproom.core.journal import read_journal
from proproom.sdk.errors import ProproomError


def _link(ctx, **args):
    return REGISTRY["link"](ctx, args)


@pytest.fixture()
def ep(nas):
    p = nas / "project" / "Ep01"
    (p / "assets").mkdir(parents=True)
    return p


def test_plan_then_execute_by_plan_id(ctx, conn, home, ep):
    """§10: plan lists files/obligations/credits; executing by plan id delivers exactly that plan."""
    a = ingest(conn, home, slug="toc", title="Tocqueville", data=b"portrait bytes")
    plan = _link(ctx, assets=[a.asset_id], project="project/Ep01", plan=True)
    assert plan["planned_files"][0]["relpath"].startswith("assets/")
    assert {"attribution", "share_alike"} <= set(plan["obligations"])
    assert not (ep / plan["planned_files"][0]["relpath"]).exists()
    res = _link(ctx, plan_id=plan["plan_id"])
    delivered = ep / res["delivered"][0]["path"]
    assert delivered.read_bytes() == b"portrait bytes"
    row = conn.execute("SELECT * FROM usage WHERE asset_id = ?", (a.asset_id,)).fetchone()
    assert row["delivered_sha256"] == a.sha and json.loads(row["credit_snapshot"])["license"] == "cc-by-sa-4.0"


def test_hardlink_only_when_asked_and_blob_read_only(ctx, conn, home, ep):
    """§10: hardlink is explicit; the blob stays 0444."""
    a = ingest(conn, home, slug="h", title="H")
    res = _link(ctx, assets=[a.asset_id], project="project/Ep01", link_mode="hardlink")
    assert res["delivered"][0]["link_mode"] == "hardlink"
    assert store.blob_abspath(home, a.sha).stat().st_mode & 0o222 == 0


def test_credits_use_delivery_time_snapshot(ctx, conn, home, ep):
    """§6 / B.3: credits come from the snapshot taken at delivery, not current metadata."""
    a = ingest(conn, home, slug="c", title="Original Title", attribution="Jane Doe")
    _link(ctx, assets=[a.asset_id], project="project/Ep01")
    conn.execute("UPDATE asset SET title = 'Renamed Later' WHERE id = ?", (a.asset_id,))
    credits = REGISTRY["credits"](ctx, {"project": "project/Ep01"})
    assert "Original Title" in credits["formatted"] and "Jane Doe" in credits["formatted"]


def test_plan_has_no_filesystem_side_effects(ctx, conn, home, nas):
    a = ingest(conn, home, slug="p", title="P")
    _link(ctx, assets=[a.asset_id], project="project/NewEpisode", plan=True)
    assert not (nas / "project" / "NewEpisode").exists()


def test_link_requires_a_project(ctx, conn, home, nas):
    a = ingest(conn, home, slug="p", title="P")
    with pytest.raises(ProproomError):
        _link(ctx, assets=[a.asset_id])
    assert not (nas / "default").exists()


def test_project_path_cannot_escape_nas(ctx, conn, home, nas):
    a = ingest(conn, home, slug="p", title="P")
    with pytest.raises(ProproomError):
        _link(ctx, assets=[a.asset_id], project="../outside")
    assert not (nas.parent / "outside").exists()


def test_as_name_cannot_escape_project(conn, home, ep):
    from proproom.core import delivery
    a = ingest(conn, home, slug="p", title="P")
    conn.execute("BEGIN IMMEDIATE")
    pid, pdir, _ = delivery.resolve_project(conn, home, "project/Ep01")
    with pytest.raises(ProproomError):
        delivery.prepare_delivery_plan(conn, home, pdir, pid,
                                       [{"asset_id": a.asset_id, "representation_id": a.rep_id,
                                         "as_name": "../../../escaped"}])
    conn.execute("ROLLBACK")


def test_review_items_need_explicit_confirmation(ctx, conn, home, ep):
    """§6: quotation must be explicitly confirmed by a human or the agent at delivery."""
    q = ingest(conn, home, slug="release", title="Press release", kind="screenshot", basis="quotation",
               license_id=None)
    plan = _link(ctx, assets=[q.asset_id], project="project/Ep01", plan=True)
    assert plan["planned_files"] == [] and plan["blocked"][0]["reason"] in ("needs_confirmation", "review")


def test_relink_keeps_usage_history(ctx, conn, home, ep):
    from proproom.core import delivery, ops
    a = ingest(conn, home, slug="v1", title="First", attribution="Alice")
    b = ingest(conn, home, slug="v2", title="Second", attribution="Bob")
    for asset in (a, b):
        conn.execute("BEGIN IMMEDIATE")
        pid, pdir, _ = delivery.resolve_project(conn, home, "project/Ep01")
        op, _, _ = ops.claim_operation(conn, home, "link", None, {"a": asset.asset_id})
        item = [{"asset_id": asset.asset_id, "representation_id": asset.rep_id, "as_name": "hero"}]
        delivery.execute_delivery(conn, home, pdir, pid, op, delivery.prepare_delivery_plan(conn, home, pdir, pid, item))
        conn.execute("COMMIT")
    live = conn.execute("SELECT asset_id, credit_snapshot FROM usage WHERE as_name='hero' AND removed_at IS NULL") \
        .fetchall()
    assert [r["asset_id"] for r in live] == [b.asset_id]
    assert json.loads(live[0]["credit_snapshot"])["artist"] == "Bob"
    assert conn.execute("SELECT count(*) FROM usage WHERE as_name='hero'").fetchone()[0] == 2


def test_manifest_is_patched_not_replaced(ctx, conn, home, ep):
    """§10: exporter produces a patch, core applies it; appendix A: existing assets.json entries stay."""
    (ep / "assets.json").write_text(json.dumps({"assets": [{"id": "hand-made", "kind": "photo",
                                                            "file": "assets/hand.jpg"}]}))
    a = ingest(conn, home, slug="n", title="N")
    _link(ctx, assets=[a.asset_id], project="project/Ep01")
    ids = [x["id"] for x in json.loads((ep / "assets.json").read_text())["assets"]]
    assert "hand-made" in ids and len(ids) == 2


def test_delivered_extension_matches_content(ctx, nas):
    ep = nas / "project" / "EpV"
    (ep / "assets").mkdir(parents=True)
    (ep / "assets" / "clip.webm").write_bytes(b"\x1aE\xdf\xa3 fake webm")
    (ep / "assets.json").write_text(json.dumps({"assets": [
        {"id": "clip", "kind": "footage", "file": "assets/clip.webm", "license": "CC BY-SA 4.0",
         "source": "https://commons.wikimedia.org/wiki/File:Clip.webm"}]}))
    REGISTRY["import_project"](ctx, {"project": "project/EpV"})
    c = db.connect(ctx.booted.home.db)
    aid = c.execute("SELECT id FROM asset").fetchone()[0]
    (nas / "project" / "Ep02").mkdir(parents=True)
    res = REGISTRY["link"](ctx, {"assets": [aid], "project": "project/Ep02"})
    assert res["delivered"][0]["path"].endswith(".webm")


def test_copied_files_are_editable_in_the_project(conn, home, tmp_path):
    a = ingest(conn, home, slug="c", title="C")
    dst = tmp_path / "proj" / "c.jpg"
    assert store.link_or_copy(store.blob_abspath(home, a.sha), dst, mode="copy") == "copy"
    assert os.access(dst, os.W_OK) and dst.stat().st_mode & 0o200


def test_reflink_is_only_reported_when_cloned(conn, home, tmp_path, monkeypatch):
    import fcntl
    a = ingest(conn, home, slug="r", title="R")

    def no_clone(*_a, **_k):
        raise OSError(95, "Operation not supported")
    monkeypatch.setattr(fcntl, "ioctl", no_clone)
    assert store.link_or_copy(store.blob_abspath(home, a.sha), tmp_path / "x.jpg", mode="auto") == "copy"


def test_plan_execution_is_idempotent(ctx, conn, home, ep):
    a = ingest(conn, home, slug="i", title="I")
    plan = _link(ctx, assets=[a.asset_id], project="project/Ep01", plan=True)
    first = _link(ctx, plan_id=plan["plan_id"])
    second = _link(ctx, plan_id=plan["plan_id"])
    assert second == first
    steps = [e["step"] for e in read_journal(home, plan["plan_id"])]
    assert steps.count("delivered_file") == 1


def test_interrupted_delivery_is_recoverable(ctx, conn, home, ep):
    """§10: every step is journaled; after a crash the op is rolled back or continued, never left half-done."""
    a = ingest(conn, home, slug="ok", title="OK", data=b"ok bytes")
    b = ingest(conn, home, slug="gone", title="Gone", data=b"gone bytes")
    p = store.blob_abspath(home, b.sha)
    os.chmod(p, 0o644)
    p.unlink()  # second item's blob is missing -> delivery fails after the first file was placed
    with pytest.raises(ProproomError):
        _link(ctx, assets=[a.asset_id, b.asset_id], project="project/Ep01", idempotency_key="crash-1")
    op = conn.execute("SELECT state FROM operation WHERE idempotency_key = 'crash-1'").fetchone()
    placed = list((ep / "assets").iterdir())
    usages = conn.execute("SELECT count(*) FROM usage").fetchone()[0]
    assert op is not None and op["state"] in ("failed", "rolled_back")
    assert (placed == [] and usages == 0) or len(placed) == usages


def test_credits_group_on_screen_sources(ctx, conn, home, ep):
    """§6: credits list open-licence media, on-screen source pages, and data separately."""
    q = ingest(conn, home, slug="release", title="Ministry press release", kind="screenshot", basis="quotation",
               license_id=None)
    conn.execute("UPDATE rights_assessment SET decision = 'allow' WHERE id = ?", (q.assessment_id,))  # confirmed
    _link(ctx, assets=[q.asset_id], project="project/Ep01")
    credits = REGISTRY["credits"](ctx, {"project": "project/Ep01"})
    assert [i["title"] for i in credits["groups"]["quotation"]] == ["Ministry press release"]


def test_credits_resolve_the_exact_project(ctx, conn, home, nas):
    a = ingest(conn, home, slug="a", title="Only In V2")
    (nas / "project" / "SiennaCanadaV2").mkdir(parents=True)
    _link(ctx, assets=[a.asset_id], project="project/SiennaCanadaV2")
    with pytest.raises(ProproomError) as e:  # no project 'SiennaCanada' exists
        REGISTRY["credits"](ctx, {"project": "project/SiennaCanada"})
    assert e.value.code == "project_not_found"
