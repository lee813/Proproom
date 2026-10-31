"""Operation lifecycle: idempotency key claim, request_hash conflict, plan -> run -> succeeded/failed,
recovery of planned/running on start.

Handover §8, §10, B.3.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Tuple

from ..sdk.errors import ProproomError
from .ids import ulid, utcnow
from .journal import append_journal, journal_path
from .paths import Home


def canonical_hash(data: dict[str, Any]) -> str:
    """Canonical SHA-256 hash of a request payload."""
    serialized = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def claim_operation(
    conn: sqlite3.Connection,
    home: Home,
    kind: str,
    idempotency_key: str | None,
    request: dict[str, Any],
    actor: str = "cli",
    initial_state: str = "running",
) -> tuple[str, bool, dict[str, Any] | None]:
    """Claim an operation in the database.

    Returns:
      (op_id, is_new, previous_result)
      If idempotent retry with same key:
        - If previous succeeded: returns (op_id, False, previous_result)
        - If previous running/planned: returns (op_id, False, None)
      Raises ProproomError("idempotency_conflict") if request hash differs.
    """
    now = utcnow()
    req_hash = canonical_hash(request)
    req_json = json.dumps(request, sort_keys=True, separators=(",", ":"))

    if not idempotency_key:
        # Non-idempotent operation: generate a random key
        op_id = ulid()
        j_path = str(journal_path(home, op_id).relative_to(home.root))
        conn.execute(
            "INSERT INTO operation(id, kind, idempotency_key, request_hash, request, state, actor, created, started, journal_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (op_id, kind, f"auto_{op_id}", req_hash, req_json, initial_state, actor, now, now, j_path),
        )
        append_journal(home, op_id, "start", {"kind": kind, "actor": actor})
        return op_id, True, None

    # Try to claim with ON CONFLICT DO NOTHING
    op_id = ulid()
    j_path = str(journal_path(home, op_id).relative_to(home.root))
    conn.execute(
        "INSERT INTO operation(id, kind, idempotency_key, request_hash, request, state, actor, created, started, journal_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(kind, idempotency_key) DO NOTHING",
        (op_id, kind, idempotency_key, req_hash, req_json, initial_state, actor, now, now, j_path),
    )

    row = conn.execute(
        "SELECT id, request_hash, state, result, error, journal_path FROM operation WHERE kind = ? AND idempotency_key = ?",
        (kind, idempotency_key),
    ).fetchone()

    if row["request_hash"] != req_hash:
        raise ProproomError(
            code="idempotency_conflict",
            message=f"Idempotency key '{idempotency_key}' was previously used with a different request payload",
            hint="Use a unique idempotency_key or keep the exact same parameters on retry",
            retryable=False,
        )

    if row["id"] == op_id:
        # Newly claimed
        append_journal(home, op_id, "start", {"kind": kind, "actor": actor, "key": idempotency_key})
        return op_id, True, None

    # Pre-existing operation
    if row["state"] == "succeeded":
        res = json.loads(row["result"]) if row["result"] else {}
        return row["id"], False, res

    if row["state"] == "failed":
        err = json.loads(row["error"]) if row["error"] else {}
        raise ProproomError(
            code=err.get("code", "previous_failed"),
            message=err.get("message", "Operation previously failed with this idempotency key"),
            details=err,
            retryable=False,
        )

    # In planned or running state
    return row["id"], False, None


def update_plan(conn: sqlite3.Connection, home: Home, op_id: str, plan: dict[str, Any]) -> None:
    plan_json = json.dumps(plan, ensure_ascii=False)
    conn.execute("UPDATE operation SET plan = ?, state = 'planned' WHERE id = ?", (plan_json, op_id))
    append_journal(home, op_id, "plan", plan)


def complete_operation(
    conn: sqlite3.Connection, home: Home, op_id: str, result: dict[str, Any]
) -> None:
    now = utcnow()
    res_json = json.dumps(result, ensure_ascii=False)
    conn.execute(
        "UPDATE operation SET state = 'succeeded', result = ?, finished = ? WHERE id = ?",
        (res_json, now, op_id),
    )
    append_journal(home, op_id, "succeeded", result)


def fail_operation(
    conn: sqlite3.Connection, home: Home, op_id: str, error: dict[str, Any]
) -> None:
    now = utcnow()
    err_json = json.dumps(error, ensure_ascii=False)
    conn.execute(
        "UPDATE operation SET state = 'failed', error = ?, finished = ? WHERE id = ?",
        (err_json, now, op_id),
    )
    append_journal(home, op_id, "failed", error)


def recover_operations(conn: sqlite3.Connection, home: Home) -> list[str]:
    """Check for unfinished operations at startup and mark them failed or recovered."""
    rows = conn.execute("SELECT id, kind, state FROM operation WHERE state IN ('planned', 'running')").fetchall()
    recovered = []
    now = utcnow()
    for r in rows:
        op_id = r["id"]
        append_journal(home, op_id, "recovered_on_boot", {"previous_state": r["state"]})
        # If it was planned, leave as planned so agent can execute by plan id; if running, mark failed
        if r["state"] == "running":
            conn.execute(
                "UPDATE operation SET state = 'failed', error = ?, finished = ? WHERE id = ?",
                (json.dumps({"code": "interrupted", "message": "Server restarted while operation was running"}), now, op_id),
            )
            recovered.append(op_id)
    return recovered
