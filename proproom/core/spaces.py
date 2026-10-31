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
    model: str = "google/embeddinggemma-2",
    revision: str = "main",
    modalities: list[str] | None = None,
    dim: int = 256,
    metric: str = "cosine",
) -> tuple[int, str]:
    """Ensure embedding space row and corresponding vec0 table exist. Returns (space_id, vec_table)."""
    mods = modalities or ["text", "image", "video"]
    prompts = {"query": "SearchQuery", "doc": "Document"}
    fp = space_fingerprint(model, revision, mods, {}, prompts, "mean", True, dim, metric)

    row = conn.execute("SELECT id, vec_table FROM embedding_space WHERE fingerprint = ?", (fp,)).fetchone()
    if row:
        return row["id"], row["vec_table"]

    # Space 1 is vec_s1 (created in migration 0001)
    max_id = conn.execute("SELECT coalesce(max(id), 0) FROM embedding_space").fetchone()[0]
    new_id = max_id + 1
    vec_table = f"vec_s{new_id}"
    now = utcnow()

    # Create new vec0 table if new_id > 1
    if new_id > 1:
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
        "VALUES (?, ?, ?, ?, ?, '{}', ?, 'mean', 1, ?, ?, ?, 1, ?)",
        (new_id, fp, model, revision, json.dumps(mods), json.dumps(prompts), dim, metric, vec_table, now),
    )
    return new_id, vec_table
