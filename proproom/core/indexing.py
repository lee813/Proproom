"""Indexing pipeline: embed active representations into active embedding space.

Handover §4.2, §7.
"""
from __future__ import annotations

import logging
import json
from pathlib import Path
import struct
from typing import Any

from . import db, hardware, jobs, spaces, store
from .ids import utcnow
from .plugins import run_sync
from ..sdk.context import EmbedContext
from ..sdk.errors import ProproomError

log = logging.getLogger("proproom.indexing")


def _embedder(ctx: Any) -> Any:
    pm = ctx.plugin_manager or ctx.pm()
    for plugin in pm.plugins.values():
        if "embedder" in plugin.types and plugin.instance and plugin.manifest.get("plugin", {}).get("status") != "stub":
            return plugin.instance
    raise ProproomError("no_embedder", "No embedder plugin is loaded")


def index_representations(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    embedder = _embedder(ctx)
    conn = db.connect(ctx.booted.home.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        space_id, _ = spaces.ensure_embedding_space(conn, embedder)
        job_id = jobs.create_job(conn, "index", {"space_id": space_id, "fingerprint": embedder.fingerprint, "dim": embedder.dim})
        row = dict(conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone())
        conn.execute("COMMIT")
        return row
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _embed_one_batch(embedder: Any, ectx: EmbedContext, inputs: list[dict]) -> tuple[list, list]:
    batch = run_sync(embedder.embed(ectx, inputs))
    if len(batch.vectors) != len(inputs) or len(batch.input_hashes) != len(inputs):
        raise ProproomError("invalid_embedding_batch", "Embedder returned the wrong number of vectors")
    return batch.vectors, batch.input_hashes


def _embed_each(embedder: Any, ectx: EmbedContext, inputs: list[dict], candidates: list, skipped: list
                ) -> tuple[list, list]:
    """Embed a batch. If it fails, embed its items one by one: an item that fails is recorded in `skipped` (and
    tried again on the next index run) so one unreadable file does not stop the index. If every item fails, the
    embedder itself is broken and the error is raised. `candidates` is narrowed in place to the embedded items."""
    if not inputs:
        return [], []
    try:
        return _embed_one_batch(embedder, ectx, inputs)
    except ProproomError as e:
        if e.code == "invalid_embedding_batch" or len(inputs) == 1:
            raise
    except Exception:
        if len(inputs) == 1:
            raise
    vectors, hashes, kept, first_error = [], [], [], None
    for inp, cand in zip(inputs, candidates):
        try:
            (vec,), (h,) = _embed_one_batch(embedder, ectx, [inp])
        except Exception as e:  # recorded per item below; re-raised if nothing embeds
            first_error = first_error or e
            skipped.append({"representation_id": cand["rep_id"], "reason": "embed_failed",
                            "error": f"{getattr(e, 'code', type(e).__name__)}: {getattr(e, 'message', str(e))}"[:300]})
            continue
        vectors.append(vec)
        hashes.append(h)
        kept.append(cand)
    if not kept:
        raise first_error
    candidates[:] = kept
    return vectors, hashes


def step_index(ctx: Any, conn: Any, job_id: str, params: dict[str, Any]) -> None:
    b = ctx.booted
    embedder = _embedder(ctx)
    job_row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    cursor = json.loads(job_row["cursor"]) if job_row["cursor"] else {}
    space_id = params["space_id"]
    if params.get("fingerprint") != embedder.fingerprint or params.get("dim") != embedder.dim:
        raise ProproomError("embedding_space_mismatch", "The queued embedder changed before indexing")
    space = conn.execute("SELECT vec_table FROM embedding_space WHERE id = ?", (space_id,)).fetchone()
    if not space:
        raise ProproomError("space_not_found", "Index embedding space no longer exists")
    vec_table = space["vec_table"]
    rows = conn.execute(
        "SELECT r.id AS rep_id, r.asset_id, r.blob_sha256, r.width, r.height, r.duration, "
        "a.kind, a.synthetic, b.mime, coalesce(ar.decision, 'deny') AS rights "
        "FROM representation r JOIN asset a ON a.id = r.asset_id AND a.status = 'active' "
        "JOIN blob b ON b.sha256 = r.blob_sha256 LEFT JOIN asset_rights ar ON ar.asset_id = a.id "
        "LEFT JOIN vec_target vt ON vt.space_id = ? AND vt.representation_id = r.id AND vt.segment_id IS NULL AND vt.stale = 0 "
        "WHERE vt.rid IS NULL AND r.id > ? ORDER BY r.id LIMIT 16", (space_id, cursor.get("after", ""))
    ).fetchall()
    if not rows:
        conn.execute("BEGIN IMMEDIATE")
        jobs.finish_job(conn, job_id, "succeeded")
        conn.execute("COMMIT")
        return
    after = rows[-1]["rep_id"]
    inputs, candidates, skipped = [], [], []
    for cand in rows:
        path = store.blob_abspath(b.home, cand["blob_sha256"])
        # The blob's sniffed MIME decides the input, not the asset kind: a manifest can call a GeoJSON file a photo.
        mime = cand["mime"] or ""
        if mime.startswith("video/"):
            inp = {"video": str(path)}
        elif mime.startswith("audio/"):
            inp = {"audio": str(path)}
        elif mime.startswith("image/"):
            inp = {"image": str(path)}
        elif mime.startswith("text/"):
            inp = {"text": path.read_text(encoding="utf-8")}
        else:
            skipped.append({"representation_id": cand["rep_id"], "reason": "unsupported_mime", "mime": mime})
            continue
        inp["prompt"] = "document"
        inputs.append(inp)
        candidates.append(cand)
    hw = cursor.get("hardware")
    if hw is None:
        hw = hardware.detect()
    cursor["hardware"] = hw
    gpu = hw.get("tier") == "nvidia"
    ectx = EmbedContext(plugin="embedder", log=log, config={}, device="cuda" if gpu else "cpu",
                        dtype=hw["devices"][0].get("dtype", "fp32") if gpu else "fp32",
                        models_dir=str(b.home.dir("models")))
    vectors, hashes = _embed_each(embedder, ectx, inputs, candidates, skipped)

    # Commit vectors in transaction
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = utcnow()
        for cand, vec, in_hash in zip(candidates, vectors, hashes):
            rep_id = cand["rep_id"]
            asset_id = cand["asset_id"]

            # Remove existing target for this rep if stale
            old = conn.execute(
                "SELECT rid FROM vec_target WHERE space_id = ? AND representation_id = ? AND segment_id IS NULL",
                (space_id, rep_id),
            ).fetchone()
            if old:
                conn.execute(f"DELETE FROM {vec_table} WHERE rid = ?", (old["rid"],))
                conn.execute("DELETE FROM vec_target WHERE rid = ?", (old["rid"],))

            cur = conn.execute(
                "INSERT INTO vec_target(space_id, asset_id, representation_id, segment_id, input_hash, stale, created) "
                "VALUES (?, ?, ?, NULL, ?, 0, ?)",
                (space_id, asset_id, rep_id, in_hash, now),
            )
            rid = cur.lastrowid

            w = cand["width"] or 0
            h = cand["height"] or 0
            min_edge = min(w, h) if (w > 0 and h > 0) else 0
            raw_vec = struct.pack(f"{len(vec)}f", *vec)

            conn.execute(
                f"INSERT INTO {vec_table}(rid, embedding, kind, rights, synthetic, is_segment, min_edge, duration, stale) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, 0)",
                (
                    rid,
                    raw_vec,
                    cand["kind"],
                    cand["rights"],
                    cand["synthetic"],
                    min_edge,
                    float(cand["duration"] or 0.0),
                ),
            )

        progress = json.loads(job_row["progress"])
        progress["indexed"] = progress.get("indexed", 0) + len(candidates)
        previous = progress.get("skipped", [])
        if isinstance(previous, list):
            progress["skipped_count"] = progress.get("skipped_count", len(previous)) + len(skipped)
            progress["skipped"] = (previous + skipped)[:20]
        else:
            progress["skipped_count"] = progress.get("skipped_count", int(previous)) + len(skipped)
            progress["skipped"] = skipped[:20]
        cursor["after"] = after
        jobs.update_job_progress(conn, job_id, progress, cursor=cursor)
        conn.execute("COMMIT")
        return
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

