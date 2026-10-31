"""Per-operation JSONL journal in <home>/journal/<op>.jsonl; replay to roll back or continue after a crash.

Handover §10.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .ids import utcnow
from .paths import Home


def journal_path(home: Home, op_id: str) -> Path:
    return home.dir("journal") / f"{op_id}.jsonl"


def append_journal(home: Home, op_id: str, step: str, payload: dict[str, Any] | None = None) -> None:
    p = journal_path(home, op_id)
    entry = {"ts": utcnow(), "step": step, "data": payload or {}}
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(p, "a", encoding="utf-8") as f:
        f.write(line)


def read_journal(home: Home, op_id: str) -> list[dict[str, Any]]:
    p = journal_path(home, op_id)
    if not p.is_file():
        return []
    entries = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries
