"""Persistent event queue for hooks (handover §11.2).

Events are committed in the same transaction as state changes and delivered at least once.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .ids import utcnow


def publish_event(conn: sqlite3.Connection, kind: str, payload: dict[str, Any], subscribers: list[str] | None = None) -> int:
    """Publish an event to the event log and schedule deliveries."""
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO event(kind, payload, created) VALUES (?, ?, ?)",
        (kind, json.dumps(payload, ensure_ascii=False), now),
    )
    event_id = cur.lastrowid

    if subscribers is None:
        rows = conn.execute("SELECT DISTINCT name, types FROM plugin_registry WHERE enabled = 1").fetchall()
        subscribers = list({r["name"] for r in rows if "hook" in json.loads(r["types"])})

    if subscribers:
        for sub in subscribers:
            conn.execute(
                "INSERT INTO event_delivery(event_id, subscriber, state, attempts, next_attempt) "
                "VALUES (?, ?, 'pending', 0, ?)",
                (event_id, sub, now),
            )

    return event_id


def deliver_events(conn: sqlite3.Connection, plugin_instances: dict[str, Any]) -> int:
    """Deliver pending events to subscribers."""
    rows = conn.execute(
        "SELECT ed.event_id, ed.subscriber, e.kind, e.payload FROM event_delivery ed "
        "JOIN event e ON e.id = ed.event_id WHERE ed.state IN ('pending', 'failed') LIMIT 50"
    ).fetchall()

    delivered_count = 0
    now = utcnow()
    from .plugins import run_sync
    for r in rows:
        sub = r["subscriber"]
        inst = plugin_instances.get(sub)
        payload = json.loads(r["payload"])
        try:
            if inst is None:
                raise RuntimeError(f"Hook {sub} is unavailable")
            run_sync(inst.handle(None, r["event_id"], r["kind"], payload))
            conn.execute("UPDATE event_delivery SET state = 'delivered', attempts = attempts + 1 WHERE event_id = ? AND subscriber = ?", (r["event_id"], sub))
            delivered_count += 1
        except Exception as e:
            conn.execute("UPDATE event_delivery SET state = 'failed', attempts = attempts + 1, last_error = ? WHERE event_id = ? AND subscriber = ?", (str(e), r["event_id"], sub))
    return delivered_count
