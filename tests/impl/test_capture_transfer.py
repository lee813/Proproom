"""Source special-transfer contract: core owns the sink and enforces rights."""
import pytest
from tests.handover.support import install_source
from proproom.api.registry import REGISTRY
from proproom.core import db, store
from proproom.sdk.types import RepresentationOffer, ResolvedSource, RightsEvidence
from proproom.sdk.errors import ProproomError


class TransferSource:
    def __init__(self, license_id="CC BY 4.0"):
        self.license_id = license_id
        self.transfers = 0
    async def resolve(self, ctx, ref):
        return ResolvedSource(ref=ref, title="Generated screenshot", kind="screenshot", metadata={},
                              evidence=[RightsEvidence(basis="license", license_id=self.license_id)],
                              offers=[RepresentationOffer("original", "image/png", "https://example.org/page")])
    async def transfer(self, ctx, request, sink):
        self.transfers += 1
        await sink.write(b"image ")
        await sink.write(b"data")


def test_core_receives_special_transfer_after_rights_check(ctx, home):
    src = TransferSource()
    install_source(ctx, "fake", src)
    result = REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "item"})
    assert src.transfers == 1
    assert store.blob_abspath(home, result["sha256"]).read_bytes() == b"image data"


def test_core_refuses_denied_special_transfer(ctx, home):
    src = TransferSource("CC BY-NC 4.0")
    install_source(ctx, "fake", src)
    with pytest.raises(ProproomError) as exc:
        REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "item"})
    assert exc.value.code == "rights_denied" and src.transfers == 0
    conn = db.connect(home.db)
    assert conn.execute("SELECT count(*) FROM blob").fetchone()[0] == 0
    conn.close()


def test_failed_transfer_does_not_leave_partial_file(ctx, home):
    class Broken(TransferSource):
        async def transfer(self, ctx, request, sink):
            await sink.write(b"partial")
            raise RuntimeError("failed")
    src = Broken()
    install_source(ctx, "fake", src)
    with pytest.raises(RuntimeError):
        REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "item"})
    assert not list(home.dir("tmp").glob("transfer_*"))


def test_snapshot_is_persisted_as_provenance_evidence(ctx, home):
    class WithSnapshot(TransferSource):
        async def resolve(self, ctx, ref):
            result = await super().resolve(ctx, ref)
            result.source_url = "https://example.org/requested"
            result.publisher = "Example Publisher"
            result.metadata = {"final_url": "https://example.org/final", "raw": {"viewport": 430}}
            result.offers.append(RepresentationOffer("snapshot", "multipart/related", "https://example.org/final"))
            return result
        async def transfer(self, ctx, request, sink):
            await sink.write(b"snapshot" if request.offer.role == "snapshot" else b"image")
    install_source(ctx, "fake", WithSnapshot())
    result = REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "snapshot"})
    conn = db.connect(home.db)
    row = conn.execute("SELECT source_url, final_url, publisher, snapshot_sha256, raw_metadata FROM provenance WHERE asset_id=?", (result["asset_id"],)).fetchone()
    assert row["source_url"] == "https://example.org/requested" and row["final_url"] == "https://example.org/final"
    assert row["publisher"] == "Example Publisher"
    assert store.blob_abspath(home, row["snapshot_sha256"]).read_bytes() == b"snapshot"
    assert '430' in row["raw_metadata"]
    conn.close()


def test_publisher_policy_checks_whole_publisher_name():
    from proproom.core.rights import evaluate_rights
    assert evaluate_rights({}, "license", license_id="cc-by-4.0", publisher="Example Publisher")[0] == "allow"
    assert evaluate_rights({}, "license", license_id="cc-by-4.0", publisher="AP")[0] == "deny"
    assert evaluate_rights({}, "license", license_id="cc-by-4.0", publisher="Getty Images")[0] == "deny"


def test_byte_limit_counts_screenshot_and_snapshot_together(ctx, home):
    class WithSnapshot(TransferSource):
        async def resolve(self, ctx, ref):
            result = await super().resolve(ctx, ref)
            result.offers.append(RepresentationOffer("snapshot", "multipart/related", "https://example.org/page"))
            return result
        async def transfer(self, ctx, request, sink):
            await sink.write(b"123456")
    install_source(ctx, "fake", WithSnapshot())
    with pytest.raises(ProproomError) as err:
        REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "limited", "max_bytes": 10})
    assert err.value.code == "budget_exceeded"
    assert not list(home.dir("tmp").glob("transfer_*"))


def test_explicit_open_government_licence_is_preserved():
    from proproom.plugins.builtin.capture import _evidence
    url = "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
    evidence = _evidence(url, "https://example.org/page")
    assert evidence.basis == "license" and evidence.license_id == "ogl-uk-3.0"
    assert evidence.license_url == url and evidence.jurisdiction == "GB"


def test_plugin_test_does_not_claim_unrun_contract_passed(ctx):
    install_source(ctx, "fake", TransferSource())
    with pytest.raises(ProproomError) as err:
        REGISTRY["plugin_test"](ctx, {"name": "fake"})
    assert err.value.code == "not_implemented"
    assert not REGISTRY["plugin_test"].implemented


def test_pull_rejects_sources_that_prohibit_bulk(ctx, home, nas, monkeypatch):
    from proproom.core import jobs, worker
    from proproom.core.plugins import LoadedPlugin, PluginManager
    from proproom.plugins.builtin.capture import Plugin
    pm = PluginManager(home, "test")
    pm.plugins["capture"] = LoadedPlugin("capture", "1", "0" * 64, ["source"], "builtin", {"source": {"bulk_allowed": False}}, Plugin())
    ctx.plugin_manager = pm
    monkeypatch.setattr(jobs, "should_pause", lambda *args: (False, None))
    (nas / "no-bulk.yaml").write_text("jobs:\n  - {source: capture, search: 'https://example.org/page'}\n")
    jid = REGISTRY["pull"](ctx, {"spec": "no-bulk.yaml"})["job_id"]
    assert worker.tick(ctx)
    conn = db.connect(home.db)
    row = conn.execute("SELECT state, error FROM job WHERE id = ?", (jid,)).fetchone()
    assert row["state"] == "failed" and "bulk_not_allowed" in row["error"]
    conn.close()


def test_rate_limit_is_shared_across_source_contexts(monkeypatch):
    import asyncio
    import proproom.core.http as http
    waits = []
    async def fake_sleep(delay):
        waits.append(delay)
    monkeypatch.setattr(http.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(http.asyncio, "sleep", fake_sleep)
    first = http.RateLimitedClient("test", rate_per_sec=2, allowed_domains=["example.org"], rate_key="independent-test-source")
    second = http.RateLimitedClient("test", rate_per_sec=2, allowed_domains=["example.org"], rate_key="independent-test-source")
    asyncio.run(first.prepare_request("https://example.org/a"))
    asyncio.run(second.prepare_request("https://example.org/b"))
    assert waits == [0.5]


@pytest.mark.parametrize("method", ["text", "json", "download"])
def test_source_redirect_is_checked_before_contacting_target(home, monkeypatch, method):
    import asyncio
    import httpx
    from proproom.core.download import download_file
    from proproom.core.http import RateLimitedClient
    requests = []
    def handle(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://forbidden.invalid/private"}, request=request)
    transport = httpx.MockTransport(handle)
    original = httpx.AsyncClient
    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    source = RateLimitedClient("test", rate_per_sec=0, allowed_domains=["allowed.invalid"])
    with pytest.raises(ProproomError) as err:
        if method == "text":
            asyncio.run(source.get_text("https://allowed.invalid/start"))
        elif method == "json":
            asyncio.run(source.get_json("https://allowed.invalid/start"))
        else:
            asyncio.run(download_file(home, "https://allowed.invalid/start", allowed_domains=["allowed.invalid"]))
    assert err.value.code == "domain_disallowed"
    assert requests == ["https://allowed.invalid/start"]


def test_quotation_capture_search_is_opt_in_and_reports_review(ctx, conn, home):
    from tests.handover.support import ingest
    item = ingest(conn, home, slug="release", title="Public bulletin", kind="screenshot", basis="quotation", license_id=None)
    without = REGISTRY["search"](ctx, {"query": "bulletin"})["items"]
    with_quotation = REGISTRY["search"](ctx, {"query": "bulletin", "include_quotation": True})["items"]
    assert not any(row["asset_id"] == item.asset_id for row in without)
    match = next(row for row in with_quotation if row["asset_id"] == item.asset_id)
    assert match["decision"] == "review" and "quotation_confirmation" in match["obligations"]


def test_new_plugin_scaffold_is_marked_stub(ctx):
    import tomllib
    from pathlib import Path
    result = REGISTRY["plugin_new"](ctx, {"name": "new-source", "types": ["source"]})
    manifest = tomllib.loads((Path(result["created"]) / "plugin.toml").read_text())
    assert manifest["plugin"]["status"] == "stub"
