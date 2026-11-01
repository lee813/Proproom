"""§8 agent protocol: registry, idempotency, errors, batches, HTTP, MCP."""
from __future__ import annotations

import io
import json
from dataclasses import fields

import pytest
from support import gap, ingest, install_source

from proproom.api import mcp
from proproom.api.registry import REGISTRY
from proproom.core import db
from proproom.core.ops import canonical_hash
from proproom.sdk.errors import ProproomError
from proproom.sdk.types import Candidate, CandidatePage, SourceRef

SIDE_EFFECT_OPS = {"fetch", "link", "share", "pull", "add"}            # §8 幂等
MCP_DEFAULT = {"search", "remote", "fetch", "link", "credits", "share", "share_feedback", "job", "capabilities"}


def test_error_shape():
    """§8: {code, message, hint, retryable, retry_after, details}."""
    assert {f.name for f in fields(ProproomError)} == {"code", "message", "hint", "retryable", "retry_after", "details"}


def test_side_effect_ops_accept_idempotency_key():
    for name in SIDE_EFFECT_OPS:
        op = REGISTRY[name]
        assert op.side_effects and "idempotency_key" in op.schema()["input"]["properties"], name


def test_capabilities_lists_every_operation_with_schemas(ctx):
    caps = REGISTRY["capabilities"](ctx, {})
    assert {o["name"] for o in caps["operations"]} == set(REGISTRY)
    assert all("input" in o and "output" in o for o in caps["operations"])


def test_mcp_default_tool_group_is_small():
    """§8 MCP: only the common tools by default; admin tools are a separate group."""
    assert {op.name for op in REGISTRY.values() if op.mcp == "default"} == MCP_DEFAULT


def test_idempotent_retry_returns_original_result(ctx, nas):
    (nas / "x.png").write_bytes(b"own work")
    first = REGISTRY["add"](ctx, {"paths": ["x.png"], "idempotency_key": "k1"})
    again = REGISTRY["add"](ctx, {"paths": ["x.png"], "idempotency_key": "k1"})
    assert again == first
    with pytest.raises(ProproomError) as e:
        REGISTRY["add"](ctx, {"paths": ["x.png", "y.png"], "idempotency_key": "k1"})
    assert e.value.code == "idempotency_conflict"


def test_retry_while_running_does_not_repeat_side_effects(ctx, nas):
    (nas / "x.png").write_bytes(b"own work")
    args = {"paths": ["x.png"], "idempotency_key": "k-running"}
    c = db.connect(ctx.booted.home.db)
    c.execute("INSERT INTO operation(id, kind, idempotency_key, request_hash, request, state, actor, created) "
              "VALUES ('01JAAAAAAAAAAAAAAAAAAAAAAA', 'add', 'k-running', ?, ?, 'running', 'other', 'now')",
              (canonical_hash(args), json.dumps(args)))
    with pytest.raises(ProproomError) as e:
        REGISTRY["add"](ctx, args)
    assert e.value.retryable
    assert c.execute("SELECT count(*) FROM asset").fetchone()[0] == 0


def test_failed_operation_is_recorded(ctx, fake_download):
    from types import SimpleNamespace

    from proproom.sdk.types import RepresentationOffer

    class Broken:  # resolves fine, then breaks ingest *inside* fetch's write transaction
        async def resolve(self, ctx_, ref):
            offer = RepresentationOffer(role="original", mime="image/jpeg", url="https://example.org/x.jpg")
            return SimpleNamespace(offers=[offer], metadata=None, rights=[], evidence=[])
    install_source(ctx, "broken", Broken())
    with pytest.raises(Exception):
        REGISTRY["fetch"](ctx, {"source": "broken", "external_id": "x", "idempotency_key": "k-fail"})
    c = db.connect(ctx.booted.home.db)
    assert c.execute("SELECT state FROM operation WHERE idempotency_key = 'k-fail'").fetchone()[0] == "failed"


def test_link_many_reports_per_item(ctx, conn, home, nas):
    a = ingest(conn, home, slug="a", title="A")
    (nas / "project" / "ep").mkdir(parents=True)
    res = REGISTRY["link_many"](ctx, {"items": [
        {"assets": ["01NOTANASSETAAAAAAAAAAAAAA"], "project": "project/ep"},
        {"assets": [a.asset_id], "project": "project/ep"}]})
    assert [r.get("status") for r in res["results"]] == ["error", "ok"]


class _Remote:
    async def search(self, ctx_, req):
        return CandidatePage(items=[Candidate(ref=SourceRef("fake", "F1"), title="Cand 1")])


def test_remote_results_are_a_selection(ctx):
    install_source(ctx, "fake", _Remote())
    assert REGISTRY["remote"](ctx, {"source": "fake", "query": "x"}).get("selection_id")


def test_remote_works_over_http(lib, home):
    from fastapi.testclient import TestClient

    from proproom.api import http
    from proproom.core import bootstrap
    pdir = home.dir("plugins") / "fakesrc"
    pdir.mkdir()
    (pdir / "plugin.toml").write_text('[plugin]\nname = "fakesrc"\nversion = "0.0.1"\ntypes = ["source"]\n')
    (pdir / "__init__.py").write_text(
        "from proproom.sdk.types import Candidate, CandidatePage, SourceRef\n"
        "class Plugin:\n"
        "    async def search(self, ctx, req):\n"
        "        return CandidatePage(items=[Candidate(ref=SourceRef('fakesrc', 'A'), title='a')])\n")
    with TestClient(http.app, raise_server_exceptions=False) as client:
        tok = bootstrap.read_token(home)
        r = client.post("/v1/ops/remote", json={"source": "fakesrc", "query": "x"},
                        headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text


def _mcp_roundtrip(monkeypatch, *messages):
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(json.dumps(m) + "\n" for m in messages)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    mcp.run_mcp_server()
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_mcp_lists_default_tools_only(lib, monkeypatch):
    resp = _mcp_roundtrip(monkeypatch, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert {t["name"] for t in resp[0]["result"]["tools"]} == MCP_DEFAULT


def test_mcp_structured_results(lib, monkeypatch):
    resp = _mcp_roundtrip(monkeypatch,
                          {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                          {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "capabilities", "arguments": {}}})
    assert all("outputSchema" in t for t in resp[0]["result"]["tools"])
    assert "structuredContent" in resp[1]["result"]


def test_mcp_errors_use_standard_shape(lib, monkeypatch):
    resp = _mcp_roundtrip(monkeypatch, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "search", "arguments": {"query": "x", "k": "bad"}}})
    res = resp[0]["result"]
    assert res["isError"] and "code" in res.get("structuredContent", {}).get("error", {})
