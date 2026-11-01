"""Gaps found reviewing the tasks 1b-7 round (2026-10-31): fake job success, no hook retry, pull budget and steps.

Contract (decided here, on top of test_review_findings.py):
- An operation that starts a job returns it `queued`; the work happens in `worker.tick`.
- A job that fails ends `failed` with `job.error`; `tick` never marks a job `succeeded` that it did not run.
- A `failed` hook delivery is retried by `tick` once `next_attempt` is due, until it is delivered or `dead`.
"""
from __future__ import annotations

import pytest
from support import gap, ingest, install_source

from proproom.api.registry import REGISTRY
from proproom.core import db, jobs
from proproom.core.plugins import LoadedPlugin
from proproom.sdk.types import (Candidate, CandidatePage, EmbeddingBatch, RepresentationOffer, ResolvedSource,
                                RightsEvidence, SourceRef)


def _db(ctx):
    return db.connect(ctx.booted.home.db)


def _drain(ctx, limit=50):
    from proproom.core import worker
    for _ in range(limit):
        if not worker.tick(ctx):
            return
    raise AssertionError("worker never went idle")


class _Embedder:
    fingerprint = {"model": "probe", "revision": "1", "modalities": ["image"], "preprocess": {}, "prompts": {},
                   "pooling": "mean", "normalize": 1, "truncation": 4}
    dim = 4

    def __init__(self, fail=False):
        self.fail = fail

    async def embed(self, ctx, inputs):
        if self.fail:
            raise RuntimeError("GPU fell over")
        return EmbeddingBatch(space_fingerprint=self.fingerprint, vectors=[[1.0, 0, 0, 0] for _ in inputs],
                              input_hashes=[str(i) for i, _ in enumerate(inputs)])


def _install_embedder(ctx, inst):
    pm = install_source(ctx, "fake", object())
    pm.plugins["emb"] = LoadedPlugin("emb", "0", "0" * 64, ["embedder"], "dir", {}, inst)


class _Src:
    def __init__(self, candidates=()):
        self.ev = RightsEvidence(basis="license", license_id="CC BY 4.0")
        self.candidates = list(candidates)

    async def resolve(self, ctx_, ref):
        return ResolvedSource(ref=ref, title=ref.external_id, kind="photo", metadata={}, evidence=[self.ev],
                              offers=[RepresentationOffer("original", "image/jpeg",
                                                          f"https://example.org/{ref.external_id}.jpg")])

    async def search(self, ctx_, req):
        return CandidatePage(items=[Candidate(ref=SourceRef("fake", x), title=x, rights=self.ev)
                                    for x in self.candidates])


def _pull(ctx, nas, budget):
    (nas / "pull.yaml").write_text("jobs:\n  - {source: fake, kind: photo, search: x, share: 1.0}\n")
    return REGISTRY["pull"](ctx, {"spec": "pull.yaml", "budget": budget})


# ───────────────────────── §13 jobs: no fake success ─────────────────────────

def test_tick_never_marks_a_job_it_did_not_run_succeeded(ctx, conn):
    conn.execute("BEGIN IMMEDIATE")
    jid = jobs.create_job(conn, "import_project", {"project": "project/Nope"}, state="running")
    conn.execute("COMMIT")
    _drain(ctx)
    assert _db(ctx).execute("SELECT state FROM job WHERE id = ?", (jid,)).fetchone()[0] != "succeeded"


def test_index_returns_a_queued_job(ctx, conn, home):
    ingest(conn, home, slug="q", title="Q")
    _install_embedder(ctx, _Embedder())
    assert REGISTRY["index"](ctx, {})["state"] == "queued"


def test_a_failing_embedder_fails_the_index_job(ctx, conn, home):
    ingest(conn, home, slug="f", title="F")
    _install_embedder(ctx, _Embedder(fail=True))
    try:
        REGISTRY["index"](ctx, {})
    except Exception:
        pass  # raising here is fine; a lying job row is not
    _drain(ctx)
    row = _db(ctx).execute("SELECT state, error FROM job WHERE kind = 'index'").fetchone()
    assert row["state"] == "failed" and "GPU fell over" in (row["error"] or "")


# ───────────────────────── §11.2 hooks: at least once ─────────────────────────

def test_a_failed_hook_delivery_is_retried(ctx, home, fake_download):
    flag = home.dir("tmp") / "failed_once"
    d = home.dir("plugins") / "flaky"
    d.mkdir()
    (d / "plugin.toml").write_text('[plugin]\nname = "flaky"\nversion = "0.0.1"\ntypes = ["hook"]\n')
    (d / "__init__.py").write_text(
        "import os\nclass Plugin:\n    async def handle(self, ctx, event_id, kind, payload):\n"
        f"        if not os.path.exists({str(flag)!r}):\n"
        f"            open({str(flag)!r}, 'w').write('x')\n"
        "            raise RuntimeError('first try fails')\n")
    pm = install_source(ctx, "fake", _Src())
    pm._load_directory_plugins()
    REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "H"})
    c = _db(ctx)
    for _ in range(10):  # backoff may push next_attempt into the future: make it due
        _drain(ctx)
        c.execute("UPDATE event_delivery SET next_attempt = '2000-01-01T00:00:00.000Z' WHERE state = 'failed'")
        c.commit()
    row = c.execute("SELECT state, attempts FROM event_delivery WHERE subscriber = 'flaky'").fetchone()
    assert row["state"] == "delivered" and row["attempts"] == 2


# ───────────────────────── §13 pull: budget and steps ─────────────────────────

def test_pull_stops_at_the_budget(ctx, nas, fake_download):
    install_source(ctx, "fake", _Src([f"c{i}" for i in range(5)]))
    job = _pull(ctx, nas, "60")
    _drain(ctx)
    row = _db(ctx).execute("SELECT used_bytes, budget_bytes FROM job WHERE id = ?", (job["job_id"],)).fetchone()
    assert len(fake_download) < 5 and row["used_bytes"] <= row["budget_bytes"] + 60


def test_one_tick_is_one_step_of_a_pull(ctx, nas, fake_download):
    from proproom.core import worker
    install_source(ctx, "fake", _Src([f"d{i}" for i in range(50)]))
    _pull(ctx, nas, "1G")
    worker.tick(ctx)
    assert len(fake_download) < 50


def test_an_unparseable_budget_is_refused(ctx, nas):
    (nas / "pull.yaml").write_text("jobs: []\n")
    with pytest.raises(Exception):
        REGISTRY["pull"](ctx, {"spec": "pull.yaml", "budget": "lots"})
