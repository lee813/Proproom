"""Helpers for the handover-conformance suite (imported by the tests; fixtures live in conftest.py)."""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from proproom.api.registry import OpContext
from proproom.core import dedupe, store
from proproom.core.plugins import LoadedPlugin, PluginManager


def policy_rules(conn) -> dict:
    return json.loads(conn.execute("SELECT rules FROM policy ORDER BY version DESC LIMIT 1").fetchone()["rules"])


@dataclass
class Ingested:
    asset_id: str
    rid: int
    rep_id: str
    sha: str
    assessment_id: str


def ingest(conn, home, *, slug: str, title: str, kind: str = "photo", data: bytes | None = None,
           mime: str = "image/jpeg", width: int = 2000, height: int = 2000, basis: str = "license",
           license_id: str | None = "cc-by-sa-4.0", pd_reason: str | None = None, attribution: str = "Author",
           source: str = "manual", external_id: str | None = None, cjk_text: str | None = None,
           synthetic: bool = False, extra_reps: list[dict] | None = None) -> Ingested:
    """Ingest one asset through core's own registration functions, in one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        sha, size, rel = store.put_bytes(home, data if data is not None else f"bytes:{slug}".encode(), mime)
        dedupe.register_blob(conn, sha, size, mime, stored=True, storage_path=rel)
        aid, rid = dedupe.register_asset(conn, slug=slug, kind=kind, title=title, synthetic=synthetic)
        rep = dedupe.register_representation(conn, asset_id=aid, blob_sha256=sha, role="original",
                                             width=width, height=height)
        for r in extra_reps or []:
            s2, n2, rel2 = store.put_bytes(home, r["data"], r.get("mime", mime))
            dedupe.register_blob(conn, s2, n2, r.get("mime", mime), stored=True, storage_path=rel2)
            dedupe.register_representation(conn, asset_id=aid, blob_sha256=s2, role=r["role"],
                                           width=r.get("width"), height=r.get("height"))
        prov = dedupe.register_provenance(conn, asset_id=aid, source=source, external_id=external_id or slug,
                                          source_url=f"https://example.org/{slug}")
        _, ass = dedupe.register_rights(conn, provenance_id=prov, asset_id=aid, policy_version=1,
                                        policy_rules=policy_rules(conn), basis=basis, license_id=license_id,
                                        pd_reason=pd_reason, attribution=attribution, synthetic=synthetic)
        dedupe.sync_search_doc(conn, rid=rid, title=title, artist=attribution, cjk_text=cjk_text)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return Ingested(aid, rid, rep, sha, ass)


class FakeHttp:
    """Offline stand-in for core's RateLimitedClient: replays fixtures keyed by a substring of the URL/params."""

    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[tuple[str, dict | None]] = []
        self.user_agent = "test-agent"
        self.allowed_domains: list[str] = []

    async def get_json(self, url, params=None, **_):
        self.calls.append((url, params))
        key = f"{url}?{json.dumps(params, sort_keys=True) if params else ''}"
        for needle, resp in self.responses.items():
            if needle in key:
                return resp
        raise AssertionError(f"no fixture for {key}")

    get_text = get_json


def install_source(ctx: OpContext, name: str, instance, domains: dict | None = None) -> PluginManager:
    """Register an in-memory source plugin on this OpContext (no network, no plugin dir)."""
    pm = PluginManager(ctx.booted.home, "test-instance")
    pm.plugins[name] = LoadedPlugin(name=name, version="0.0.1", hash="0" * 64, types=["source"], origin="dir",
                                    manifest={"domains": domains or {"download": ["example.org"]}},
                                    instance=instance)
    ctx.plugin_manager = pm
    return pm


def gap(finding: str):
    return pytest.mark.xfail(strict=True, reason=f"handover gap: {finding}")
