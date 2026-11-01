"""Task R2: journal recovery and bounded index progress."""
import json

import pytest
from support import ingest
from proproom.api.registry import REGISTRY
from proproom.core import bootstrap, delivery, store
from proproom.core.journal import read_journal
from proproom.sdk.errors import ProproomError


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize("point", ["file", "manifest"])
def test_restart_undoes_interrupted_link_and_preserves_originals(ctx, conn, home, nas, monkeypatch, point):
    project = nas / "project" / "test"
    (project / "assets").mkdir(parents=True)
    asset = ingest(conn, home, slug="source", title="Source", data=b"new image")
    plan = REGISTRY["link"](ctx, {"assets": [asset.asset_id], "project": "project/test", "plan": True})
    target = project / plan["planned_files"][0]["relpath"]
    target.write_bytes(b"existing user file")
    original_manifest = b'{"assets": [{"id": "old", "note": "keep"}]}\n'
    (project / "assets.json").write_bytes(original_manifest)
    if point == "file":
        real_copy = store.link_or_copy
        def crash_after_copy(*args, **kwargs):
            real_copy(*args, **kwargs)
            raise SimulatedCrash()
        monkeypatch.setattr(store, "link_or_copy", crash_after_copy)
    else:
        real_manifest = delivery._update_manifest
        def crash_after_manifest(*args, **kwargs):
            real_manifest(*args, **kwargs)
            raise SimulatedCrash()
        monkeypatch.setattr(delivery, "_update_manifest", crash_after_manifest)
    with pytest.raises(SimulatedCrash):
        REGISTRY["link"](ctx, {"plan_id": plan["plan_id"]})
    assert target.read_bytes() == b"new image"
    bootstrap.boot(home)
    assert target.read_bytes() == b"existing user file"
    assert (project / "assets.json").read_bytes() == original_manifest
    row = conn.execute("SELECT state, error FROM operation WHERE id = ?", (plan["plan_id"],)).fetchone()
    assert row["state"] == "failed" and json.loads(row["error"])["code"] == "interrupted"
    assert conn.execute("SELECT count(*) FROM usage WHERE operation_id = ?", (plan["plan_id"],)).fetchone()[0] == 0
    assert any(e["step"] == "delivery_rolled_back" for e in read_journal(home, plan["plan_id"]))


def test_recovery_refuses_to_overwrite_subsequent_user_edits(ctx, conn, home, nas, monkeypatch):
    project = nas / "project" / "test"
    (project / "assets").mkdir(parents=True)
    asset = ingest(conn, home, slug="image", title="Image", data=b"delivered")
    plan = REGISTRY["link"](ctx, {"assets": [asset.asset_id], "project": "project/test", "plan": True})
    target = project / plan["planned_files"][0]["relpath"]
    original = store.link_or_copy
    def crash_after_copy(*args, **kwargs):
        original(*args, **kwargs)
        raise SimulatedCrash()
    monkeypatch.setattr(store, "link_or_copy", crash_after_copy)
    with pytest.raises(SimulatedCrash):
        REGISTRY["link"](ctx, {"plan_id": plan["plan_id"]})
    target.write_bytes(b"edited after crash")
    bootstrap.boot(home)
    assert target.read_bytes() == b"edited after crash"
    row = conn.execute("SELECT state, error FROM operation WHERE id = ?", (plan["plan_id"],)).fetchone()
    assert row["state"] == "failed" and json.loads(row["error"])["code"] == "recovery_conflict"


def test_boot_does_not_recover_active_delivery(home, conn):
    from proproom.core import ops
    from proproom.core.journal import acquire_delivery_lock
    conn.execute("BEGIN IMMEDIATE")
    op_id, _, _ = ops.claim_operation(conn, home, "link", "active-delivery", {}, initial_state="running")
    conn.execute("COMMIT")
    lock = acquire_delivery_lock(home, op_id)
    try:
        bootstrap.boot(home)
        assert conn.execute("SELECT state FROM operation WHERE id = ?", (op_id,)).fetchone()[0] == "running"
    finally:
        lock.close()


def test_index_caches_hardware_and_bounds_skipped_samples(ctx, conn, home, monkeypatch):
    from proproom.core import hardware, jobs, worker
    from proproom.core.plugins import LoadedPlugin, PluginManager
    class Embedder:
        fingerprint = {"model": "probe", "revision": "1", "modalities": ["image"], "preprocess": {},
                       "prompts": {}, "pooling": "mean", "normalize": 1, "truncation": 4}
        dim = 4
        async def embed(self, context, inputs):
            raise AssertionError("unknown kinds must be skipped")
    for i in range(35):
        ingest(conn, home, slug=f"doc-{i}", title="Document", kind="document",
               mime="application/octet-stream", data=f"document-{i}".encode())
    pm = PluginManager(home, "test")
    pm.plugins["embed"] = LoadedPlugin("embed", "1", "0" * 64, ["embedder"], "dir", {}, Embedder())
    ctx.plugin_manager = pm
    monkeypatch.setattr(jobs, "should_pause", lambda *args: (False, None))
    detections = []
    def detect():
        detections.append(True)
        return {"tier": "cpu", "devices": []}
    monkeypatch.setattr(hardware, "detect", detect)
    jid = REGISTRY["index"](ctx, {})["id"]
    for _ in range(4):
        assert worker.tick(ctx)
    row = conn.execute("SELECT state, progress FROM job WHERE id = ?", (jid,)).fetchone()
    progress = json.loads(row["progress"])
    assert row["state"] == "succeeded"
    assert progress["skipped_count"] == 35 and len(progress["skipped"]) == 20
    assert len(detections) == 1


def test_pull_persists_order_and_page_cursors(ctx, nas, fake_download, monkeypatch):
    from support import install_source
    from proproom.core import jobs, worker
    from proproom.sdk.types import Candidate, CandidatePage, RepresentationOffer, ResolvedSource, RightsEvidence, SourceRef
    evidence = RightsEvidence(basis="license", license_id="CC BY 4.0")
    class Source:
        def __init__(self):
            self.searches = []
            self.resolved = []
        async def search(self, context, request):
            self.searches.append(request.cursor)
            ids = ["first", "second"] if request.cursor is None else ["third"]
            return CandidatePage(items=[Candidate(ref=SourceRef("fake", name, "v1"), title=name) for name in ids],
                                 next_cursor="next" if request.cursor is None else None)
        async def resolve(self, context, ref):
            self.resolved.append((ref.external_id, ref.revision))
            return ResolvedSource(ref=ref, title=ref.external_id, kind="photo", metadata={}, evidence=[evidence],
                                  offers=[RepresentationOffer("original", "image/jpeg", f"https://example.org/{ref.external_id}.jpg")])
    source = Source()
    install_source(ctx, "fake", source)
    monkeypatch.setattr(jobs, "should_pause", lambda *args: (False, None))
    (nas / "pull.yaml").write_text("jobs:\n  - {source: fake, search: x}\n")
    REGISTRY["pull"](ctx, {"spec": "pull.yaml", "budget": "1M"})
    for _ in range(20):
        if not worker.tick(ctx):
            break
    assert source.searches == [None, "next"]
    assert len(fake_download) == 3
    assert set(source.resolved) == {("first", "v1"), ("second", "v1"), ("third", "v1")}


def test_pull_error_migration_preserves_prior_decisions():
    import sqlite3
    from proproom.core import db
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE policy(version INTEGER PRIMARY KEY);
        CREATE TABLE job(id TEXT PRIMARY KEY);
        CREATE TABLE pull_seen(source TEXT NOT NULL, external_id TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('taken','rejected','skipped','deferred')),
            reason TEXT NOT NULL, policy_version INTEGER NOT NULL REFERENCES policy(version),
            job_id TEXT REFERENCES job(id), evaluated_at TEXT NOT NULL,
            PRIMARY KEY(source, external_id)) STRICT;
        INSERT INTO policy VALUES (1);
        INSERT INTO pull_seen VALUES ('source', 'id', 'taken', 'ok', 1, NULL, 'now');
    """)
    migration = next(sql for version, _, sql in db.migrations() if version == 2)
    conn.executescript(migration)
    assert conn.execute("SELECT decision FROM pull_seen WHERE external_id='id'").fetchone()[0] == "taken"
    conn.execute("INSERT INTO pull_seen VALUES ('source', 'retry', 'error', 'network', 1, NULL, 'now')")
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()


def test_restart_removes_an_interrupted_temporary_copy(ctx, conn, home, nas, monkeypatch):
    project = nas / "project" / "test"
    (project / "assets").mkdir(parents=True)
    asset = ingest(conn, home, slug="image", title="Image", data=b"complete blob")
    plan = REGISTRY["link"](ctx, {"assets": [asset.asset_id], "project": "project/test", "plan": True})
    def crash_during_copy(src, dst, mode, *, temporary):
        temporary.write_bytes(b"partial")
        raise SimulatedCrash()
    monkeypatch.setattr(store, "link_or_copy", crash_during_copy)
    with pytest.raises(SimulatedCrash):
        REGISTRY["link"](ctx, {"plan_id": plan["plan_id"]})
    assert list((project / "assets").glob(".prop-*.tmp"))
    bootstrap.boot(home)
    assert not list((project / "assets").glob(".prop-*.tmp"))
    assert not (project / plan["planned_files"][0]["relpath"]).exists()
