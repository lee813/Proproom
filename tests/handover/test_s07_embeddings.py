"""§4.2 / §7 embeddings: real model, index job, query embedding in hybrid search, one space at a time.

Contract this file adds (decided here, consistent with sdk.types / sdk.context):
- An embedder plugin exposes `fingerprint` (dict), `dim` (int) and
  `async embed(ctx: EmbedContext, inputs) -> EmbeddingBatch`, one vector and one input_hash per input.
  Each input is {"text": str} or {"image" | "video" | "audio": local path}, plus "prompt": "query" | "document".
- `_index(ctx)` embeds every active representation that has no fresh vector in the active space,
  using the embedder on `ctx.plugin_manager`; it records a job of kind 'index'.
- `REGISTRY["search"]` embeds the query with prompt "query" whenever an embedder is loaded and a space is active.
- Set PROP_REAL_MODEL=1 to also run the real EmbeddingGemma 2 check (downloads the model, needs the GPU image).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sys
from pathlib import Path

import pytest
from support import gap, ingest

from proproom.api.registry import REGISTRY
from proproom.core.plugins import LoadedPlugin, PluginManager
from proproom.sdk.context import EmbedContext
from proproom.sdk.types import EmbeddingBatch

_AXES = [{"harbour", "port", "docks", "cranes"}, {"portrait", "writer", "painter"}]


class FakeEmbedder:
    """Deterministic 4-d 'semantic' embedder: words on the same axis are near each other."""

    dim = 4

    def __init__(self, revision="1"):
        self.fingerprint = {"model": "test/fake", "revision": revision, "modalities": ["text", "image"],
                            "preprocess": {}, "prompts": {"query": "q", "doc": "d"}, "pooling": "mean",
                            "normalize": 1, "truncation": 4}
        self.calls = 0

    async def embed(self, ctx, inputs):
        vectors, hashes = [], []
        for inp in inputs:
            self.calls += 1
            raw = inp["text"].encode() if "text" in inp else Path(next(inp[k] for k in ("image", "video", "audio")
                                                                    if k in inp)).read_bytes()
            words = set(re.findall(r"[a-z]+", raw.decode(errors="ignore").lower()))
            v = [1.0 if words & axis else 0.0 for axis in _AXES] + [0.0, 0.1]
            n = math.sqrt(sum(x * x for x in v))
            vectors.append([x / n for x in v])
            hashes.append(hashlib.sha256(raw).hexdigest())
        return EmbeddingBatch(space_fingerprint=self.fingerprint, vectors=vectors, input_hashes=hashes)


def _install_embedder(ctx, emb) -> PluginManager:
    pm = getattr(ctx, "plugin_manager", None) or PluginManager(ctx.booted.home, "test-instance")
    pm.plugins = {k: v for k, v in pm.plugins.items() if "embedder" not in v.types}
    pm.plugins["fake_embed"] = LoadedPlugin("fake_embed", "0.0.1", "0" * 64, ["embedder"], "dir", {}, emb)
    ctx.plugin_manager = pm
    return pm


def _index(ctx) -> None:
    """Queue an index job (it returns `queued`, see test_round3_findings.py) and run the worker until idle."""
    from proproom.core import worker
    assert REGISTRY["index"](ctx, {})["state"] == "queued"
    for _ in range(50):
        if not worker.tick(ctx):
            return
    raise AssertionError("worker never went idle")


@pytest.fixture()
def corpus(conn, home):
    port = ingest(conn, home, slug="rotterdam", title="Rotterdam at dawn", data=b"port docks cranes")
    face = ingest(conn, home, slug="writer", title="Study of a man", data=b"portrait writer")
    return port, face


# ───────────────────────── the built-in embedder ─────────────────────────

def test_builtin_embedder_never_fakes_vectors(monkeypatch, tmp_path):
    from proproom.plugins.builtin.embedgemma2 import Plugin
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # model stack unavailable → must refuse
    ctx = EmbedContext(plugin="embedgemma2", log=logging.getLogger("t"), config={}, device="cpu", dtype="fp32",
                       models_dir=str(tmp_path))
    from proproom.sdk.errors import ProproomError
    with pytest.raises(ProproomError):  # a typed refusal, not a crash or a vector
        asyncio.run(Plugin().embed(ctx, [{"text": "harbour", "prompt": "query"}]))


_FAKE_ST = """
import numpy as np
LOADS, CALLS = [], []
class SentenceTransformer:
    def __init__(self, name, *a, **kw):
        LOADS.append(name)
    def encode(self, x, prompt_name=None, **kw):
        CALLS.append((x, prompt_name))
        n = len(x) if isinstance(x, list) else None
        return np.ones((n, 768), dtype="float32") if n is not None else np.ones(768, dtype="float32")
"""


@pytest.fixture()
def fake_st(tmp_path, monkeypatch):
    """A sentence_transformers package on sys.path that has NOT been imported yet, like a fresh worker process."""
    pkg = tmp_path / "st" / "sentence_transformers"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(_FAKE_ST)
    monkeypatch.syspath_prepend(str(pkg.parent))
    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
    yield lambda: sys.modules["sentence_transformers"]
    sys.modules.pop("sentence_transformers", None)


def _ectx(tmp_path):
    return EmbedContext(plugin="embedgemma2", log=logging.getLogger("t"), config={}, device="cpu", dtype="fp32",
                        models_dir=str(tmp_path))


def test_builtin_embedder_imports_and_keeps_the_model(fake_st, tmp_path):
    """§4.2: loaded through sentence-transformers once, kept loaded between batches, 256-d output."""
    from proproom.plugins.builtin.embedgemma2 import Plugin
    p = Plugin()
    b1 = asyncio.run(p.embed(_ectx(tmp_path), [{"text": "harbour", "prompt": "query"}]))
    asyncio.run(p.embed(_ectx(tmp_path), [{"text": "cranes", "prompt": "query"}]))
    assert [len(v) for v in b1.vectors] == [256]
    assert fake_st().LOADS == ["google/embeddinggemma-2"]


def test_builtin_embedder_uses_the_handover_prompts(fake_st, tmp_path):
    from proproom.plugins.builtin.embedgemma2 import Plugin
    asyncio.run(Plugin().embed(_ectx(tmp_path), [{"text": "harbour", "prompt": "query"},
                                                 {"text": "a port", "prompt": "document"}]))
    assert {pn for _, pn in fake_st().CALLS} == {"SearchQuery", "Document"}


def test_builtin_embedder_encodes_media_not_empty_text(fake_st, tmp_path):
    """§7: media are encoded with model.encode({"image": p}); input_hash covers the file bytes."""
    from proproom.plugins.builtin.embedgemma2 import Plugin
    img = tmp_path / "a.jpg"
    img.write_bytes(b"jpeg bytes")
    batch = asyncio.run(Plugin().embed(_ectx(tmp_path), [{"image": str(img), "prompt": "document"}]))
    sent = [x for x, _ in fake_st().CALLS]
    flat = [y for x in sent for y in (x if isinstance(x, list) else [x])]
    assert any(isinstance(y, dict) and "image" in y for y in flat)
    assert batch.input_hashes == [hashlib.sha256(b"jpeg bytes").hexdigest()]


@pytest.mark.skipif(os.environ.get("PROP_REAL_MODEL") != "1", reason="set PROP_REAL_MODEL=1 to load EmbeddingGemma 2")
def test_embeddinggemma2_real_model(tmp_path):
    """§4.2: 256-d MRL-truncated, normalised, and the query prompt ranks the matching document first."""
    from proproom.plugins.builtin.embedgemma2 import Plugin
    from proproom.core import hardware
    hw = hardware.detect()
    gpu = hw["tier"] == "nvidia"
    ctx = EmbedContext(plugin="embedgemma2", log=logging.getLogger("t"), config={},
                       device="cuda" if gpu else "cpu", dtype=hw["devices"][0]["dtype"] if gpu else "fp32",
                       models_dir=str(tmp_path))
    p = Plugin()
    batch = asyncio.run(p.embed(ctx, [{"text": "harbour cranes at night", "prompt": "query"},
                                      {"text": "container port with gantry cranes", "prompt": "document"},
                                      {"text": "oil portrait of a nineteenth-century writer", "prompt": "document"}]))
    q, near, far = batch.vectors
    assert len(q) == 256 and abs(sum(x * x for x in q) - 1) < 1e-3
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
    assert dot(q, near) > dot(q, far)
    assert len(batch.input_hashes) == 3 and len(set(batch.input_hashes)) == 3


# ───────────────────────── index job ─────────────────────────

def test_index_writes_vectors_with_input_hash(ctx, conn, corpus):
    _install_embedder(ctx, FakeEmbedder())
    _index(ctx)
    space = conn.execute("SELECT id, vec_table FROM embedding_space WHERE active = 1").fetchone()
    rows = conn.execute("SELECT input_hash, stale FROM vec_target WHERE space_id = ?", (space["id"],)).fetchall()
    assert len(rows) == 2 and all(r["input_hash"] and r["stale"] == 0 for r in rows)
    assert conn.execute(f"SELECT count(*) FROM {space['vec_table']}").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM job WHERE kind = 'index' AND state = 'succeeded'").fetchone()[0] == 1


def test_index_is_incremental(ctx, conn, home, corpus):
    emb = FakeEmbedder()
    _install_embedder(ctx, emb)
    _index(ctx)
    first = emb.calls
    _index(ctx)
    assert emb.calls == first
    ingest(conn, home, slug="docks", title="Antwerp", data=b"docks")
    _index(ctx)
    assert emb.calls == first + 1


class _Picky(FakeEmbedder):
    """Fails any batch holding an unreadable file, like a real image decoder does."""

    def __init__(self, bad=b"corrupt"):
        super().__init__()
        self.bad, self.seen = bad, []

    async def embed(self, ctx, inputs):
        for inp in inputs:
            raw = inp["text"].encode() if "text" in inp else Path(next(inp[k] for k in ("image", "video", "audio")
                                                                    if k in inp)).read_bytes()
            self.seen.append(raw)
            if self.bad in raw:
                raise RuntimeError("Unsupported or unrecognized image format")
        return await super().embed(ctx, inputs)


def _index_job(conn):
    return conn.execute("SELECT state, progress FROM job WHERE kind = 'index' ORDER BY created DESC").fetchone()


def test_one_unreadable_file_does_not_stop_the_index(ctx, conn, home, corpus):
    ingest(conn, home, slug="broken", title="Broken", data=b"corrupt bytes")
    _install_embedder(ctx, _Picky())
    _index(ctx)
    job = _index_job(conn)
    progress = json.loads(job["progress"])
    assert job["state"] == "succeeded" and progress["indexed"] == 2
    assert [s["reason"] for s in progress["skipped"]] == ["embed_failed"]
    assert "unrecognized image format" in progress["skipped"][0]["error"]


def test_an_embedder_that_fails_on_everything_fails_the_job(ctx, conn, corpus):
    _install_embedder(ctx, _Picky(bad=b""))
    _index(ctx)
    assert _index_job(conn)["state"] == "failed"


def test_the_blob_mime_not_the_asset_kind_picks_the_input(ctx, conn, home, corpus):
    ingest(conn, home, slug="world", title="World", kind="photo", mime="application/octet-stream",
           data=b'{"type": "FeatureCollection"}')
    emb = _Picky()
    _install_embedder(ctx, emb)
    _index(ctx)
    assert not any(b"FeatureCollection" in raw for raw in emb.seen)
    assert "unsupported_mime" in [s["reason"] for s in json.loads(_index_job(conn)["progress"])["skipped"]]


def test_a_new_fingerprint_gets_its_own_space(ctx, conn, corpus):
    """§7: 不同 embedding_space 的向量不能混在一起搜 — a revision change is a new space."""
    _install_embedder(ctx, FakeEmbedder(revision="1"))
    _index(ctx)
    _install_embedder(ctx, FakeEmbedder(revision="2"))
    _index(ctx)
    spaces = conn.execute("SELECT id, vec_table, active FROM embedding_space ORDER BY id").fetchall()
    assert len(spaces) == 2 and [s["active"] for s in spaces] == [0, 1]
    assert spaces[0]["vec_table"] != spaces[1]["vec_table"]
    assert conn.execute("SELECT count(*) FROM vec_target WHERE space_id = ?", (spaces[1]["id"],)).fetchone()[0] == 2


# ───────────────────────── hybrid search ─────────────────────────

def test_search_finds_by_meaning_not_just_words(ctx, corpus):
    """§7: 'harbour' shares no word with 'Rotterdam at dawn'; only the vector channel can find it."""
    port, _ = corpus
    _install_embedder(ctx, FakeEmbedder())
    _index(ctx)
    items = REGISTRY["search"](ctx, {"query": "harbour"})["items"]
    assert items and items[0]["asset_id"] == port.asset_id
    assert "vec" in items[0]["ranks"]


def test_vector_recall_respects_filters(ctx, conn, home, corpus):
    """§7: filters apply at recall time in every channel, including KNN."""
    ingest(conn, home, slug="nc-port", title="Hamburg", data=b"harbour cranes", license_id="cc-by-nc-4.0")
    _install_embedder(ctx, FakeEmbedder())
    _index(ctx)
    port, _ = corpus
    ids = [i["asset_id"] for i in REGISTRY["search"](ctx, {"query": "harbour", "kind": ["photo"]})["items"]]
    assert ids[0] == port.asset_id and len(ids) == len(set(ids))
    assert all(conn.execute("SELECT decision FROM asset_rights WHERE asset_id = ?", (i,)).fetchone()[0] == "allow"
               for i in ids)
