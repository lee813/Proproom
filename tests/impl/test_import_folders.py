"""Project import takes the media in a project folder whether or not assets.json lists it, treats a missing license
as usable, and says what rights blocked (§6, §13). A new assessment reaches the vector rows (§7)."""
from __future__ import annotations

import json

from support import ingest, policy_rules

from proproom.api.registry import REGISTRY
from proproom.core import db, dedupe

JPEG = b"\xff\xd8\xff\xe0"


def _db(ctx):
    return db.connect(ctx.booted.home.db)


def test_a_folder_without_assets_json_imports_its_media(ctx, nas):
    ep = nas / "project" / "NoManifest"
    (ep / "assets" / "sub").mkdir(parents=True)
    (ep / "assets" / "train.jpg").write_bytes(JPEG + b"train")
    (ep / "assets" / "sub" / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42clip")
    (ep / "assets" / "map.geojson").write_text("{}")
    (ep / "render.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42render")  # outside assets/: the project's own output
    res = REGISTRY["import_project"](ctx, {"project": "project/NoManifest"})
    assert res["state"] == "succeeded"
    assert res["progress"]["imported"] == 2 and res["progress"]["blocked"] == 0
    c = _db(ctx)
    assert {r[0] for r in c.execute("SELECT kind FROM asset")} == {"photo", "footage"}
    assert {r[0] for r in c.execute("SELECT decision FROM asset_rights")} == {"allow"}
    assert c.execute("SELECT count(*) FROM usage").fetchone()[0] == 0
    assert not (ep / "assets.json").exists()

    again = REGISTRY["import_project"](ctx, {"project": "project/NoManifest"})
    assert again["progress"]["imported"] == 0 and c.execute("SELECT count(*) FROM asset").fetchone()[0] == 2


def test_unlisted_files_come_in_next_to_listed_ones(ctx, nas):
    ep = nas / "project" / "Mixed"
    (ep / "assets").mkdir(parents=True)
    (ep / "assets" / "listed.jpg").write_bytes(JPEG + b"listed")
    (ep / "assets" / "extra.jpg").write_bytes(JPEG + b"extra")
    (ep / "assets.json").write_text(json.dumps({"assets": [
        {"id": "listed", "kind": "photo", "file": "assets/listed.jpg", "source": "https://example.org/listed"}]}))
    res = REGISTRY["import_project"](ctx, {"project": "project/Mixed"})
    assert res["progress"]["imported"] == 2 and res["progress"]["not_in_assets_json"] == 1
    c = _db(ctx)
    # Listed entries back-fill usage; found files don't, so a later link doesn't write them into assets.json.
    assert [r[0] for r in c.execute("SELECT as_name FROM usage")] == ["listed"]
    # No license in assets.json is still usable.
    assert {r[0] for r in c.execute("SELECT decision FROM asset_rights")} == {"allow"}


def test_the_result_lists_what_rights_blocked(ctx, nas):
    ep = nas / "project" / "Blocked"
    (ep / "assets").mkdir(parents=True)
    (ep / "assets" / "nc.jpg").write_bytes(JPEG + b"nc")
    (ep / "assets" / "ok.jpg").write_bytes(JPEG + b"ok")
    (ep / "assets.json").write_text(json.dumps({"assets": [
        {"id": "nc", "kind": "photo", "file": "assets/nc.jpg", "license": "CC BY-NC 4.0"},
        {"id": "ok", "kind": "photo", "file": "assets/ok.jpg", "license": "CC BY 4.0"}]}))
    progress = REGISTRY["import_project"](ctx, {"project": "project/Blocked"})["progress"]
    assert progress["blocked"] == 1
    item = progress["blocked_items"][0]
    assert item["as_name"] == "nc" and item["decision"] == "deny" and item["reasons"] == ["license_nc"]


def test_a_new_assessment_updates_the_vector_rows(ctx, conn, home):
    a = ingest(conn, home, slug="v", title="V", license_id="cc-by-nc-4.0")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO embedding_space(id, fingerprint, model, revision, modalities, preprocess, prompts, "
                 "pooling, normalize, dim, metric, vec_table, active, created) "
                 "VALUES (1, 'fp', 'm', 'r', '[]', '{}', '{}', 'mean', 1, 256, 'cosine', 'vec_s1', 1, 'now')")
    conn.execute("INSERT INTO vec_target(rid, space_id, asset_id, representation_id, input_hash, created) "
                 "VALUES (1, 1, ?, ?, 'h', 'now')", (a.asset_id, a.rep_id))
    conn.execute("INSERT INTO vec_s1(rid, embedding, kind, rights, synthetic, is_segment, min_edge, duration, stale) "
                 "VALUES (1, ?, 'photo', 'deny', 0, 0, 2000, 0.0, 0)", (json.dumps([1.0] + [0.0] * 255),))
    prov = dedupe.register_provenance(conn, asset_id=a.asset_id, source="manual", external_id="v-own")
    dedupe.register_rights(conn, provenance_id=prov, asset_id=a.asset_id, policy_version=1,
                           policy_rules=policy_rules(conn), basis="own_work")
    conn.execute("COMMIT")
    assert conn.execute("SELECT rights FROM vec_s1 WHERE rid = 1").fetchone()[0] == "allow"
