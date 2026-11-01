"""§5.2 identity and dedupe rules."""
from __future__ import annotations

from support import gap, ingest

from proproom.api.registry import REGISTRY
from proproom.core import dedupe, store


def test_same_bytes_are_one_blob(conn, home):
    """§5.2: dedupe looks only at sha256."""
    a = ingest(conn, home, slug="a", title="A", data=b"same-bytes")
    b = ingest(conn, home, slug="b", title="B", data=b"same-bytes")
    assert a.sha == b.sha
    assert conn.execute("SELECT count(*) FROM blob WHERE sha256 = ?", (a.sha,)).fetchone()[0] == 1


def test_blobs_are_immutable_on_disk(conn, home):
    """§2 principle 1 / §10: blob bytes are never modified in place (0444)."""
    a = ingest(conn, home, slug="a", title="A")
    assert store.blob_abspath(home, a.sha).stat().st_mode & 0o222 == 0


def test_ulid_identity_survives_slug_rename(conn, home):
    """§5.2: renamed slugs keep resolving to the same asset via asset_slug_history."""
    a = ingest(conn, home, slug="commons-tocqueville-1850", title="Tocqueville")
    conn.execute("INSERT INTO asset_slug_history(slug, asset_id, retired) VALUES ('old-slug', ?, 'now')", (a.asset_id,))
    assert dedupe.register_asset(conn, slug="old-slug", kind="photo", title="x")[0] == a.asset_id


def test_add_files_with_same_stem_are_different_assets(ctx, nas):
    """§5.2: different works must never merge just because their file names look alike."""
    (nas / "music").mkdir()
    (nas / "broll").mkdir()
    (nas / "music" / "theme.mp3").write_bytes(b"music bytes")
    (nas / "broll" / "theme.mp4").write_bytes(b"video bytes")
    res = REGISTRY["add"](ctx, {"paths": ["music/theme.mp3", "broll/theme.mp4"]})
    assert len({x["asset_id"] for x in res["added"]}) == 2


def test_same_bytes_from_two_sources_share_one_asset(ctx, nas):
    """§5.2: 跨来源相同字节 — one blob, both provenances kept, under the same asset."""
    for ep in ("ep1", "ep2"):
        d = nas / "project" / ep / "assets"
        d.mkdir(parents=True)
        (d / "p.jpg").write_bytes(b"identical portrait bytes")
        src = "https://commons.wikimedia.org/wiki/File:P.jpg" if ep == "ep1" else "https://museum.example/p"
        (nas / "project" / ep / "assets.json").write_text(
            '{"assets": [{"id": "p", "kind": "portrait", "file": "assets/p.jpg", "license": "CC0", '
            f'"source": "{src}"}}]}}')
        REGISTRY["import_project"](ctx, {"project": f"project/{ep}"})
    from proproom.core import db
    c = db.connect(ctx.booted.home.db)
    assert c.execute("SELECT count(*) FROM asset").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM provenance").fetchone()[0] == 2


def test_every_assessment_cites_evidence_of_its_own_asset(ctx, nas):
    """§5 / §6: evidence → provenance → asset must point back to the assessed asset."""
    d = nas / "project" / "ep" / "assets"
    d.mkdir(parents=True)
    (d / "a.jpg").write_bytes(b"a")
    (d / "b.jpg").write_bytes(b"b")
    (nas / "project" / "ep" / "assets.json").write_text(
        '{"assets": [{"id": "a", "kind": "photo", "file": "assets/a.jpg", "license": "CC0", "source": "https://x/1"},'
        ' {"id": "b", "kind": "photo", "file": "assets/b.jpg", "license": "CC0", "source": "https://x/1"}]}')
    REGISTRY["import_project"](ctx, {"project": "project/ep"})
    from proproom.core import db
    c = db.connect(ctx.booted.home.db)
    bad = c.execute(
        "SELECT count(*) FROM rights_assessment ra "
        "JOIN rights_evidence re ON re.id = json_extract(ra.evidence_ids, '$[0]') "
        "JOIN provenance p ON p.id = re.provenance_id WHERE p.asset_id != ra.asset_id").fetchone()[0]
    orphans = c.execute("SELECT count(*) FROM asset a WHERE NOT EXISTS "
                        "(SELECT 1 FROM provenance p WHERE p.asset_id = a.id)").fetchone()[0]
    assert (bad, orphans) == (0, 0)
