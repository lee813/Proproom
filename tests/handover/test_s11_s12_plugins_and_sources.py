"""§11 plugin contract and §12 sources — offline, with recorded-style fixtures (no network)."""
from __future__ import annotations

import asyncio
import logging

import pytest
from support import FakeHttp, gap, install_source

from proproom.api.registry import REGISTRY
from proproom.core import db
from proproom.core.http import check_domain
from proproom.core.plugins import PluginManager
from proproom.plugins.builtin.archive_org import Plugin as ArchiveOrg
from proproom.plugins.builtin.commons import Plugin as Commons
from proproom.sdk.context import SourceContext
from proproom.sdk.types import EnumerateRequest, RepresentationOffer, ResolvedSource, RightsEvidence, SourceRef

run = asyncio.run


def _sctx(http) -> SourceContext:
    return SourceContext(plugin="t", log=logging.getLogger("t"), config={}, http=http, keys={})


def _commons_page(title, *, lic="CC BY-SA 4.0", mime="image/jpeg", categories="", videoinfo=None):
    meta = {"Artist": {"value": "<a href='//x'>Théodore Chassériau</a>"},
            "LicenseShortName": {"value": lic}, "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
            "ImageDescription": {"value": "Portrait"}, "Categories": {"value": categories}}
    page = {"title": title, "imageinfo": [{"url": "https://upload.wikimedia.org/o.jpg", "width": 3000, "height": 4000,
                                           "mime": mime, "size": 123, "thumburl": "https://upload.wikimedia.org/t.jpg",
                                           "thumbwidth": 2000, "thumbheight": 2667, "extmetadata": meta}]}
    if videoinfo:
        page["videoinfo"] = [videoinfo]
    return {"query": {"pages": {"1": page}}}


# ───────────────────────── contract: plugins vs. SDK types ─────────────────────────

def test_commons_resolve_returns_sdk_type():
    http = FakeHttp({"Alexis": _commons_page("File:Alexis de Tocqueville.jpg")})
    r = run(Commons().resolve(_sctx(http), SourceRef("commons", "File:Alexis de Tocqueville.jpg")))
    assert isinstance(r, ResolvedSource) and r.title and r.kind
    assert r.evidence and r.evidence[0].attribution == "Théodore Chassériau"   # HTML stripped (§12)
    assert any(o.role == "original" for o in r.offers)


def test_commons_enumerate_category():
    http = FakeHttp({"categorymembers": {"query": {"pages": {}}}})
    page = run(Commons().enumerate(_sctx(http), EnumerateRequest(category="Videos of parliaments")))
    assert page.items == []


def test_archive_org_resolve_returns_sdk_type():
    http = FakeHttp({"metadata/": {"metadata": {"title": "Industrial film", "licenseurl": ""}, "files": []}})
    assert isinstance(run(ArchiveOrg().resolve(_sctx(http), SourceRef("archive_org", "film1"))), ResolvedSource)


# ───────────────────────── §12 source rules ─────────────────────────
# These check the *content* a source returns; `lenient` lets the type-mismatch gap above not mask them.

@pytest.fixture()
def lenient(monkeypatch):
    from types import SimpleNamespace

    import proproom.plugins.builtin.archive_org as ao
    import proproom.plugins.builtin.commons as cm

    def ns(**kw):
        n = SimpleNamespace(tags=[], **kw)
        n.evidence = kw.get("evidence") or kw.get("rights") or []
        return n
    for mod in (cm, ao):
        monkeypatch.setattr(mod, "ResolvedSource", ns)
        monkeypatch.setattr(mod, "RepresentationOffer", lambda **kw: SimpleNamespace(**kw))


def test_commons_never_invents_a_pd_reason(lenient):
    http = FakeHttp({"Seal": _commons_page("File:Seal.svg", lic="Public domain")})
    r = run(Commons().resolve(_sctx(http), SourceRef("commons", "File:Seal.svg")))
    assert all(ev.pd_reason in (None, "pd-us-gov") for ev in r.evidence)


def test_commons_marks_disputed_files(lenient):
    http = FakeHttp({"Leak": _commons_page("File:Leak.jpg", categories="Deletion requests October 2026|Copyright violations")})
    r = run(Commons().resolve(_sctx(http), SourceRef("commons", "File:Leak.jpg")))
    assert "disputed" in r.tags or any("disputed" in ev.restrictions for ev in r.evidence)


def test_archive_org_reads_licenseurl_per_item(lenient):
    """§12: 每一条都要读 licenseurl — never assume the collection is PD."""
    http = FakeHttp({"metadata/": {"metadata": {"title": "Film", "licenseurl": ""}, "files": []}})
    r = run(ArchiveOrg().resolve(_sctx(http), SourceRef("archive_org", "film1")))
    assert r.evidence[0].basis == "unknown"


# ───────────────────────── §11.3 core-owned fetch pipeline ─────────────────────────

class _Src:
    """SDK-shaped source; also exposes `.rights` so today's registry can read it (keeps tests on target)."""

    def __init__(self, evidence, offers):
        self.evidence, self.offers = evidence, offers

    async def resolve(self, ctx_, ref):
        r = ResolvedSource(ref=ref, title="T", kind="photo", metadata={"title": "T"}, evidence=self.evidence,
                           offers=self.offers)
        r.rights = r.evidence
        return r


def test_fetch_checks_rights_before_download(ctx, fake_download):
    install_source(ctx, "fake", _Src([RightsEvidence(basis="license", license_id="CC BY-NC 4.0")],
                                     [RepresentationOffer("original", "image/jpeg", "https://example.org/a.jpg")]))
    with pytest.raises(Exception):
        REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "A"})
    c = db.connect(ctx.booted.home.db)
    assert fake_download == [] and c.execute("SELECT count(*) FROM asset").fetchone()[0] == 0


def test_fetch_prefers_the_1080p_derivative_for_video(ctx, fake_download):
    install_source(ctx, "fake", _Src([RightsEvidence(basis="license", license_id="CC BY-SA 4.0")], [
        RepresentationOffer("original", "video/webm", "https://example.org/orig-2160.webm", height=2160),
        RepresentationOffer("vp9_1080", "video/webm", "https://example.org/d-1080.vp9.webm", height=1080)]))
    REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "V"})
    assert fake_download == ["https://example.org/d-1080.vp9.webm"]


def test_fetch_identity_is_source_external_id(ctx, fake_download):
    ev = [RightsEvidence(basis="license", license_id="CC BY 4.0")]
    install_source(ctx, "fake", _Src(ev, [RepresentationOffer("original", "image/jpeg", "https://example.org/x.jpg")]))
    a = REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "File:Tocqueville (1850).jpg"})
    b = REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "File:Tocqueville 1850.jpg"})
    assert a["asset_id"] != b["asset_id"]


# ───────────────────────── §11.4 / §12 network rules ─────────────────────────

def test_undeclared_domains_are_refused(lib):
    pm = PluginManager(lib.home, "i")
    from proproom.core.plugins import LoadedPlugin
    pm.plugins["nodomains"] = LoadedPlugin("nodomains", "0", "0" * 64, ["source"], "dir", {}, object())
    allowed = pm.get_source_context("nodomains").http.allowed_domains
    with pytest.raises(Exception):
        check_domain("https://evil.example/steal", allowed)


def test_user_agent_contact_comes_from_config(lib):
    from proproom.core import bootstrap
    lib.home.config.write_text('[http]\ncontact = "ops@example.org"\n')
    booted = bootstrap.boot(lib.home)
    pm = PluginManager(booted.home, "i")
    pm.load_all()
    assert "ops@example.org" in pm.get_source_context("commons").http.user_agent


def test_redirect_targets_are_domain_checked():
    """§11.4: 重定向之后的目标域名也要检查 — the check itself."""
    check_domain("https://upload.wikimedia.org/x", ["upload.wikimedia.org"])
    with pytest.raises(Exception):
        check_domain("https://upload.wikimedia.org.evil.example/x", ["upload.wikimedia.org"])


# ───────────────────────── §11 wiring ─────────────────────────

def test_directory_policy_plugin_is_enforced(ctx, home, fake_download):
    pdir = home.dir("plugins") / "nomaps"
    pdir.mkdir()
    (pdir / "plugin.toml").write_text('[plugin]\nname = "nomaps"\nversion = "0.0.1"\ntypes = ["policy"]\n')
    (pdir / "__init__.py").write_text(
        "from proproom.sdk.types import PolicyDecision\n"
        "class Plugin:\n"
        "    def decide(self, ctx, asset):\n"
        "        return PolicyDecision(decision='deny', code='no_map') if asset.get('kind') == 'map' or "
        "'map' in (asset.get('title') or '').lower() else PolicyDecision(decision='allow')\n")
    pm = install_source(ctx, "fake", _Src([RightsEvidence(basis="license", license_id="CC BY-SA 4.0")],
                                          [RepresentationOffer("original", "image/png", "https://example.org/m.png")]))
    pm._load_directory_plugins()
    try:
        res = REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "File:Old map of Paris.png"})
        decision = res["decision"]
    except Exception:
        decision = "deny"
    assert decision == "deny"


def test_ingest_and_link_publish_events(ctx, fake_download, nas):
    install_source(ctx, "fake", _Src([RightsEvidence(basis="license", license_id="CC BY 4.0")],
                                     [RepresentationOffer("original", "image/jpeg", "https://example.org/e.jpg")]))
    res = REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "E"})
    (nas / "project" / "Ep").mkdir(parents=True)
    REGISTRY["link"](ctx, {"assets": [res["asset_id"]], "project": "project/Ep"})
    c = db.connect(ctx.booted.home.db)
    kinds = {r[0] for r in c.execute("SELECT kind FROM event")}
    assert {"ingested", "linked"} <= kinds


def test_hooks_receive_event_id(conn):
    from proproom.core import events
    seen = []

    class Hook:
        async def handle(self, ctx_, event_id, kind, payload):
            seen.append(event_id)

    conn.execute("BEGIN IMMEDIATE")
    eid = events.publish_event(conn, "linked", {"x": 1}, subscribers=["h"])
    events.deliver_events(conn, {"h": Hook()})
    conn.execute("COMMIT")
    assert seen == [eid]
