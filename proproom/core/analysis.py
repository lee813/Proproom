"""Analysis backlog: runs analyzer and segmenter plugins on stored representations, one pair per worker step.

Handover §11.2, §13. Plugins run outside any transaction (they may load models or run ffmpeg for minutes); core
validates and persists what they return — flags, regions, tags, loudness, segments — in one transaction, together
with the `analysis_run` row that takes the pair off the backlog.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any

from .ids import ulid, utcnow
from .plugins import run_sync
from ..sdk.context import AnalysisContext
from ..sdk.errors import ProproomError
from ..sdk.types import AnalysisResult, SegmentProposal

log = logging.getLogger("proproom.analysis")

MAX_ATTEMPTS = 3
REGION_KINDS = ("face", "plate", "text", "subject", "logo", "highlight")
DEFAULT_MEDIA = {"analyzer": ["image/", "video/", "audio/"], "segmenter": ["video/"]}
MIN_SEGMENT_S = 2.0

_PENDING = (
    "FROM representation r JOIN asset a ON a.id = r.asset_id AND a.status = 'active' "
    "JOIN blob b ON b.sha256 = r.blob_sha256 AND b.stored = 1 "
    "WHERE ({media}) AND NOT EXISTS (SELECT 1 FROM analysis_run ar WHERE ar.representation_id = r.id "
    "AND ar.plugin = ? AND ar.plugin_version = ? AND (ar.state = 'done' OR ar.attempts >= ? {not_due}))"
)


def _workers(pm: Any) -> list[tuple[Any, str]]:
    """Loaded, non-stub analyzer and segmenter plugins, in a stable order."""
    out = []
    for p in sorted(pm.plugins.values(), key=lambda p: p.name):
        if p.instance is None or p.manifest.get("plugin", {}).get("status") == "stub":
            continue
        for role, method in (("analyzer", "analyze"), ("segmenter", "segment")):
            if role in p.types and callable(getattr(p.instance, method, None)):
                out.append((p, role))
    return out


def _pending(p: Any, role: str, *, due_only: bool) -> tuple[str, list]:
    prefixes = p.manifest.get("plugin", {}).get("media_prefixes") or DEFAULT_MEDIA[role]
    sql = _PENDING.format(media=" OR ".join("b.mime LIKE ?" for _ in prefixes),
                          not_due="OR ar.next_attempt > ?" if due_only else "")
    params = [f"{prefix}%" for prefix in prefixes] + [p.name, p.version, MAX_ATTEMPTS]
    return sql, (params + [utcnow()] if due_only else params)


def backlog(conn: sqlite3.Connection, pm: Any) -> int:
    """Number of (representation, plugin) pairs not yet analyzed (including ones waiting for a retry)."""
    total = 0
    for p, role in _workers(pm):
        sql, params = _pending(p, role, due_only=False)
        total += conn.execute(f"SELECT count(*) {sql}", params).fetchone()[0]
    return total


def step(ctx: Any, conn: sqlite3.Connection, pm: Any) -> bool:
    """Run the next due (representation, plugin) pair. Returns False when nothing is due."""
    for p, role in _workers(pm):
        sql, params = _pending(p, role, due_only=True)
        rep = conn.execute(f"SELECT r.id, r.duration, b.mime {sql} ORDER BY r.created, r.id LIMIT 1", params).fetchone()
        if rep is not None:
            _run(ctx, conn, p, role, rep)
            return True
    return False


def _run(ctx: Any, conn: sqlite3.Connection, p: Any, role: str, rep: sqlite3.Row) -> None:
    home = ctx.booted.home

    def open_representation(rep_id: str) -> Path:
        r = conn.execute("SELECT b.storage_path FROM representation r JOIN blob b ON b.sha256 = r.blob_sha256 "
                         "WHERE r.id = ? AND b.stored = 1", (rep_id,)).fetchone()
        if r is None:
            raise ProproomError("not_found", f"No stored representation {rep_id}")
        return home.root / r["storage_path"]

    actx = AnalysisContext(plugin=p.name, log=logging.getLogger(f"proproom.plugin.{p.name}"),
                           config=ctx.booted.config.get(p.name, {}), open_representation=open_representation)
    now = utcnow()
    try:
        if role == "analyzer":
            result = run_sync(p.instance.analyze(actx, rep["id"]))
            if not isinstance(result, AnalysisResult):
                raise ProproomError("plugin_contract", f"{p.name}.analyze returned {type(result).__name__}")
            writes = _analysis_writes(rep, result, p, now)
        else:
            writes = _segment_writes(rep, run_sync(p.instance.segment(actx, rep["id"])))
        error = None
    except Exception as e:  # kept on the analysis_run row and retried with backoff, never dropped silently
        log.warning("%s %s failed on %s: %s", role, p.name, rep["id"], e)
        if isinstance(e, ProproomError):
            error = {"code": e.code, "message": e.message[:500]}
        else:
            error = {"code": type(e).__name__, "message": str(e)[:500]}
        writes = []

    conn.execute("BEGIN IMMEDIATE")
    try:
        for sql, params in writes:
            conn.execute(sql, params)
        prev = conn.execute("SELECT attempts FROM analysis_run WHERE representation_id = ? AND plugin = ? "
                            "AND plugin_version = ?", (rep["id"], p.name, p.version)).fetchone()
        attempts = prev["attempts"] + 1 if prev else 1
        retry = None
        if error:
            retry = (datetime.now(timezone.utc) + timedelta(minutes=4 ** (attempts - 1))
                     ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute(
            "INSERT INTO analysis_run(representation_id, plugin, plugin_version, state, attempts, error, next_attempt, "
            "updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(representation_id, plugin, plugin_version) DO UPDATE "
            "SET state = excluded.state, attempts = excluded.attempts, error = excluded.error, "
            "next_attempt = excluded.next_attempt, updated = excluded.updated",
            (rep["id"], p.name, p.version, "failed" if error else "done", attempts,
             json.dumps(error, ensure_ascii=False) if error else None, retry, now))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _analysis_writes(rep: sqlite3.Row, res: AnalysisResult, p: Any, now: str) -> list[tuple[str, list]]:
    """Validate a plugin's result into SQL writes. Raises on anything out of contract, so nothing is half-saved."""
    # A run replaces this plugin's earlier machine output on the representation (an older version's flags and boxes).
    # A flag someone reviewed stays, and the plugin does not raise that kind again.
    writes: list[tuple[str, list]] = [
        ("DELETE FROM flag WHERE target_type = 'representation' AND target_id = ? AND origin = 'machine' "
         "AND plugin = ? AND resolution IS NULL", [rep["id"], p.name]),
        ("DELETE FROM region WHERE representation_id = ? AND origin = 'machine' AND plugin = ?", [rep["id"], p.name]),
    ]
    for fl in res.flags:
        if fl.severity not in ("info", "warn", "block") or not fl.kind:
            raise ProproomError("plugin_contract", f"Invalid flag {fl!r}")
        writes.append(("INSERT INTO flag(target_type, target_id, kind, severity, score, evidence, origin, plugin, "
                       "plugin_version, created) SELECT 'representation', ?, ?, ?, ?, ?, 'machine', ?, ?, ? "
                       "WHERE NOT EXISTS (SELECT 1 FROM flag WHERE target_type = 'representation' AND target_id = ? "
                       "AND kind = ? AND origin = 'machine' AND plugin = ? AND resolution IS NOT NULL)",
                       [rep["id"], fl.kind, fl.severity, fl.score, json.dumps(fl.evidence or {}, ensure_ascii=False),
                        p.name, p.version, now, rep["id"], fl.kind, p.name]))
    for rg in res.regions:
        if rg.get("kind") not in REGION_KINDS:
            raise ProproomError("plugin_contract", f"Invalid region kind {rg.get('kind')!r}")
        x, y = max(0.0, float(rg["x"])), max(0.0, float(rg["y"]))
        w, h = min(float(rg["w"]), 1.0 - x), min(float(rg["h"]), 1.0 - y)
        if w <= 0 or h <= 0:
            raise ProproomError("plugin_contract", f"Empty region {rg!r}")
        writes.append(("INSERT INTO region(representation_id, kind, x, y, w, h, t, score, origin, plugin, created) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'machine', ?, ?)",
                       [rep["id"], rg["kind"], x, y, w, h, rg.get("t"), rg.get("score"), p.name, now]))
    for tag in res.tags:
        writes.append(("INSERT OR IGNORE INTO tag(asset_id, tag, origin) "
                       "SELECT asset_id, ?, 'machine' FROM representation WHERE id = ?", [str(tag), rep["id"]]))
    m = res.measurements
    if any(m.get(k) is not None for k in ("lufs", "lra", "true_peak")):
        writes.append(("UPDATE representation SET loudness_lufs = ?, loudness_range = ?, true_peak = ? WHERE id = ?",
                       [m.get("lufs"), m.get("lra"), m.get("true_peak"), rep["id"]]))
    return writes


def _segment_writes(rep: sqlite3.Row, proposals: list[SegmentProposal]) -> list[tuple[str, list]]:
    """§11.2: core checks segment bounds — at least 2 s, inside the clip, not overlapping."""
    duration = rep["duration"]
    writes, last_t1 = [], 0.0
    for prop in sorted(proposals or [], key=lambda s: s.t0):
        t0, t1 = float(prop.t0), float(prop.t1)
        if duration:
            t1 = min(t1, float(duration))
        if t0 < 0 or t1 - t0 < MIN_SEGMENT_S or t0 < last_t1 - 1e-6:
            raise ProproomError("plugin_contract", f"Invalid segment {t0:.2f}-{t1:.2f}")
        last_t1 = t1
        # Segments are referenced by selections and usage, so a re-run keeps the ones it finds again.
        writes.append(("INSERT OR IGNORE INTO segment(id, representation_id, t0, t1, algo, algo_version, params) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       [ulid(), rep["id"], t0, t1, prop.algo, prop.algo_version, json.dumps(prop.params or {})]))
    return writes
