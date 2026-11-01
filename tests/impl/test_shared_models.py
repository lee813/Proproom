"""One copy of each model per container, on the tier's dtype, released when idle; no silent CPU fallback when the
GPU goes away; imports and searches that never hold the write lock through slow work (handover §4.2, §5.3, §8)."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from support import ingest

from proproom.api import cli, client, http
from proproom.api.registry import REGISTRY, OpContext
from proproom.core import bootstrap, db, embedding, hardware, importer, jobs, search, sheet, store, worker
from proproom.core.plugins import LoadedPlugin, PluginManager
from proproom.sdk.context import EmbedContext
from proproom.sdk.errors import ProproomError
from tests.handover.test_s07_embeddings import FakeEmbedder, _index, _install_embedder

_FAKE_ST = """
import numpy as np
LOADS = []
class SentenceTransformer:
    def __init__(self, name, *a, **kw):
        LOADS.append((name, kw))
    def encode(self, x, prompt_name=None, **kw):
        return np.ones(768, dtype="float32")
"""


@pytest.fixture()
def fake_st(tmp_path, monkeypatch):
    pkg = tmp_path / "st" / "sentence_transformers"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(_FAKE_ST)
    monkeypatch.syspath_prepend(str(pkg.parent))
    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
    yield lambda: sys.modules["sentence_transformers"]
    sys.modules.pop("sentence_transformers", None)


def _ectx(tmp_path, dtype="fp32", device="cpu"):
    return EmbedContext(plugin="embedgemma2", log=logging.getLogger("t"), config={}, device=device, dtype=dtype,
                        models_dir=str(tmp_path))


def _write_lock_free(home) -> bool:
    """True when another connection can take the write lock right now, without waiting."""
    other = db.connect(home.db)
    try:
        other.execute("PRAGMA busy_timeout=0")
        try:
            other.execute("BEGIN IMMEDIATE")
        except Exception as e:  # noqa: BLE001 - the probe's answer
            if "locked" in str(e):
                return False
            raise
        other.execute("ROLLBACK")
        return True
    finally:
        other.close()


# ───────────────────────── embedder: dtype by tier, release ─────────────────────────

@pytest.mark.parametrize("dtype,torch_name", [("bf16", "bfloat16"), ("fp16", "float16"), ("fp32", "float32")])
def test_embedder_loads_in_the_tiers_dtype(fake_st, tmp_path, dtype, torch_name):
    """§4.2: bf16 on Ampere+, fp16 on Turing, fp32 on CPU; low_cpu_mem_usage while loading."""
    import torch
    from proproom.plugins.builtin.embedgemma2 import Plugin
    asyncio.run(Plugin().embed(_ectx(tmp_path, dtype), [{"text": "harbour", "prompt": "query"}]))
    (_, kw), = fake_st().LOADS
    assert kw["model_kwargs"] == {"dtype": getattr(torch, torch_name), "low_cpu_mem_usage": True}


def test_embedder_refuses_a_dtype_it_has_no_build_for(fake_st, tmp_path):
    from proproom.plugins.builtin.embedgemma2 import Plugin
    with pytest.raises(ProproomError) as e:
        asyncio.run(Plugin().embed(_ectx(tmp_path, "int8"), [{"text": "harbour", "prompt": "query"}]))
    assert e.value.code == "unsupported_dtype"


def test_released_embedder_loads_again_on_the_next_call(fake_st, tmp_path):
    from proproom.plugins.builtin.embedgemma2 import Plugin
    from proproom.sdk.protocols import HoldsModel
    p = Plugin()
    assert isinstance(p, HoldsModel)
    asyncio.run(p.embed(_ectx(tmp_path), [{"text": "a", "prompt": "query"}]))
    p.release()
    assert p._model is None
    asyncio.run(p.embed(_ectx(tmp_path), [{"text": "b", "prompt": "query"}]))
    assert len(fake_st().LOADS) == 2


def test_regions_detector_can_be_released():
    from proproom.plugins.builtin.regions import Plugin
    from proproom.sdk.protocols import HoldsModel
    p = Plugin()
    assert isinstance(p, HoldsModel)
    p.release()  # nothing loaded: a no-op


class _Holder:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1


def test_release_idle_frees_only_models_unused_for_the_idle_time(home):
    pm = PluginManager(home, "t")
    holder, plain = _Holder(), object()
    pm.plugins = {"h": LoadedPlugin("h", "1", "0" * 64, ["embedder"], "dir", {}, holder),
                  "p": LoadedPlugin("p", "1", "0" * 64, ["analyzer"], "dir", {}, plain)}
    pm.mark_used("h")
    pm.mark_used("p")
    assert pm.release_idle(3600) == []
    assert pm.release_idle(0) == ["h"] and holder.released == 1
    assert pm.release_idle(0) == []  # not used since: nothing more to release


class _HoldingEmbedder(FakeEmbedder):
    def release(self):
        pass


def test_search_and_index_count_as_use_of_the_embedder(ctx, conn, home):
    ingest(conn, home, slug="port", title="Rotterdam port", data=b"port docks cranes")
    pm = _install_embedder(ctx, _HoldingEmbedder())
    _index(ctx)
    assert pm.release_idle(0) == ["fake_embed"]
    REGISTRY["search"](ctx, {"query": "cranes"})
    assert pm.release_idle(0) == ["fake_embed"]


# ───────────────────────── GPU loss ─────────────────────────

@pytest.fixture()
def nvidia_tier(monkeypatch):
    monkeypatch.setattr(hardware, "detect", lambda: {"tier": "nvidia", "devices": [{"dtype": "bf16"}], "cores": 2})


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens any file")
def test_device_nodes_that_cannot_be_opened_mean_the_gpu_is_lost(nvidia_tier, tmp_path):
    ok, denied = tmp_path / "nvidiactl", tmp_path / "nvidia0"
    ok.write_text("")
    denied.write_text("")
    denied.chmod(0)
    assert hardware.gpu_lost([str(ok)]) == []
    assert hardware.gpu_lost([str(ok), str(denied)]) == [str(denied)]
    assert hardware.gpu_lost([str(tmp_path / "gone")]) == [str(tmp_path / "gone")]


def test_a_cpu_tier_process_never_reports_a_lost_gpu(monkeypatch, tmp_path):
    monkeypatch.setattr(hardware, "detect", lambda: {"tier": "cpu", "devices": [], "cores": 2})
    assert hardware.gpu_lost([str(tmp_path / "gone")]) == []


@pytest.fixture()
def gpu_gone(nvidia_tier, monkeypatch):
    monkeypatch.setattr(hardware, "gpu_lost", lambda nodes=None: ["/dev/nvidia0"])


def test_the_worker_does_no_work_without_its_gpu(ctx, gpu_gone):
    with pytest.raises(ProproomError) as e:
        worker.tick(ctx)
    assert e.value.code == "gpu_lost" and e.value.retryable


def test_search_reports_a_lost_gpu_instead_of_quietly_dropping_to_keywords(ctx, conn, home, monkeypatch):
    ingest(conn, home, slug="port", title="Rotterdam port", data=b"port docks cranes")
    _install_embedder(ctx, FakeEmbedder())
    _index(ctx)
    monkeypatch.setattr(hardware, "detect", lambda: {"tier": "nvidia", "devices": [{"dtype": "bf16"}], "cores": 2})
    monkeypatch.setattr(hardware, "gpu_lost", lambda nodes=None: ["/dev/nvidia0"])
    with pytest.raises(ProproomError) as e:
        REGISTRY["search"](ctx, {"query": "cranes"})
    assert e.value.code == "gpu_lost"


def test_capabilities_warns_about_a_lost_gpu(ctx, gpu_gone):
    assert "gpu_lost" in REGISTRY["capabilities"](ctx, {"schemas": False})["warnings"]


def test_propd_reports_unhealthy_and_stops_when_the_gpu_is_lost(lib, monkeypatch):
    from fastapi.testclient import TestClient

    def tick(_ctx):
        raise ProproomError("gpu_lost", "The GPU is no longer reachable (/dev/nvidia0)", retryable=True)

    monkeypatch.setattr(worker, "tick", tick)
    server = SimpleNamespace(should_exit=False)
    monkeypatch.setitem(http._state, "server", server)
    with TestClient(http.app) as c:
        for _ in range(50):
            if server.should_exit:
                break
            time.sleep(0.1)
        assert server.should_exit and http._state["gpu_lost"]
        monkeypatch.setattr(hardware, "gpu_lost", lambda nodes=None: ["/dev/nvidia0"])
        r = c.get("/healthz")
        assert r.status_code == 503 and r.json()["gpu_lost"] == ["/dev/nvidia0"]
    http._state.pop("gpu_lost", None)


# ───────────────────────── import race, write lock ─────────────────────────

def _episode(nas, name, files):
    ep = nas / "project" / name
    (ep / "assets").mkdir(parents=True)
    for rel, data in files.items():
        (ep / rel).write_bytes(data)
    (ep / "assets.json").write_text(json.dumps({"assets": [
        {"id": rel.split("/")[-1].split(".")[0], "kind": "photo", "file": rel, "license": "CC0"} for rel in files]}))


def test_an_import_copies_files_without_holding_the_write_lock(ctx, nas, home, monkeypatch):
    """§5.3: a large video takes seconds to copy; other writers must not wait (or fail) behind it."""
    _episode(nas, "EpLock", {"assets/a.jpg": b"a", "assets/b.jpg": b"b"})
    real, free = store.put_file, []

    def put_file(*a, **kw):
        free.append(_write_lock_free(home))
        return real(*a, **kw)

    monkeypatch.setattr(store, "put_file", put_file)
    assert REGISTRY["import_project"](ctx, {"project": "project/EpLock"})["progress"]["imported"] == 2
    assert free == [True, True]


def test_the_worker_leaves_an_import_running_in_another_call_alone(ctx, nas, home, monkeypatch):
    """The live import race: the worker found the inline job and failed it with `not_implemented`."""
    _episode(nas, "EpRace", {"assets/a.jpg": b"a"})
    real = store.put_file

    def put_file(*a, **kw):
        while worker.tick(ctx):  # the container's worker, running while the import is under way
            pass
        return real(*a, **kw)

    monkeypatch.setattr(store, "put_file", put_file)
    res = REGISTRY["import_project"](ctx, {"project": "project/EpRace"})
    c = db.connect(home.db)
    assert tuple(c.execute("SELECT state, error FROM job WHERE id = ?", (res["job_id"],)).fetchone()) == ("succeeded", None)


def test_a_queued_job_nobody_steps_still_fails_instead_of_waiting_forever(ctx, conn):
    conn.execute("BEGIN IMMEDIATE")
    jid = jobs.create_job(conn, "mystery", {})
    conn.execute("COMMIT")
    worker.tick(ctx)
    assert conn.execute("SELECT state FROM job WHERE id = ?", (jid,)).fetchone()[0] == "failed"


def test_search_embeds_and_renders_the_sheet_without_the_write_lock(ctx, conn, home, monkeypatch):
    """The first query in a process loads the model (seconds), and a sheet decodes video frames."""
    ingest(conn, home, slug="port", title="Rotterdam port", data=b"port docks cranes")
    seen = []

    class _Probe(FakeEmbedder):
        async def embed(self, ectx, inputs):
            if inputs and inputs[0].get("prompt") == "query":
                seen.append(("embed", _write_lock_free(home)))
            return await super().embed(ectx, inputs)

    _install_embedder(ctx, _Probe())
    _index(ctx)
    real = sheet.render_contact_sheet

    def render(cells, out):
        seen.append(("sheet", _write_lock_free(home)))
        return real(cells, out)

    monkeypatch.setattr(sheet, "render_contact_sheet", render)
    res = REGISTRY["search"](ctx, {"query": "cranes", "sheet": True})
    assert seen == [("embed", True), ("sheet", True)]
    assert conn.execute("SELECT count(*) FROM selection_item WHERE selection_id = ?",
                        (res["selection_id"],)).fetchone()[0] == len(res["items"]) >= 1


# ───────────────────────── one propd, CLI and MCP forward to it ─────────────────────────

@pytest.fixture()
def propd(lib, monkeypatch):
    """A real propd on a free port, serving the test library."""
    import uvicorn
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(http.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    monkeypatch.delenv("PROP_LOCAL")
    monkeypatch.setenv("PROP_SERVER", f"http://127.0.0.1:{port}")
    yield http
    server.should_exit = True
    t.join(10)


@pytest.fixture()
def no_local_boot(monkeypatch):
    """After propd has started: a CLI or MCP process that booted the library itself would fail."""
    def boot(*a, **kw):
        raise AssertionError("ran the operation in-process instead of forwarding it to propd")
    monkeypatch.setattr(bootstrap, "boot", boot)


def test_the_cli_forwards_operations_to_propd(propd, no_local_boot, lib, capsys):
    cli.main(["search", "anything"])
    res = json.loads(capsys.readouterr().out)
    c = db.connect(lib.home.db)
    assert c.execute("SELECT created_by FROM selection WHERE id = ?", (res["selection_id"],)).fetchone()[0] == "cli"


def test_forwarded_errors_keep_their_code_and_exit_status(propd, no_local_boot, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["job", "get", "NOPE"])
    assert e.value.code == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "job_not_found"


def test_propd_shares_one_plugin_manager_across_requests(propd, lib, monkeypatch):
    loads = []
    monkeypatch.setattr(PluginManager, "load_all", lambda self, conn=None: loads.append(1))
    server = client.find("cli")
    server.call("capabilities", {"schemas": False})
    server.call("search", {"query": "anything"})
    assert loads == []
    assert propd._state["pm"] is not None


def test_the_mcp_server_forwards_tool_calls_to_propd(propd, no_local_boot, monkeypatch, capsys):
    from proproom.api.mcp import run_mcp_server
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "capabilities", "arguments": {"schemas": False}}}]
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)))
    run_mcp_server()
    out = json.loads(capsys.readouterr().out.splitlines()[0])
    assert out["result"]["isError"] is False
    assert out["result"]["structuredContent"]["instance_id"] == propd._state["instance_id"]


def test_with_no_propd_running_the_cli_runs_the_operation_itself(lib, monkeypatch, capsys):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here
    monkeypatch.delenv("PROP_LOCAL")
    monkeypatch.setenv("PROP_SERVER", f"http://127.0.0.1:{port}")
    assert client.find("cli") is None
    cli.main(["search", "anything"])
    assert "selection_id" in json.loads(capsys.readouterr().out)


def test_a_propd_for_another_library_is_not_used(propd, monkeypatch):
    monkeypatch.setattr(client, "_instance_id", lambda home: "another-library")
    assert client.find("cli") is None


def test_a_propd_that_lost_its_gpu_is_reported_not_bypassed(propd, monkeypatch):
    monkeypatch.setattr(hardware, "gpu_lost", lambda nodes=None: ["/dev/nvidia0"])
    with pytest.raises(ProproomError) as e:
        client.find("cli")
    assert e.value.code == "server_unavailable" and e.value.retryable


def test_add_paths_are_resolved_where_prop_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli._parse_args("add", ["a.jpg"])["paths"] == [str(tmp_path / "a.jpg")]


# ───────────────────────── the real model on the GPU ─────────────────────────

@pytest.mark.skipif(os.environ.get("PROP_REAL_MODEL") != "1", reason="set PROP_REAL_MODEL=1 to load EmbeddingGemma 2")
def test_real_embedder_uses_the_tier_dtype_and_gives_the_memory_back(tmp_path):
    import torch
    from proproom.plugins.builtin.embedgemma2 import Plugin
    hardware.detect.cache_clear()
    device, dtype = hardware.embed_device()
    if device != "cuda":
        pytest.skip("needs the nvidia tier")
    p = Plugin()
    models = os.environ.get("PROP_MODELS_DIR", str(tmp_path))  # a cache with the weights saves a 1.5 GB download
    asyncio.run(p.embed(_ectx(models, dtype, device), [{"text": "harbour cranes", "prompt": "query"}]))
    assert next(p._model.parameters()).dtype == {"bf16": torch.bfloat16, "fp16": torch.float16}[dtype]
    assert torch.cuda.memory_allocated() > 2**30
    p.release()
    assert torch.cuda.memory_allocated() < 2**27
