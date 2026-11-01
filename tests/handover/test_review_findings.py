"""Gaps found by probing after the first round of fixes (2026-10-31): cases the section files did not pin down.

Move each test into its section file once its gap is fixed. Tests without a gap mark were fixed in the
same round and stay as regressions.

Contract this file adds (not spelled out in the handover, decided here):
- `proproom.core.worker.tick(ctx) -> bool` runs one unit of background work — the next step of a due job, or
  the due event deliveries — using `ctx.plugin_manager`, and returns whether it did anything. The container's
  worker loop calls it; tests call it directly.
"""
from __future__ import annotations

import json

import pytest
from support import gap, ingest, install_source

from proproom.api.registry import REGISTRY
from proproom.core import db, dedupe
from proproom.core.plugins import LoadedPlugin
from proproom.sdk.types import (Candidate, CandidatePage, RepresentationOffer, ResolvedSource, RightsEvidence,
                                SourceRef)


class _Src:
    def __init__(self, evidence, *, candidates=()):
        self.evidence, self.candidates = evidence, list(candidates)

    async def resolve(self, ctx_, ref):
        return ResolvedSource(ref=ref, title=f"T {ref.external_id}", kind="photo", metadata={}, evidence=self.evidence,
                              offers=[RepresentationOffer("original", "image/jpeg",
                                                          f"https://example.org/{ref.external_id}.jpg")])

    async def search(self, ctx_, req):
        return CandidatePage(items=[Candidate(ref=SourceRef("fake", x), title=x, rights=self.evidence[0])
                                    for x in self.candidates])

    enumerate = search


def _db(ctx):
    return db.connect(ctx.booted.home.db)


def _drain(ctx, limit=20):
    from proproom.core import worker
    for _ in range(limit):
        if not worker.tick(ctx):
            return
    raise AssertionError("worker never went idle")


# ───────────────────────── §6 rights: fail closed ─────────────────────────

def test_fetch_weighs_all_evidence(ctx, fake_download):
    install_source(ctx, "fake", _Src([RightsEvidence(basis="license", license_id="CC BY 4.0"),
                                      RightsEvidence(basis="license", license_id="CC BY-NC 4.0")]))
    with pytest.raises(Exception):
        REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "two"})
    assert fake_download == []


def test_crashing_policy_plugin_fails_closed(ctx, fake_download):
    class Boom:
        def decide(self, ctx_, asset):
            raise RuntimeError("policy plugin crashed")

    pm = install_source(ctx, "fake", _Src([RightsEvidence(basis="license", license_id="CC BY 4.0")]))
    pm.plugins["boom"] = LoadedPlugin("boom", "0", "0" * 64, ["policy"], "dir", {}, Boom())
    with pytest.raises(Exception):
        REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "boom"})
    assert fake_download == []


# ───────────────────────── §5 identity ─────────────────────────

def test_provenance_never_attaches_to_another_asset(conn):
    conn.execute("BEGIN IMMEDIATE")
    a1, _ = dedupe.register_asset(conn, slug="r1", kind="photo", title="r1")
    a2, _ = dedupe.register_asset(conn, slug="r2", kind="photo", title="r2")
    dedupe.register_provenance(conn, asset_id=a1, source="commons", external_id="File:X.jpg", revision="123")
    try:
        p2 = dedupe.register_provenance(conn, asset_id=a2, source="commons", external_id="File:X.jpg", revision="123")
    except Exception:
        p2 = None  # refusing the conflict is acceptable; silently re-pointing is not
    owner = conn.execute("SELECT asset_id FROM provenance WHERE id = ?", (p2,)).fetchone() if p2 else None
    conn.execute("ROLLBACK")
    assert owner is None or owner["asset_id"] == a2


def test_import_source_comes_from_the_url_host(ctx, nas):
    ep = nas / "project" / "EpSrc"
    (ep / "assets").mkdir(parents=True)
    (ep / "assets" / "a.jpg").write_bytes(b"aaaa")
    (ep / "assets" / "b.jpg").write_bytes(b"bbbb")
    (ep / "assets.json").write_text(json.dumps({"assets": [
        {"id": "a", "kind": "photo", "file": "assets/a.jpg", "license": "CC0",
         "source": "https://commons.wikimedia.org/wiki/File:A.jpg"},
        {"id": "b", "kind": "photo", "file": "assets/b.jpg", "license": "CC0",
         "source": "https://example.org/commons-mirror/b"}]}))
    REGISTRY["import_project"](ctx, {"project": "project/EpSrc"})
    rows = dict(_db(ctx).execute("SELECT source_url, source FROM provenance").fetchall())
    assert rows["https://commons.wikimedia.org/wiki/File:A.jpg"] == "commons"
    assert rows["https://example.org/commons-mirror/b"] != "commons"


# ───────────────────────── §10 delivery ─────────────────────────

def test_delivery_keeps_entries_of_a_list_manifest(ctx, conn, home, nas):
    ep = nas / "project" / "EpList"
    (ep / "assets").mkdir(parents=True)
    (ep / "assets.json").write_text(json.dumps([{"id": "hand-made", "kind": "photo", "file": "assets/old.jpg"}]))
    a = ingest(conn, home, slug="new", title="New")
    REGISTRY["link"](ctx, {"assets": [a.asset_id], "project": "project/EpList"})
    data = json.loads((ep / "assets.json").read_text())
    entries = data if isinstance(data, list) else data.get("assets", [])
    assert "hand-made" in {e.get("id") for e in entries}


def test_delivery_refuses_to_overwrite_an_unreadable_manifest(ctx, conn, home, nas):
    ep = nas / "project" / "EpBroken"
    (ep / "assets").mkdir(parents=True)
    broken = b'{"assets": [{"id": "hand-made", '  # truncated by an editor crash
    (ep / "assets.json").write_bytes(broken)
    a = ingest(conn, home, slug="new2", title="New")
    with pytest.raises(Exception):
        REGISTRY["link"](ctx, {"assets": [a.asset_id], "project": "project/EpBroken"})
    assert (ep / "assets.json").read_bytes() == broken


# ───────────────────────── §13 pull ─────────────────────────

def test_pull_takes_candidates_and_records_them(ctx, nas, fake_download):
    install_source(ctx, "fake", _Src([RightsEvidence(basis="license", license_id="CC BY 4.0")],
                                     candidates=["c1", "c2"]))
    (nas / "pull.yaml").write_text("budget: 1M\njobs:\n  - {source: fake, kind: photo, search: \"x\", share: 1.0}\n")
    job = REGISTRY["pull"](ctx, {"spec": "pull.yaml"})
    _drain(ctx)
    c = _db(ctx)
    assert len(fake_download) == 2
    assert {tuple(r) for r in c.execute("SELECT external_id, decision FROM pull_seen")} == {("c1", "taken"),
                                                                                          ("c2", "taken")}
    row = c.execute("SELECT state, used_bytes FROM job WHERE id = ?", (job["job_id"],)).fetchone()
    assert row["state"] == "succeeded" and row["used_bytes"] > 0


def test_pull_records_why_a_candidate_was_rejected(ctx, nas, fake_download):
    install_source(ctx, "fake", _Src([RightsEvidence(basis="license", license_id="CC BY-NC 4.0")],
                                     candidates=["nc1"]))
    (nas / "pull.yaml").write_text("budget: 1M\njobs:\n  - {source: fake, kind: photo, search: \"x\", share: 1.0}\n")
    REGISTRY["pull"](ctx, {"spec": "pull.yaml"})
    _drain(ctx)
    row = _db(ctx).execute("SELECT decision, reason FROM pull_seen WHERE external_id = 'nc1'").fetchone()
    assert fake_download == [] and row["decision"] == "rejected" and row["reason"]


def test_a_failing_source_does_not_wedge_the_worker(ctx, nas, fake_download):
    class Down:
        async def search(self, ctx_, req):
            raise RuntimeError("source is down")

    install_source(ctx, "down", Down())
    (nas / "pull.yaml").write_text("budget: 1M\njobs:\n  - {source: down, kind: photo, search: \"x\", share: 1.0}\n")
    job = REGISTRY["pull"](ctx, {"spec": "pull.yaml"})
    _drain(ctx)
    row = _db(ctx).execute("SELECT state, error FROM job WHERE id = ?", (job["job_id"],)).fetchone()
    assert row["state"] in ("failed", "succeeded") and "source is down" in (row["error"] or "")


# ───────────────────────── §11.2 hooks ─────────────────────────

def _hook_plugin(home, name, body):
    d = home.dir("plugins") / name
    d.mkdir()
    (d / "plugin.toml").write_text(f'[plugin]\nname = "{name}"\nversion = "0.0.1"\ntypes = ["hook"]\n')
    (d / "__init__.py").write_text("class Plugin:\n    async def handle(self, ctx, event_id, kind, payload):\n"
                                   + "".join(f"        {line}\n" for line in body))


def test_hook_plugins_receive_ingest_events(ctx, home, fake_download):
    seen = home.dir("tmp") / "hook_seen"
    _hook_plugin(home, "recorder", [f"open({str(seen)!r}, 'a').write(f'{{event_id}} {{kind}}\\n')"])
    pm = install_source(ctx, "fake", _Src([RightsEvidence(basis="license", license_id="CC BY 4.0")]))
    pm._load_directory_plugins()
    REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "E"})
    _drain(ctx)
    c = _db(ctx)
    eid = c.execute("SELECT id FROM event WHERE kind = 'ingested'").fetchone()[0]
    assert f"{eid} ingested" in seen.read_text().splitlines()
    assert c.execute("SELECT state FROM event_delivery WHERE event_id = ? AND subscriber = 'recorder'",
                     (eid,)).fetchone()[0] == "delivered"


def test_failing_hook_does_not_undo_the_ingest(ctx, home, fake_download):
    _hook_plugin(home, "broken", ["raise RuntimeError('hook down')"])
    pm = install_source(ctx, "fake", _Src([RightsEvidence(basis="license", license_id="CC BY 4.0")]))
    pm._load_directory_plugins()
    res = REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "F"})
    _drain(ctx)
    c = _db(ctx)
    assert c.execute("SELECT count(*) FROM asset WHERE id = ?", (res["asset_id"],)).fetchone()[0] == 1
    row = c.execute("SELECT state, attempts, last_error FROM event_delivery WHERE subscriber = 'broken'").fetchone()
    assert row["state"] in ("failed", "dead") and row["attempts"] >= 1 and "hook down" in row["last_error"]
