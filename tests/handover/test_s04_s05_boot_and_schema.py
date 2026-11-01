"""§4.1 boot / first start, §5.1 entities, §5.3 SQLite conventions."""
from __future__ import annotations

import os
import stat

from proproom.core import bootstrap, db
from proproom.core.paths import resolve_home

SPEC_TABLES = {  # §5.1
    "asset", "asset_slug_history", "blob", "representation", "external_location", "watch_root", "provenance",
    "rights_evidence", "policy", "rights_assessment", "entity", "asset_entity", "alias", "tag", "note", "region",
    "flag", "segment", "similar", "embedding_space", "vec_target", "search_doc", "fts_latin", "fts_cjk",
    "selection", "selection_item", "share", "share_item", "share_feedback", "project", "operation", "usage",
    "job", "pull_seen", "event", "event_delivery", "plugin_registry", "instance",
}


def test_home_resolution_order(nas, monkeypatch):
    """§4.1: env PROP_HOME, then /nas/.proproom.toml `home` (relative to /nas), then /nas/proproom."""
    assert resolve_home().root == nas / "proproom" and resolve_home().source == "default"
    (nas / ".proproom.toml").write_text('home = "assets/library"\n')
    assert resolve_home().root == nas / "assets" / "library" and resolve_home().source == "boot_file"
    monkeypatch.setenv("PROP_HOME", str(nas / "elsewhere"))
    assert resolve_home().root == nas / "elsewhere" and resolve_home().source == "env"


def test_first_start_layout(lib):
    """§4.1: db.sqlite (+wal/shm), the six dirs, config.toml, instance.token."""
    root = lib.home.root
    for d in ("blobs", "thumbs", "models", "plugins", "tmp", "journal"):
        assert (root / d).is_dir(), d
    assert (root / "db.sqlite").is_file() and (root / "config.toml").is_file()
    assert stat.S_IMODE((root / "instance.token").stat().st_mode) == 0o600
    assert len(bootstrap.read_token(lib.home)) >= 32


def test_config_is_never_overwritten(lib):
    """§4.1: config.toml is generated once; the admin's edits survive restarts."""
    lib.home.config.write_text('[server]\npublic_url = "http://10.0.0.5:8765"\n')
    again = bootstrap.boot(lib.home)
    assert again.config["server"]["public_url"] == "http://10.0.0.5:8765"
    assert again.home.config.read_text().startswith("[server]")


def test_policy_is_versioned(lib):
    """§6: policy has a version; changing the admin's rules creates a new version, rebooting does not."""
    assert bootstrap.boot(lib.home).policy_version == 1
    lib.home.config.write_text('[rights]\nallow = ["cc0", "pd", "cc-by", "cc-by-sa", "cc-by-nc"]\n')
    assert bootstrap.boot(lib.home).policy_version == 2
    assert bootstrap.boot(lib.home).policy_version == 2


def test_sqlite_tmp_stays_in_library(lib):
    """§5.3: SQLITE_TMPDIR points at <home>/tmp."""
    assert os.environ["SQLITE_TMPDIR"] == str(lib.home.dir("tmp"))


def test_connection_pragmas(conn):
    """§5.3: WAL, synchronous=NORMAL, foreign_keys, busy_timeout=5000, temp_store=MEMORY."""
    p = lambda name: conn.execute(f"PRAGMA {name}").fetchone()[0]  # noqa: E731
    assert p("journal_mode") == "wal"
    assert p("synchronous") == 1      # NORMAL
    assert p("foreign_keys") == 1
    assert p("busy_timeout") == 5000
    assert p("temp_store") == 2       # MEMORY


def test_all_spec_tables_exist(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type IN ('table','view')")}
    assert SPEC_TABLES <= names, SPEC_TABLES - names
    assert "asset_rights" in names


def test_all_plain_tables_are_strict(conn):
    """§5.3: every table is STRICT."""
    rows = conn.execute("SELECT name, sql FROM sqlite_schema WHERE type='table' AND sql NOT LIKE 'CREATE VIRTUAL%' "
                        "AND name NOT LIKE 'sqlite_%'").fetchall()
    shadow = ("fts_latin_", "fts_cjk_", "vec_s1_")
    loose = [r["name"] for r in rows if not r["name"].startswith(shadow) and not r["sql"].rstrip().endswith("STRICT")]
    assert loose == []


def test_json_columns_are_validated(conn):
    """§5.3: JSON columns carry CHECK(json_valid(...))."""
    import sqlite3

    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO event(kind, payload, created) VALUES ('x', 'not json', 'now')")


def test_migrations_are_forward_only_and_recorded(conn):
    """§5.3: PRAGMA user_version tracks migrations/NNNN_*.sql."""
    assert db.user_version(conn) == db.latest_version() >= 1
    assert [m[1] for m in db.migrations()][0] == "0001_init.sql"
