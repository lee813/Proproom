"""Tests for Task 4: Crash recovery on boot and worker pause/resume rules."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from proproom.api.registry import REGISTRY
from proproom.core import bootstrap, db, jobs, ops, worker
from proproom.core.journal import read_journal
from proproom.sdk.types import Candidate, CandidatePage, RepresentationOffer, ResolvedSource, RightsEvidence, SourceRef


class _FakeSrc:
    async def resolve(self, ctx_, ref):
        return ResolvedSource(
            ref=ref,
            title="T",
            kind="photo",
            metadata={},
            evidence=[RightsEvidence(basis="license", license_id="CC0")],
            offers=[RepresentationOffer("original", "image/jpeg", "https://example.org/t.jpg")],
        )

    async def search(self, ctx_, req):
        return CandidatePage(items=[Candidate(ref=SourceRef("fake", "1"), title="T")])

    enumerate = search


def test_boot_recovers_interrupted_running_operations(home):
    conn = db.connect(home.db)
    conn.execute("BEGIN IMMEDIATE")
    op_id, _, _ = ops.claim_operation(
        conn, home, "link", "test-crash-key", {"dummy": 1}, initial_state="running"
    )
    conn.execute("COMMIT")
    conn.close()

    # Re-run boot
    b = bootstrap.boot(home)
    conn = db.connect(home.db)
    row = conn.execute("SELECT state, error FROM operation WHERE id = ?", (op_id,)).fetchone()
    conn.close()

    assert row["state"] == "failed"
    err = json.loads(row["error"])
    assert err["code"] == "interrupted"

    journal_entries = read_journal(home, op_id)
    steps = [e["step"] for e in journal_entries]
    assert "recovered_on_boot" in steps


def test_worker_pauses_and_resumes_on_lockfile(ctx, nas, home, monkeypatch):
    # Setup pull job
    (nas / "pull.yaml").write_text("budget: 1M\njobs: []\n")
    job = REGISTRY["pull"](ctx, {"spec": "pull.yaml"})
    job_id = job["job_id"]

    # Configure lockfile
    ctx.booted.config["pause"] = {"lockfiles": ["tools/BUSY"]}
    (nas / "tools").mkdir(parents=True, exist_ok=True)
    busy_file = nas / "tools" / "BUSY"
    busy_file.touch()

    # Worker tick should pause the job
    worked = worker.tick(ctx)
    assert not worked

    conn = db.connect(home.db)
    row = conn.execute("SELECT state, pause_reason FROM job WHERE id = ?", (job_id,)).fetchone()
    assert row["state"] == "paused"
    assert row["pause_reason"] == "lockfile"

    # Remove lockfile
    busy_file.unlink()

    # Worker tick should resume the job
    worked = worker.tick(ctx)
    assert worked

    row = conn.execute("SELECT state, pause_reason FROM job WHERE id = ?", (job_id,)).fetchone()
    assert row["state"] in ("running", "succeeded")
    assert row["pause_reason"] is None
    conn.close()


def test_user_paused_job_is_not_resumed_by_worker(ctx, nas, home):
    (nas / "pull.yaml").write_text("budget: 1M\njobs: []\n")
    job = REGISTRY["pull"](ctx, {"spec": "pull.yaml"})
    job_id = job["job_id"]

    # User pauses job
    REGISTRY["job"](ctx, {"action": "pause", "job_id": job_id})
    conn = db.connect(home.db)
    row = conn.execute("SELECT state, pause_reason FROM job WHERE id = ?", (job_id,)).fetchone()
    assert row["state"] == "paused"
    assert row["pause_reason"] == "user"

    # Worker tick with no lockfile/load should NOT resume user-paused job
    ctx.booted.config["pause"] = {"lockfiles": []}
    worked = worker.tick(ctx)
    assert not worked

    row2 = conn.execute("SELECT state, pause_reason FROM job WHERE id = ?", (job_id,)).fetchone()
    assert row2["state"] == "paused"
    assert row2["pause_reason"] == "user"
    conn.close()
