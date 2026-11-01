"""Regression coverage for task R's safety contracts."""
import asyncio
import logging
import pytest
from proproom.core import eval as evaluation, jobs
from proproom.core.pull import parse_budget
from proproom.plugins.builtin.embedgemma2 import Plugin
from proproom.sdk.context import EmbedContext
from proproom.sdk.errors import ProproomError


def test_invalid_budgets_are_rejected():
    for value in ("lots", "-1", "0", "nan"):
        with pytest.raises(ProproomError):
            parse_budget(value)
    assert parse_budget("1.5K") == 1536


def test_unjudged_queries_are_not_scored():
    with pytest.raises(ValueError):
        evaluation.recall_at_k([], [])
    with pytest.raises(ValueError):
        evaluation.ndcg_at_k([], [])


def test_empty_embedding_input_is_rejected(tmp_path, monkeypatch):
    import sys, types
    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=object))
    ctx = EmbedContext(plugin="embedgemma2", log=logging.getLogger("test"), config={},
                       device="cpu", dtype="fp32", models_dir=str(tmp_path))
    with pytest.raises(ProproomError):
        asyncio.run(Plugin().embed(ctx, [{"prompt": "query"}]))


def test_only_stale_jobs_are_recovered(conn):
    conn.execute("BEGIN IMMEDIATE")
    old = jobs.create_job(conn, "pull", {}, state="running")
    live = jobs.create_job(conn, "import_project", {}, state="running")
    conn.execute("UPDATE job SET heartbeat = '2000-01-01T00:00:00.000Z' WHERE id = ?", (old,))
    jobs.recover_stale_jobs(conn, "2001-01-01T00:00:00.000Z")
    conn.execute("COMMIT")
    assert conn.execute("SELECT state FROM job WHERE id = ?", (old,)).fetchone()[0] == "queued"
    assert conn.execute("SELECT state FROM job WHERE id = ?", (live,)).fetchone()[0] == "running"


def test_index_runs_in_batches_and_finishes(ctx, conn, home, monkeypatch):
    from support import ingest
    from proproom.api.registry import REGISTRY
    from proproom.core import hardware, worker
    from proproom.core.plugins import LoadedPlugin, PluginManager
    from proproom.sdk.types import EmbeddingBatch

    class Embedder:
        fingerprint = {"model": "unit-test", "revision": "1", "modalities": ["image"],
                       "preprocess": {}, "prompts": {}, "pooling": "mean", "normalize": 1, "truncation": 4}
        dim = 4
        calls = []

        async def embed(self, context, inputs):
            self.calls.append(len(inputs))
            return EmbeddingBatch(space_fingerprint="test", vectors=[[1.0, 0, 0, 0] for _ in inputs],
                                  input_hashes=[str(i) for i in range(len(inputs))])

    for i in range(20):
        ingest(conn, home, slug=f"image-{i}", title=f"Image {i}", data=f"image-{i}".encode())
    embedder = Embedder()
    pm = PluginManager(home, "test")
    pm.plugins["unit"] = LoadedPlugin("unit", "1", "0" * 64, ["embedder"], "dir", {}, embedder)
    ctx.plugin_manager = pm
    monkeypatch.setattr(jobs, "should_pause", lambda *args: (False, None))
    monkeypatch.setattr(hardware, "detect", lambda: {"tier": "cpu", "devices": []})
    result = REGISTRY["index"](ctx, {})
    assert result["state"] == "queued" and embedder.calls == []
    assert worker.tick(ctx)
    assert embedder.calls == [16]
    assert conn.execute("SELECT state FROM job WHERE id = ?", (result["id"],)).fetchone()[0] == "running"
    assert worker.tick(ctx)
    assert worker.tick(ctx)
    assert embedder.calls == [16, 4]
    assert conn.execute("SELECT count(*) FROM vec_target").fetchone()[0] == 20
    assert conn.execute("SELECT state FROM job WHERE id = ?", (result["id"],)).fetchone()[0] == "succeeded"
