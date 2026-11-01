"""Fixes from an agent's field report: several picks in one fetch, capture without a domain list, an old project's
manifest.json, readable names from `link --assets`, and source, title and tags on `add`."""
from __future__ import annotations

import json

import pytest

from support import ingest, install_source

from proproom.api.registry import REGISTRY
from proproom.core import db, http, ids
from proproom.sdk.errors import ProproomError
from proproom.sdk.types import RepresentationOffer, ResolvedSource, RightsEvidence

JPEG = b"\xff\xd8\xff\xe0"


def _db(ctx):
    return db.connect(ctx.booted.home.db)


class Numbered:
    """A source whose item `n` transfers the bytes `item n`; `bad` fails to resolve."""
    async def resolve(self, ctx, ref):
        if ref.external_id == "bad":
            raise ProproomError("not_found", "no such item")
        return ResolvedSource(ref=ref, title=f"Item {ref.external_id}", kind="photo", metadata={},
                              evidence=[RightsEvidence(basis="license", license_id="CC BY 4.0")],
                              offers=[RepresentationOffer("original", "image/png", "https://example.org/x")])

    async def transfer(self, ctx, request, sink):
        await sink.write(b"item " + request.ref.external_id.encode())


def _selection(conn, ids_):
    sel = ids.ulid()
    cands = [{"idx": i, "source": "fake", "external_id": e} for i, e in enumerate(ids_, start=1)]
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO selection(id, created, created_by, query) VALUES (?, ?, 'test', ?)",
                 (sel, ids.utcnow(), json.dumps({"candidates": cands})))
    conn.execute("COMMIT")
    return sel


def test_fetch_takes_every_pick_and_reports_each(ctx, conn):
    install_source(ctx, "fake", Numbered())
    sel = _selection(conn, ["a", "bad", "c"])
    out = REGISTRY["fetch"](ctx, {"selection_id": sel, "pick": [1, 2, 3]})
    assert [(r["pick"], r["status"]) for r in out["results"]] == [(1, "ok"), (2, "error"), (3, "ok")]
    assert out["results"][1]["error"]["code"] == "not_found" and out["fetched"] == 2
    assert conn.execute("SELECT count(*) FROM asset").fetchone()[0] == 2


def test_a_single_pick_still_returns_the_plain_result(ctx, conn):
    install_source(ctx, "fake", Numbered())
    out = REGISTRY["fetch"](ctx, {"selection_id": _selection(conn, ["a"]), "pick": [1]})
    assert "sha256" in out and "results" not in out


@pytest.mark.parametrize("url,ok", [
    ("http://8.8.8.8/page", True), ("https://[2606:4700::1111]/", True),
    ("http://127.0.0.1:8765/", False), ("http://192.168.1.10:8085/", False), ("http://10.0.0.5/", False),
    ("http://169.254.169.254/latest/meta-data", False), ("http://localhost/", False), ("http://[::1]/", False)])
def test_any_public_host_is_open_but_the_lan_is_not(url, ok):
    if ok:
        http.check_domain(url, [http.ANY_PUBLIC_HOST])
    else:
        with pytest.raises(ProproomError) as exc:
            http.check_domain(url, [http.ANY_PUBLIC_HOST])
        assert exc.value.code == "domain_disallowed"


def test_a_plugin_with_no_domains_still_has_no_network():
    with pytest.raises(ProproomError):
        http.check_domain("http://8.8.8.8/", [])


def test_capture_with_no_domain_list_may_reach_public_sites(ctx):
    pm = install_source(ctx, "capture", object())
    pm.plugins["capture"].manifest = {"source": {"configurable_domains": True}}
    assert pm.get_source_context("capture").http.allowed_domains == [http.ANY_PUBLIC_HOST]


def test_an_old_manifest_json_gives_source_and_author_not_own_work(ctx, nas):
    ep = nas / "project" / "OldBrightline"
    (ep / "assets").mkdir(parents=True)
    (ep / "assets" / "train.jpg").write_bytes(JPEG + b"train")
    (ep / "assets" / "mine.jpg").write_bytes(JPEG + b"mine")
    (ep / "assets" / "manifest.json").write_text(json.dumps([
        {"id": "P01", "filename": "train.jpg", "url": "https://press.example.com/img/train.jpg",
         "source": "https://press.example.com/media-kit", "author": "Brightline", "usage": "press kit"}]))
    res = REGISTRY["import_project"](ctx, {"project": "project/OldBrightline"})
    assert res["progress"]["imported"] == 2 and res["progress"]["blocked"] == 0
    c = _db(ctx)
    row = c.execute("SELECT e.basis, e.attribution, p.source_url FROM rights_evidence e JOIN provenance p "
                    "ON p.id = e.provenance_id JOIN asset a ON a.id = p.asset_id WHERE a.slug LIKE '%train%'").fetchone()
    assert tuple(row) == ("unknown", "Brightline", "https://press.example.com/media-kit")
    own = c.execute("SELECT e.basis FROM rights_evidence e JOIN provenance p ON p.id = e.provenance_id "
                    "WHERE p.external_id LIKE '%mine.jpg'").fetchone()
    assert own["basis"] == "own_work"
    text = REGISTRY["credits"](ctx, {"project": "project/OldBrightline"})["formatted"]
    assert "by Brightline" in text and "Unknown" not in text


def test_a_listed_item_with_a_source_and_no_license_is_not_own_work_and_unknown_is_not_a_credit(ctx, nas):
    ep = nas / "project" / "Sourced"
    (ep / "assets").mkdir(parents=True)
    (ep / "assets" / "a.jpg").write_bytes(JPEG + b"a")
    (ep / "assets" / "b.jpg").write_bytes(JPEG + b"b")
    (ep / "assets.json").write_text(json.dumps({"assets": [
        {"id": "a", "file": "assets/a.jpg", "source": "https://news.example.com/story", "artist": "Unknown",
         "credit": "Unknown / None"},
        {"id": "b", "file": "assets/b.jpg", "source": "", "artist": "Unknown"}]}))
    REGISTRY["import_project"](ctx, {"project": "project/Sourced"})
    rows = {r["external_id"].split("#")[0]: (r["basis"], r["attribution"]) for r in _db(ctx).execute(
        "SELECT p.external_id, e.basis, e.attribution FROM rights_evidence e JOIN provenance p ON p.id = e.provenance_id")}
    assert rows["https://news.example.com/story"] == ("unknown", None)
    assert [v for k, v in rows.items() if k != "https://news.example.com/story"] == [("own_work", None)]


def test_link_by_asset_id_names_the_file_after_the_slug(ctx, conn, home, nas):
    a = ingest(conn, home, slug="harbour-at-dawn", title="Harbour at dawn")
    plan = REGISTRY["link"](ctx, {"assets": [a.asset_id], "project": "project/Named", "plan": True})
    assert plan["planned_files"][0]["relpath"] == "assets/harbour-at-dawn.jpg"


def test_add_takes_a_title_a_source_and_tags(ctx, nas):
    f = nas / "inbox" / "IMG_0042.jpg"
    f.parent.mkdir()
    f.write_bytes(JPEG + b"photo")
    out = REGISTRY["add"](ctx, {"paths": [str(f)], "title": "Lisbon tram at night",
                                "source": "https://news.example.com/lisbon", "tags": ["tram", " night "]})
    assert out["added"][0]["slug"] == "lisbon-tram-at-night"
    c = _db(ctx)
    aid = out["added"][0]["asset_id"]
    assert c.execute("SELECT title FROM asset WHERE id = ?", (aid,)).fetchone()[0] == "Lisbon tram at night"
    assert {tuple(r) for r in c.execute("SELECT tag, origin FROM tag WHERE asset_id = ?", (aid,))} == {
        ("tram", "human"), ("night", "human")}
    assert tuple(c.execute("SELECT source_url, source FROM provenance WHERE asset_id = ?", (aid,)).fetchone()) == (
        "https://news.example.com/lisbon", "manual")
    # A stated source is not "own work" unless the caller says so.
    assert c.execute("SELECT e.basis FROM rights_evidence e JOIN provenance p ON p.id = e.provenance_id "
                     "WHERE p.asset_id = ?", (aid,)).fetchone()[0] == "unknown"
    assert c.execute("SELECT s.tags FROM search_doc s JOIN asset a ON a.rid = s.rid WHERE a.id = ?",
                     (aid,)).fetchone()[0].split() == ["tram", "night"]


def test_add_without_options_is_still_own_work(ctx, nas):
    f = nas / "plain.jpg"
    f.write_bytes(JPEG + b"plain")
    aid = REGISTRY["add"](ctx, {"paths": [str(f)]})["added"][0]["asset_id"]
    assert _db(ctx).execute("SELECT e.basis FROM rights_evidence e JOIN provenance p ON p.id = e.provenance_id "
                            "WHERE p.asset_id = ?", (aid,)).fetchone()[0] == "own_work"


@pytest.mark.parametrize("args", [{"title": "x"}, {"tags": "night"}, {"tags": [""]}, {"source": " "}])
def test_add_refuses_bad_options(ctx, nas, args):
    f1, f2 = nas / "one.jpg", nas / "two.jpg"
    f1.write_bytes(JPEG + b"1")
    f2.write_bytes(JPEG + b"2")
    paths = [str(f1), str(f2)] if "title" in args else [str(f1)]
    with pytest.raises(ProproomError) as exc:
        REGISTRY["add"](ctx, {"paths": paths, **args})
    assert exc.value.code == "bad_argument"
