"""Tests for CAS store and deduplication."""
from pathlib import Path
from proproom.core import bootstrap, dedupe, store
from proproom.core.paths import Home


def test_store_and_verify(tmp_path):
    home = Home(tmp_path / "lib", tmp_path, "env", {})
    bootstrap.boot(home)

    data = b"hello proproom content addressed store"
    sha, size, relpath = store.put_bytes(home, data)

    assert size == len(data)
    assert relpath.startswith("blobs/")
    p = store.blob_abspath(home, sha)
    assert p.is_file()
    assert p.read_bytes() == data

    # Verify state
    assert store.verify_blob(home, sha) == "ok"
    assert store.verify_blob(home, "0" * 64) == "missing"


def test_dedupe_and_search_doc(tmp_path):
    home = Home(tmp_path / "lib", tmp_path, "env", {})
    b = bootstrap.boot(home)
    conn = b.home.db

    import sqlite3
    from proproom.core import db
    c = db.connect(home.db)
    try:
        c.execute("BEGIN IMMEDIATE")
        aid, rid = dedupe.register_asset(c, slug="test-tocqueville", kind="portrait", title="Tocqueville Portrait")
        assert aid and rid == 1

        # Register blob
        sha, size, rel = store.put_bytes(home, b"fake image bytes")
        dedupe.register_blob(c, sha, size, "image/jpeg", stored=True, storage_path=rel)

        rep_id = dedupe.register_representation(c, asset_id=aid, blob_sha256=sha, role="original", width=2000, height=2000)
        assert rep_id

        # Search doc update
        dedupe.sync_search_doc(c, rid=rid, title="Tocqueville Portrait", artist="Chasseriau", cjk_text="托克维尔")
        c.execute("COMMIT")

        # Query FTS
        hit = c.execute("SELECT rowid FROM fts_latin WHERE fts_latin MATCH 'Chasseriau'").fetchall()
        assert [r[0] for r in hit] == [rid]

        cjk_hit = c.execute("SELECT rowid FROM fts_cjk WHERE fts_cjk MATCH '\"托克维\"'").fetchall()
        assert [r[0] for r in cjk_hit] == [rid]
    finally:
        c.close()
