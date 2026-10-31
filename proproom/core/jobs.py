"""Long-running jobs (pull/index/verify/import_project/reembed): pause on load or lockfile,
cancel, resume from cursor, heartbeat.

Handover §4.3, §13.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .ids import ulid, utcnow
from .paths import Home


def should_pause(home: Home, config: dict[str, Any], cores: int = 2) -> tuple[bool, str | None]:
    """Check if background job should pause due to system load or lockfile."""
    pause_cfg = config.get("pause", {})
    load_per_core = float(pause_cfg.get("load_per_core", 0.8))

    # 1. System load
    loadavg_path = Path("/proc/loadavg")
    if loadavg_path.is_file():
        try:
            parts = loadavg_path.read_text().split()
            load_1m = float(parts[0])
            if (load_1m / max(cores, 1)) > load_per_core:
                return True, f"load ({load_1m:.1f}/{cores} cores)"
        except Exception:
            pass

    # 2. Lockfiles relative to /nas
    lockfiles = pause_cfg.get("lockfiles", [])
    for lf in lockfiles:
        p = home.nas / lf
        if p.exists():
            return True, f"lockfile ({lf})"

    return False, None


def create_job(
    conn: sqlite3.Connection,
    kind: str,
    params: dict[str, Any],
    budget_bytes: int | None = None,
    operation_id: str | None = None,
) -> str:
    job_id = ulid()
    now = utcnow()
    conn.execute(
        "INSERT INTO job(id, kind, operation_id, state, params, progress, budget_bytes, created, started, heartbeat) "
        "VALUES (?, ?, ?, 'running', ?, '{}', ?, ?, ?, ?)",
        (job_id, kind, operation_id, json.dumps(params, ensure_ascii=False), budget_bytes, now, now, now),
    )
    return job_id


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["params"] = json.loads(d["params"]) if d["params"] else {}
    d["progress"] = json.loads(d["progress"]) if d["progress"] else {}
    d["error"] = json.loads(d["error"]) if d["error"] else None
    return d


def update_job_progress(
    conn: sqlite3.Connection,
    job_id: str,
    progress: dict[str, Any],
    used_bytes: int = 0,
    cursor: Any = None,
) -> None:
    now = utcnow()
    prog_json = json.dumps(progress, ensure_ascii=False)
    cur_json = json.dumps(cursor) if cursor is not None else None
    conn.execute(
        "UPDATE job SET progress = ?, used_bytes = used_bytes + ?, cursor = coalesce(?, cursor), heartbeat = ? WHERE id = ?",
        (prog_json, used_bytes, cur_json, now, job_id),
    )


def finish_job(
    conn: sqlite3.Connection,
    job_id: str,
    state: str = "succeeded",
    error: dict[str, Any] | None = None,
) -> None:
    now = utcnow()
    err_json = json.dumps(error, ensure_ascii=False) if error else None
    conn.execute(
        "UPDATE job SET state = ?, error = ?, finished = ?, heartbeat = ? WHERE id = ?",
        (state, err_json, now, now, job_id),
    )


def pause_job(conn: sqlite3.Connection, job_id: str, reason: str = "user") -> None:
    now = utcnow()
    conn.execute(
        "UPDATE job SET state = 'paused', pause_reason = ?, heartbeat = ? WHERE id = ?",
        (reason, now, job_id),
    )


def resume_job(conn: sqlite3.Connection, job_id: str) -> None:
    now = utcnow()
    conn.execute(
        "UPDATE job SET state = 'running', pause_reason = NULL, heartbeat = ? WHERE id = ?",
        (now, job_id),
    )


def cancel_job(conn: sqlite3.Connection, job_id: str) -> None:
    now = utcnow()
    conn.execute(
        "UPDATE job SET state = 'cancelled', finished = ?, heartbeat = ? WHERE id = ?",
        (now, now, job_id),
    )
