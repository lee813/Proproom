"""SQLite connection + forward-only migrations (handover §5.3).

Every connection: WAL, synchronous=NORMAL, foreign_keys, busy_timeout, temp_store=MEMORY, sqlite-vec loaded.
Writes go through core.writer (single writer thread); readers may open their own connections.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

import sqlite_vec

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA temp_store=MEMORY",
)

_MIGRATION_RE = re.compile(r"^(\d{4})_[\w-]+\.sql$")


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro" if readonly else f"file:{path}"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    for p in PRAGMAS:
        if readonly and "journal_mode" in p:
            continue
        conn.execute(p)
    return conn


def migrations() -> list[tuple[int, str, str]]:
    out = []
    for f in resources.files("proproom").joinpath("migrations").iterdir():
        if m := _MIGRATION_RE.match(f.name):
            out.append((int(m.group(1)), f.name, f.read_text(encoding="utf-8")))
    return sorted(out)


def user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def latest_version() -> int:
    ms = migrations()
    return ms[-1][0] if ms else 0


def migrate(conn: sqlite3.Connection, backups: Path) -> list[str]:
    """Apply pending migrations. Backs up via VACUUM INTO first when the DB already has a schema."""
    current = user_version(conn)
    pending = [m for m in migrations() if m[0] > current]
    if not pending:
        return []
    if current > 0:
        backups.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        conn.execute("VACUUM INTO ?", (str(backups / f"db-{current}-{ts}.sqlite"),))
    applied = []
    for ver, name, sql in pending:
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {ver};\nCOMMIT;")
        applied.append(name)
    return applied


def sql(name: str) -> str:
    """Load a named query from core/sql/."""
    return resources.files("proproom").joinpath("core", "sql", f"{name}.sql").read_text(encoding="utf-8")
