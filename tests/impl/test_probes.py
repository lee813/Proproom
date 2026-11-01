"""Implementation tests for probes & background worker (Task 1)."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from proproom.api.registry import REGISTRY
from proproom.core import db, dedupe, events, jobs, worker
from proproom.core.plugins import LoadedPlugin
from proproom.sdk.types import (
    Candidate,
    CandidatePage,
    PolicyDecision,
    RepresentationOffer,
    ResolvedSource,
    RightsEvidence,
    SearchRequest,
    SourceRef,
)


class _MockMultiRightsSrc:
    def __init__(self, evidence_records, offers=None):
        self.evidence_records = evidence_records
        self.offers = offers or [RepresentationOffer("original", "image/jpeg", "https://example.org/img.jpg")]

    async def resolve(self, ctx, ref: SourceRef) -> ResolvedSource:
        return ResolvedSource(
            ref=ref,
            title="Multi Rights Item",
            kind="photo",
            metadata={"title": "Multi Rights Item"},
            evidence=self.evidence_records,
            offers=self.offers,
        )


def _install_source(ctx, name: str, instance):
    from proproom.core.plugins import PluginManager
    pm = getattr(ctx, "plugin_manager", None) or PluginManager(ctx.booted.home, "test-instance")
    pm.plugins[name] = LoadedPlugin(
        name=name,
        version="0.0.1",
        hash="0" * 64,
        types=["source"],
        origin="dir",
        manifest={"domains": {"download": ["example.org"]}},
        instance=instance,
    )
    ctx.plugin_manager = pm
    return pm


def test_multi_record_rights_denies_if_any_denied(ctx, fake_download):
    evidence = [
        RightsEvidence(basis="license", license_id="CC BY 4.0"),
        RightsEvidence(basis="license", license_id="CC BY-NC 4.0"),
    ]
    _install_source(ctx, "mock_multi", _MockMultiRightsSrc(evidence))
    from proproom.sdk.errors import ProproomError
    with pytest.raises(ProproomError) as exc_info:
        REGISTRY["fetch"](ctx, {"source": "mock_multi", "external_id": "item1"})
    assert exc_info.value.code == "rights_denied"


def test_crashing_policy_plugin_fails_closed(ctx, fake_download):
    class CrashingPolicy:
        def decide(self, ctx_, asset):
            raise RuntimeError("Database connection timed out inside policy plugin")

    pm = _install_source(
        ctx,
        "mock_src",
        _MockMultiRightsSrc([RightsEvidence(basis="license", license_id="CC0-1.0")]),
    )
    pm.plugins["crash_policy"] = LoadedPlugin(
        name="crash_policy",
        version="0.0.1",
        hash="0" * 64,
        types=["policy"],
        origin="dir",
        manifest={},
        instance=CrashingPolicy(),
    )

    from proproom.sdk.errors import ProproomError
    with pytest.raises(ProproomError) as exc_info:
        REGISTRY["fetch"](ctx, {"source": "mock_src", "external_id": "item_policy_crash"})
    assert exc_info.value.code == "rights_denied"
    assert fake_download == []


def test_provenance_conflict_raises_error_strictly(conn):
    conn.execute("BEGIN IMMEDIATE")
    aid1, _ = dedupe.register_asset(conn, slug="asset-1", kind="photo", title="Asset 1")
    aid2, _ = dedupe.register_asset(conn, slug="asset-2", kind="photo", title="Asset 2")

    prov1 = dedupe.register_provenance(
        conn, asset_id=aid1, source="archive", external_id="item-x", revision="rev1"
    )
    assert prov1 is not None

    prov1_again = dedupe.register_provenance(
        conn, asset_id=aid1, source="archive", external_id="item-x", revision="rev1"
    )
    assert prov1 == prov1_again

    from proproom.sdk.errors import ProproomError
    with pytest.raises(ProproomError) as exc_info:
        dedupe.register_provenance(
            conn, asset_id=aid2, source="archive", external_id="item-x", revision="rev1"
        )
    assert exc_info.value.code == "provenance_conflict"
    conn.execute("ROLLBACK")


def test_pull_queued_and_executed_by_worker(ctx, nas, fake_download):
    class SearchableSrc:
        async def search(self, ctx_, req: SearchRequest) -> CandidatePage:
            return CandidatePage(
                items=[
                    Candidate(ref=SourceRef("searchable", "allowed_1"), title="Allowed 1"),
                    Candidate(ref=SourceRef("searchable", "denied_nc"), title="Denied NC"),
                ]
            )

        async def resolve(self, ctx_, ref: SourceRef) -> ResolvedSource:
            if ref.external_id == "denied_nc":
                ev = [RightsEvidence(basis="license", license_id="CC BY-NC 4.0")]
            else:
                ev = [RightsEvidence(basis="license", license_id="CC BY 4.0")]
            return ResolvedSource(
                ref=ref,
                title=ref.external_id,
                kind="photo",
                metadata={"title": ref.external_id},
                evidence=ev,
                offers=[RepresentationOffer("original", "image/jpeg", f"https://example.org/{ref.external_id}.jpg", bytes=500)],
            )

    _install_source(ctx, "searchable", SearchableSrc())

    pull_spec = """
budget: 10M
jobs:
  - source: searchable
    kind: photo
    search: "test query"
"""
    (nas / "pull_test.yaml").write_text(pull_spec)
    res = REGISTRY["pull"](ctx, {"spec": "pull_test.yaml"})
    assert res["state"] == "queued"

    # Worker ticks and drains the job
    worked = worker.tick(ctx)
    assert worked is True
    for _ in range(20):
        if not worker.tick(ctx):
            break
    else:
        raise AssertionError("worker did not become idle")

    c = db.connect(ctx.booted.home.db)
    job_row = c.execute("SELECT state, used_bytes FROM job WHERE id = ?", (res["job_id"],)).fetchone()
    assert job_row["state"] == "succeeded"
    assert job_row["used_bytes"] > 0

    seen_rows = c.execute("SELECT external_id, decision FROM pull_seen ORDER BY external_id").fetchall()
    seen = {r["external_id"]: r["decision"] for r in seen_rows}
    assert seen["allowed_1"] == "taken"
    assert seen["denied_nc"] == "rejected"


def test_principled_source_naming_and_shared_url_disambiguation(ctx, nas):
    d = nas / "project" / "ep_probe5" / "assets"
    d.mkdir(parents=True)
    (d / "fig1.jpg").write_bytes(b"figure 1 bytes")
    (d / "fig2.jpg").write_bytes(b"figure 2 bytes")

    manifest = {
        "assets": [
            {"id": "fig1", "kind": "photo", "file": "assets/fig1.jpg", "license": "CC0", "source": "https://gallery.example.org/article-42"},
            {"id": "fig2", "kind": "photo", "file": "assets/fig2.jpg", "license": "CC0", "source": "https://gallery.example.org/article-42"},
        ]
    }
    (nas / "project" / "ep_probe5" / "assets.json").write_text(json.dumps(manifest))

    REGISTRY["import_project"](ctx, {"project": "project/ep_probe5"})

    c = db.connect(ctx.booted.home.db)
    provs = c.execute("SELECT source, external_id, asset_id FROM provenance WHERE source = 'gallery.example.org'").fetchall()
    assert len(provs) == 2
    assert all(p["source"] == "gallery.example.org" for p in provs)
    ext_ids = {p["external_id"] for p in provs}
    assert len(ext_ids) == 2
    asset_ids = {p["asset_id"] for p in provs}
    assert len(asset_ids) == 2
