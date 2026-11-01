"""Embedding spaces: fingerprint, one vec0 table per space, input_hash staleness, never mix spaces.

Handover §7.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .ids import utcnow


def space_fingerprint(
    model: str,
    revision: str,
    modalities: list[str],
    preprocess: dict[str, Any],
    prompts: dict[str, str],
    pooling: str,
    normalize: bool,
    dim: int,
    metric: str = "cosine",
) -> str:
    canonical = json.dumps({
        "model": model,
        "revision": revision,
        "modalities": sorted(modalities),
        "preprocess": preprocess,
        "prompts": prompts,
        "pooling": pooling,
        "normalize": 1 if normalize else 0,
        "dim": dim,
        "metric": metric,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ensure_embedding_space(
    conn: sqlite3.Connection,
    emb_or_model: Any = "google/embeddinggemma-2",
    revision: str = "main",
    modalities: list[str] | None = None,
    dim: int = 256,
    metric: str = "cosine",
) -> tuple[int, str]:
    """Ensure embedding space row and corresponding vec0 table exist. Returns (space_id, vec_table)."""
    if hasattr(emb_or_model, "fingerprint") or isinstance(emb_or_model, dict):
        fp_dict = emb_or_model.fingerprint if hasattr(emb_or_model, "fingerprint") else emb_or_model
        dim = getattr(emb_or_model, "dim", fp_dict.get("truncation", 256))
        metric = fp_dict.get("metric", "cosine")
        model = fp_dict.get("model", "unknown")
        rev = str(fp_dict.get("revision", "1"))
        mods = fp_dict.get("modalities", ["text", "image", "video"])
        prep = fp_dict.get("preprocess", {})
        prompts = fp_dict.get("prompts", {"query": "SearchQuery", "doc": "Document"})
        pool = fp_dict.get("pooling", "mean")
        norm = 1 if fp_dict.get("normalize", 1) else 0
        fp = space_fingerprint(model, rev, mods, prep, prompts, pool, bool(norm), dim, metric)
    else:
        model = str(emb_or_model)
        rev = revision
        mods = modalities or ["text", "image", "video"]
        prep = {}
        prompts = {"query": "SearchQuery", "doc": "Document"}
        pool = "mean"
        norm = 1
        fp = space_fingerprint(model, rev, mods, prep, prompts, pool, bool(norm), dim, metric)

    row = conn.execute("SELECT id, vec_table, active FROM embedding_space WHERE fingerprint = ?", (fp,)).fetchone()
    if row:
        space_id = row["id"]
        vec_table = row["vec_table"]
        if not row["active"]:
            conn.execute("UPDATE embedding_space SET active = 0 WHERE active = 1")
            conn.execute("UPDATE embedding_space SET active = 1 WHERE id = ?", (space_id,))
        return space_id, vec_table

    max_id = conn.execute("SELECT coalesce(max(id), 0) FROM embedding_space").fetchone()[0]
    new_id = max_id + 1
    vec_table = f"vec_s{new_id}"
    now = utcnow()

    # Drop any pre-existing table for this space id to ensure dim matches
    conn.execute(f"DROP TABLE IF EXISTS {vec_table}")
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {vec_table} USING vec0("
        f"  rid INTEGER PRIMARY KEY,"
        f"  embedding float[{dim}] distance_metric={metric},"
        f"  kind TEXT, rights TEXT, synthetic INTEGER, is_segment INTEGER,"
        f"  min_edge INTEGER, duration FLOAT, stale INTEGER"
        f")"
    )

    # Deactivate other spaces
    conn.execute("UPDATE embedding_space SET active = 0 WHERE active = 1")

    conn.execute(
        "INSERT INTO embedding_space(id, fingerprint, model, revision, modalities, preprocess, prompts, "
        "pooling, normalize, dim, metric, vec_table, active, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (new_id, fp, model, rev, json.dumps(mods), json.dumps(prep), json.dumps(prompts), pool, norm, dim, metric, vec_table, now),
    )
    return new_id, vec_table
