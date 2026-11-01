"""Gaps found reviewing task R (2026-10-31): the stepped pull re-queries the source and forgets transient errors."""
from __future__ import annotations

from support import install_source

from proproom.api.registry import REGISTRY
from proproom.core import db
from proproom.sdk.types import (Candidate, CandidatePage, RepresentationOffer, ResolvedSource, RightsEvidence,
                                SourceRef)

EV = RightsEvidence(basis="license", license_id="CC BY 4.0")


class _Src:
    def __init__(self, n):
        self.n, self.searches = n, 0

    async def resolve(self, ctx_, ref):
        return ResolvedSource(ref=ref, title=ref.external_id, kind="photo", metadata={}, evidence=[EV],
                              offers=[RepresentationOffer("original", "image/jpeg",
                                                          f"https://example.org/{ref.external_id}.jpg")])

    async def search(self, ctx_, req):
        self.searches += 1
        return CandidatePage(items=[Candidate(ref=SourceRef("fake", f"x{i}"), title="x", rights=EV)
                                    for i in range(self.n)])


def _drain(ctx, limit=200):
    from proproom.core import worker
    for _ in range(limit):
        if not worker.tick(ctx):
            return
    raise AssertionError("worker never went idle")


def _pull(ctx, nas):
    (nas / "p.yaml").write_text("jobs:\n  - {source: fake, kind: photo, search: x}\n")
    return REGISTRY["pull"](ctx, {"spec": "p.yaml", "budget": "1G"})


def test_a_pull_searches_each_page_once(ctx, nas, fake_download):
    src = _Src(10)
    install_source(ctx, "fake", src)
    _pull(ctx, nas)
    _drain(ctx)
    assert len(fake_download) == 10 and src.searches <= 2


def test_a_transient_error_does_not_reject_a_candidate_for_good(ctx, nas, fake_download, monkeypatch):
    from proproom.core import worker
    install_source(ctx, "fake", _Src(1))
    real, calls = worker.fetch, []

    def flaky(ctx_, args):
        calls.append(args)
        if len(calls) == 1:
            raise ConnectionError("network blip")
        return real(ctx_, args)

    monkeypatch.setattr(worker, "fetch", flaky)
    _pull(ctx, nas)
    _drain(ctx)
    _pull(ctx, nas)
    _drain(ctx)
    row = db.connect(ctx.booted.home.db).execute("SELECT decision FROM pull_seen WHERE external_id = 'x0'").fetchone()
    assert len(fake_download) == 1 and row["decision"] == "taken"


# ───────────────────────── §10 recovery: never brick the boot ─────────────────────────

def _running_link(home, op_id):
    from proproom.core.ids import utcnow
    c = db.connect(home.db)
    c.execute("BEGIN IMMEDIATE")
    c.execute("INSERT INTO operation(id, kind, idempotency_key, request_hash, request, state, actor, created, started) "
              "VALUES (?, 'link', ?, 'h', '{}', 'running', 'test', ?, ?)", (op_id, f"k{op_id}", utcnow(), utcnow()))
    c.execute("COMMIT")
    c.close()


def test_a_recovery_conflict_is_reported_not_fatal(lib, nas):
    from proproom.core import bootstrap, journal
    from proproom.core.ids import ulid
    home, op_id = lib.home, ulid()
    target = nas / "project" / "P" / "assets" / "x.jpg"
    target.parent.mkdir(parents=True)
    _running_link(home, op_id)
    journal.prepare_write(home, op_id, target, "a" * 64)
    target.write_bytes(b"the editor's own file, written after the crash")
    bootstrap.boot(home)  # must not raise
    row = db.connect(home.db).execute("SELECT state, error FROM operation WHERE id = ?", (op_id,)).fetchone()
    assert row["state"] == "failed" and "recovery_conflict" in row["error"]
    assert target.read_bytes() == b"the editor's own file, written after the crash"


def test_a_legacy_journal_does_not_stop_the_boot(lib):
    from proproom.core import bootstrap, journal
    from proproom.core.ids import ulid
    home, op_id = lib.home, ulid()
    _running_link(home, op_id)
    journal.append_journal(home, op_id, "delivered_file", {"path": "project/P/assets/y.jpg"})
    bootstrap.boot(home)  # must not raise
    row = db.connect(home.db).execute("SELECT state, error FROM operation WHERE id = ?", (op_id,)).fetchone()
    assert row["state"] == "failed" and "recovery_ambiguous" in row["error"]
