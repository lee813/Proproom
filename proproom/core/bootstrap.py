"""First start / every start: make the library directory consistent (handover §4.1).

Creates db.sqlite (+ -wal/-shm), blobs/ thumbs/ models/ plugins/ tmp/ journal/ backups/,
config.toml (only if absent), instance.token (only if absent), runs migrations,
seeds `instance` and bumps `policy` when the resolved rules changed.
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass

from . import config as config_mod
from . import db
from . import ops, jobs
from datetime import datetime, timedelta, timezone
from .ids import ulid, utcnow
from .paths import LIBRARY_DIRS, Home, resolve_home


@dataclass
class Booted:
    home: Home
    config: dict
    applied: list[str]
    policy_version: int
    created: bool


def _set_tmp_env(home: Home) -> None:
    tmp = str(home.dir("tmp"))
    os.environ["SQLITE_TMPDIR"] = tmp
    os.environ["TMPDIR"] = tmp
    os.environ.setdefault("HF_HOME", str(home.dir("models") / "hf"))


def boot(home: Home | None = None) -> Booted:
    home = home or resolve_home()
    created = not home.db.exists()
    home.root.mkdir(parents=True, exist_ok=True)
    for d in LIBRARY_DIRS:
        home.dir(d).mkdir(exist_ok=True)
    _set_tmp_env(home)

    if not home.config.exists():
        home.config.write_text(config_mod.default_text(), encoding="utf-8")
    if not home.token.exists():
        fd = os.open(home.token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")

    cfg = config_mod.load(home)
    conn = db.connect(home.db)
    try:
        applied = db.migrate(conn, home.dir("backups"))
        now = utcnow()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO instance(key, value) VALUES ('instance_id', ?)", (ulid(),))
        conn.execute("INSERT OR IGNORE INTO instance(key, value) VALUES ('created', ?)", (now,))
        conn.execute("INSERT OR REPLACE INTO instance(key, value) VALUES ('schema_version', ?)",
                     (str(db.user_version(conn)),))
        policy_version = _ensure_policy(conn, config_mod.policy_rules(cfg), now)
        ops.recover_operations(conn, home)
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        jobs.recover_stale_jobs(conn, cutoff)
        from .plugins import PluginManager
        instance_id = conn.execute("SELECT value FROM instance WHERE key = 'instance_id'").fetchone()[0]
        PluginManager(home, instance_id).load_all(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return Booted(home, cfg, applied, policy_version, created)


def _ensure_policy(conn, rules: dict, now: str) -> int:
    canonical = json.dumps(rules, sort_keys=True, separators=(",", ":"))
    row = conn.execute("SELECT version, rules FROM policy ORDER BY version DESC LIMIT 1").fetchone()
    if row and json.dumps(json.loads(row["rules"]), sort_keys=True, separators=(",", ":")) == canonical:
        return row["version"]
    version = (row["version"] + 1) if row else 1
    conn.execute("INSERT INTO policy(version, rules, created, created_by, note) VALUES (?, ?, ?, 'core', ?)",
                 (version, canonical, now, "initial defaults" if version == 1 else "config.toml changed"))
    return version


def read_token(home: Home) -> str:
    return home.token.read_text().strip()
