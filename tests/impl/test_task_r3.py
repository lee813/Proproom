"""Recovery reporting and pull retry policies."""
import json

import pytest

from proproom.api.registry import OpContext, REGISTRY
from proproom.core import bootstrap, db, jobs, ops, worker
from proproom.core.ids import utcnow
from proproom.core.journal import append_journal, prepare_write, read_journal
from proproom.core.store import hash_bytes
from proproom.sdk.errors import ProproomError
from proproom.sdk.types import Candidate, CandidatePage, RepresentationOffer, ResolvedSource, RightsEvidence, SourceRef
from support import install_source


class Source:
    def __init__(self):
        self.license = "CC BY 4.0"
    async def search(self, context, request):
        return CandidatePage(items=[Candidate(SourceRef("fake", "candidate"), "Candidate")])
    async def resolve(self, context, ref):
        return ResolvedSource(ref=ref, title="Candidate", kind="photo", metadata={},
                              evidence=[RightsEvidence(basis="license", license_id=self.license)],
                              offers=[RepresentationOffer("original", "image/jpeg", "https://example.org/image.jpg")])


def pull_and_drain(ctx, nas):
    (nas / "pull-r3.yaml").write_text("jobs:\n  - {source: fake, search: candidate}\n")
    REGISTRY["pull"](ctx, {"spec": "pull-r3.yaml", "budget": "1M"})
    for _ in range(20):
        if not worker.tick(ctx):
            return
    raise AssertionError("worker never became idle")


def test_pull_caps_repeated_transient_errors(ctx, conn, nas, monkeypatch):
    install_source(ctx, "fake", Source())
    monkeypatch.setattr(jobs, "should_pause", lambda *args: (False, None))
    attempts = []
    def fail(*args):
        attempts.append(1)
        raise ConnectionError("network unavailable")
    monkeypatch.setattr(worker, "fetch", fail)
    for _ in range(7):
        pull_and_drain(ctx, nas)
    row = conn.execute("SELECT decision, reason, error_attempts, last_error FROM pull_seen").fetchone()
    assert len(attempts) == 5
    assert tuple(row) == ("error", "too_many_errors", 5, "ConnectionError: network unavailable")


def test_rejected_candidate_is_rechecked_when_policy_changes(ctx, conn, nas, fake_download, monkeypatch):
    source = Source()
    source.license = "CC BY-NC 4.0"
    install_source(ctx, "fake", source)
    monkeypatch.setattr(jobs, "should_pause", lambda *args: (False, None))
    pull_and_drain(ctx, nas)
    assert conn.execute("SELECT decision FROM pull_seen").fetchone()[0] == "rejected"
    source.license = "CC BY 4.0"
    pull_and_drain(ctx, nas)
    assert not fake_download
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO policy(version, rules, created, created_by) SELECT version+1, rules, ?, 'test' FROM policy ORDER BY version DESC LIMIT 1", (utcnow(),))
    conn.execute("COMMIT")
    ctx.booted.policy_version += 1
    pull_and_drain(ctx, nas)
    row = conn.execute("SELECT decision, policy_version FROM pull_seen").fetchone()
    assert row["decision"] == "taken" and row["policy_version"] == ctx.booted.policy_version
    assert len(fake_download) == 1


def running_link(conn, home, key):
    conn.execute("BEGIN IMMEDIATE")
    op_id, _, _ = ops.claim_operation(conn, home, "link", key, {}, initial_state="running")
    conn.execute("COMMIT")
    return op_id


def test_conflicting_recovery_preserves_all_files_and_is_visible(ctx, conn, home, nas):
    root = nas / "project" / "episode"
    root.mkdir(parents=True)
    first, second = root / "first", root / "second"
    op_id = running_link(conn, home, "conflicting")
    prepare_write(home, op_id, first, hash_bytes(b"delivered-a")[0])
    prepare_write(home, op_id, second, hash_bytes(b"delivered-b")[0])
    first.write_bytes(b"user edit")
    second.write_bytes(b"delivered-b")
    booted = bootstrap.boot(home)
    assert first.read_bytes() == b"user edit" and second.read_bytes() == b"delivered-b"
    row = conn.execute("SELECT state, error FROM operation WHERE id = ?", (op_id,)).fetchone()
    error = json.loads(row["error"])
    assert row["state"] == "failed" and error["code"] == "recovery_conflict"
    assert str(first) in error["details"]["paths"]
    assert error["details"]["journal_path"].endswith(op_id + ".jsonl")
    assert any(e["step"] == "recovery_unresolved" for e in read_journal(home, op_id))
    recovery = REGISTRY["capabilities"](OpContext(booted), {"schemas": False})["unresolved_recoveries"]
    assert any(r["operation_id"] == op_id and r["error"]["code"] == "recovery_conflict" for r in recovery)


def test_legacy_recovery_reports_recorded_paths(conn, home):
    op_id = running_link(conn, home, "legacy")
    append_journal(home, op_id, "delivered_file", {"path": "project/episode/assets/frame.jpg"})
    bootstrap.boot(home)
    error = json.loads(conn.execute("SELECT error FROM operation WHERE id = ?", (op_id,)).fetchone()[0])
    assert error["code"] == "recovery_ambiguous"
    assert error["details"]["paths"] == ["project/episode/assets/frame.jpg"]


def test_unexpected_recovery_errors_still_stop_boot(conn, home, nas, monkeypatch):
    op_id = running_link(conn, home, "unexpected")
    path = nas / "file"
    prepare_write(home, op_id, path, hash_bytes(b"new")[0])
    def fail(*args):
        raise RuntimeError("disk error")
    monkeypatch.setattr(ops, "rollback_delivery", fail)
    with pytest.raises(RuntimeError, match="disk error"):
        bootstrap.boot(home)
    def typed_failure(*args):
        raise ProproomError("unexpected", "unexpected recovery error")
    monkeypatch.setattr(ops, "rollback_delivery", typed_failure)
    with pytest.raises(ProproomError) as exc:
        bootstrap.boot(home)
    assert exc.value.code == "unexpected"
    assert conn.execute("SELECT state FROM operation WHERE id = ?", (op_id,)).fetchone()[0] == "running"
