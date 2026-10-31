"""Bootstraps a library in a temp dir and runs the appendix-B queries against sample data."""
from __future__ import annotations

import json
import sqlite3

import pytest
import sqlite_vec

from proproom.core import bootstrap, cjk, db
from proproom.core.ids import ulid, utcnow, utcnow_monotonic
from proproom.core.paths import Home


@pytest.fixture()
def booted(tmp_path, monkeypatch):
    monkeypatch.delenv("PROP_PUBLIC_URL", raising=False)
    home = Home(tmp_path / "lib", tmp_path, "env", {})
    b = bootstrap.boot(home)
    conn = db.connect(home.db)
    yield b, conn
    conn.close()


def _vec(*xs: float) -> bytes:
    v = list(xs) + [0.0] * (256 - len(xs))
    return sqlite_vec.serialize_float32(v)


def _asset(conn, rid, slug, kind, title, *, cjk_text="", decision="allow", basis="license", width=2000,
           vec=None, policy=1):
    now = utcnow()
    aid, rep, prov, ev = ulid(), ulid(), ulid(), ulid()
    sha = f"{rid:064x}"
    conn.execute("INSERT INTO asset(id, rid, slug, kind, title, created, updated) VALUES (?,?,?,?,?,?,?)",
                 (aid, rid, slug, kind, title, now, now))
    conn.execute("INSERT INTO blob(sha256, bytes, mime, stored, storage_path, created) VALUES (?,?,?,?,?,?)",
                 (sha, 1000, "image/jpeg", 1, f"blobs/{sha[:2]}/{sha}", now))
    conn.execute("INSERT INTO representation(id, asset_id, blob_sha256, role, width, height, created) "
                 "VALUES (?,?,?,?,?,?,?)", (rep, aid, sha, "original", width, width, now))
    conn.execute("INSERT INTO provenance(id, asset_id, source, external_id, retrieved, raw_metadata, plugin, "
                 "plugin_version, plugin_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                 (prov, aid, "commons", f"File:{slug}.jpg", now, "{}", "commons", "0.0.1", "x" * 64))
    conn.execute("INSERT INTO rights_evidence(id, provenance_id, basis, license_id, origin, created_by, created) "
                 "VALUES (?,?,?,?,?,?,?)", (ev, prov, basis, "cc-by-sa-4.0", "source", "commons", now))
    conn.execute("INSERT INTO rights_assessment(id, asset_id, policy_version, decision, basis, reasons, "
                 "evidence_ids, assessed_at, assessed_by) VALUES (?,?,?,?,?,?,?,?,?)",
                 (ulid(), aid, policy, decision, basis, "[]", json.dumps([ev]), utcnow_monotonic(), "core"))
    conn.execute("INSERT INTO search_doc(rid, title, cjk) VALUES (?,?,?)", (rid, title, cjk_text))
    if vec is not None:
        conn.execute("INSERT INTO vec_target(rid, space_id, asset_id, representation_id, input_hash, created) "
                     "VALUES (?,?,?,?,?,?)", (rid, 1, aid, rep, "h", now))
        conn.execute("INSERT INTO vec_s1(rid, embedding, kind, rights, synthetic, is_segment, min_edge, duration, "
                     "stale) VALUES (?,?,?,?,?,?,?,?,?)", (rid, vec, kind, decision, 0, 0, width, 0.0, 0))
    return aid


def test_bootstrap_layout(booted):
    b, conn = booted
    for d in ("blobs", "thumbs", "models", "plugins", "tmp", "journal", "backups"):
        assert b.home.dir(d).is_dir()
    assert b.home.config.is_file() and b.home.token.is_file()
    assert (b.home.token.stat().st_mode & 0o777) == 0o600
    assert db.user_version(conn) == db.latest_version() == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert b.policy_version == 1
    # idempotent second boot: no new policy version, no migrations
    again = bootstrap.boot(b.home)
    assert again.applied == [] and again.policy_version == 1 and not again.created


def test_strict_and_fk(booted):
    _, conn = booted
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO representation(id, asset_id, blob_sha256, role, created) VALUES (?,?,?,?,?)",
                     (ulid(), ulid(), "0" * 64, "original", utcnow()))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO asset(id, rid, slug, kind, title, created, updated) VALUES (?,?,?,?,?,?,?)",
                     (ulid(), 1, "x", "nope", "x", utcnow(), utcnow()))


def test_hybrid_recall(booted):
    _, conn = booted
    conn.execute("INSERT INTO embedding_space(id, fingerprint, model, revision, modalities, preprocess, prompts, "
                 "pooling, normalize, dim, metric, vec_table, active, created) VALUES "
                 "(1,'fp','google/embeddinggemma-2','r','[\"text\",\"image\"]','{}','{}','mean',1,256,'cosine',"
                 "'vec_s1',1,?)", (utcnow(),))
    toc = _asset(conn, 1, "commons-tocqueville-1850", "portrait", "Alexis de Tocqueville portrait",
                 cjk_text="托克维尔 肖像", vec=_vec(1.0, 0.1))
    _asset(conn, 2, "commons-lepen", "portrait", "Marine Le Pen", cjk_text="勒庞", vec=_vec(0.1, 1.0))
    _asset(conn, 3, "commons-denied", "portrait", "Tocqueville NC copy", decision="deny", vec=_vec(1.0, 0.0))
    _asset(conn, 4, "commons-small", "portrait", "Tocqueville thumbnail", width=400, vec=_vec(1.0, 0.2))

    q = cjk.rewrite("托克维尔肖像")
    assert q.match == '"托克维" OR "克维尔" OR "维尔肖" OR "尔肖像"'
    rows = conn.execute(db.sql("hybrid_recall"), {
        "qvec": _vec(1.0, 0.0), "kv": 10, "kf": 10, "k": 12, "min_edge": 1000,
        "q": "tocqueville", "qcjk": q.match, "include_quotation": 0, "project": None,
    }).fetchall()
    ids = [r["asset_id"] for r in rows]
    assert ids[0] == toc                      # matched by all three channels
    assert set(json.loads(rows[0]["ranks"])) == {"vec", "fts", "cjk"}
    assert rows[0]["score_type"] == "rrf"
    slugs = {r["slug"] for r in rows}
    assert "commons-denied" not in slugs      # rights filter
    assert "commons-tocqueville-1850" in slugs

    # 2-char CJK falls back to LIKE
    q2 = cjk.rewrite("勒庞")
    assert q2.match is None and q2.like == ["勒庞"]
    hit = conn.execute("SELECT rid FROM search_doc WHERE cjk LIKE ?", (f"%{q2.like[0]}%",)).fetchall()
    assert [r[0] for r in hit] == [2]


def test_idempotency_claim(booted):
    _, conn = booted
    claim = ("INSERT INTO operation(id, kind, idempotency_key, request_hash, request, state, actor, created) "
             "VALUES (?, 'link', 'k1', 'h1', '{}', 'planned', 'test', ?) ON CONFLICT(kind, idempotency_key) DO NOTHING")
    first = ulid()
    conn.execute(claim, (first, utcnow()))
    conn.execute(claim, (ulid(), utcnow()))
    rows = conn.execute("SELECT id FROM operation WHERE kind='link' AND idempotency_key='k1'").fetchall()
    assert [r[0] for r in rows] == [first]
