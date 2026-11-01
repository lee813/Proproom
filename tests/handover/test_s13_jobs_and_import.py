"""§4.3 pause rules, §13 jobs, §15 stage 2 `import_project` against real-world manifest shapes."""
from __future__ import annotations

import json

import pytest
from support import gap

from proproom.api.registry import REGISTRY
from proproom.core import db, jobs


class _Load:
    def __init__(self, text):
        self.text = text

    def __call__(self, p):  # stands in for pathlib.Path inside core.jobs
        from pathlib import Path
        if str(p) == "/proc/loadavg":
            return self
        return Path(p)

    def is_file(self):
        return True

    def read_text(self):
        return self.text


def test_lockfile_pauses(home, nas, monkeypatch):
    """§4.3: a configured lock file under /nas pauses pull/index."""
    monkeypatch.setattr(jobs, "Path", _Load("0.1 0.1 0.1 1/1 1"))
    (nas / "tools").mkdir()
    (nas / "tools" / "RENDERING").touch()
    paused, why = jobs.should_pause(home, {"pause": {"lockfiles": ["tools/RENDERING"]}})
    assert paused and "lockfile" in why


def test_load_pause_uses_host_cores(home, monkeypatch):
    """§4.3: /proc/loadavg is the host's load; divide by the host's core count."""
    monkeypatch.setattr(jobs, "Path", _Load("2.4 2.0 2.0 1/300 1"))
    monkeypatch.setattr(jobs.os, "cpu_count", lambda: 12)
    assert jobs.should_pause(home, {"pause": {"load_per_core": 0.8}}) == (False, None)


def test_pull_job_makes_progress(ctx, nas):
    (nas / "pull.yaml").write_text("budget: 1M\njobs: []\n")
    job = REGISTRY["pull"](ctx, {"spec": "pull.yaml"})
    got = REGISTRY["job"](ctx, {"action": "get", "job_id": job["job_id"]})
    assert got["state"] in ("queued", "succeeded")


def test_job_actions_reject_unknown_ids(ctx):
    from proproom.sdk.errors import ProproomError
    with pytest.raises(ProproomError):
        REGISTRY["job"](ctx, {"action": "cancel", "job_id": "01NOSUCHJOBAAAAAAAAAAAAAAA"})


# ───────────────────────── import_project (stage 2) ─────────────────────────

def _episode(nas, name, manifest, files):
    ep = nas / "project" / name
    (ep / "assets").mkdir(parents=True)
    for rel, data in files.items():
        (ep / rel).write_bytes(data)
    (ep / "assets.json").write_text(json.dumps(manifest))
    return ep


def _db(ctx):
    return db.connect(ctx.booted.home.db)


def test_import_backfills_usage_and_dedupes_blobs(ctx, nas):
    _episode(nas, "Ep1", {"assets": [
        {"id": "a", "kind": "photo", "file": "assets/a.jpg", "license": "CC BY 4.0", "source": "https://x/a"},
        {"id": "b", "kind": "photo", "file": "assets/b.jpg", "license": "CC0", "source": "https://x/b"}]},
        {"assets/a.jpg": b"same", "assets/b.jpg": b"same"})
    REGISTRY["import_project"](ctx, {"project": "project/Ep1"})
    c = _db(ctx)
    assert c.execute("SELECT count(*) FROM blob").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM usage WHERE origin = 'import'").fetchone()[0] == 2


def test_import_accepts_list_manifests(ctx, nas):
    _episode(nas, "EpList", [{"id": "a", "kind": "photo", "file": "assets/a.jpg", "license": "CC0"}],
             {"assets/a.jpg": b"a"})
    assert REGISTRY["import_project"](ctx, {"project": "project/EpList"})["progress"]["imported"] == 1


def test_import_tolerates_legacy_kinds(ctx, nas):
    _episode(nas, "EpLegacy", {"assets": [
        {"id": "b", "kind": "broll", "file": "assets/b.webm", "license": "CC BY-SA 4.0"},
        {"id": "o", "kind": "observation", "file": "assets/o.png", "license": "CC0"},
        {"id": "n", "file": "assets/n.jpg", "license": "CC0"}]},
        {"assets/b.webm": b"b", "assets/o.png": b"o", "assets/n.jpg": b"n"})
    REGISTRY["import_project"](ctx, {"project": "project/EpLegacy"})
    c = _db(ctx)
    assert c.execute("SELECT kind FROM asset WHERE slug LIKE '%-b'").fetchone()[0] == "footage"
    assert c.execute("SELECT count(*) FROM asset").fetchone()[0] == 3


def test_failed_import_does_not_leave_a_running_job(ctx, nas):
    _episode(nas, "EpBad", {"assets": [{"id": "x", "kind": "not-a-kind", "file": "assets/x.jpg"}]},
             {"assets/x.jpg": b"x"})
    with pytest.raises(Exception):
        REGISTRY["import_project"](ctx, {"project": "project/EpBad"})
    assert _db(ctx).execute("SELECT count(*) FROM job WHERE state = 'running'").fetchone()[0] == 0


def test_import_records_media_types(ctx, nas):
    _episode(nas, "EpMime", {"assets": [
        {"id": "v", "kind": "footage", "file": "assets/v.webm", "license": "CC BY-SA 4.0"},
        {"id": "p", "kind": "photo", "file": "assets/p.png", "license": "CC0"}]},
        {"assets/v.webm": b"\x1aE\xdf\xa3webm", "assets/p.png": b"\x89PNG\r\n\x1a\npng"})
    REGISTRY["import_project"](ctx, {"project": "project/EpMime"})
    assert {r[0] for r in _db(ctx).execute("SELECT mime FROM blob")} == {"video/webm", "image/png"}


def test_import_classifies_basis(ctx, nas):
    _episode(nas, "EpBasis", {"assets": [
        {"id": "pd", "kind": "portrait", "file": "assets/pd.jpg", "license": "Public domain"},
        {"id": "q", "kind": "document", "file": "assets/q.png",
         "license": "Official record of the parliament (public debate); quoted with source"}]},
        {"assets/pd.jpg": b"pd", "assets/q.png": b"q"})
    REGISTRY["import_project"](ctx, {"project": "project/EpBasis"})
    rows = dict(_db(ctx).execute("SELECT a.slug, re.basis FROM asset a JOIN provenance p ON p.asset_id = a.id "
                                 "JOIN rights_evidence re ON re.provenance_id = p.id").fetchall())
    assert rows["EpBasis-pd"] == "public_domain" and rows["EpBasis-q"] == "quotation"


def test_import_is_idempotent(ctx, nas):
    _episode(nas, "EpTwice", {"assets": [{"id": "a", "kind": "photo", "file": "assets/a.jpg", "license": "CC0"}]},
             {"assets/a.jpg": b"a"})
    REGISTRY["import_project"](ctx, {"project": "project/EpTwice"})
    second = REGISTRY["import_project"](ctx, {"project": "project/EpTwice"})
    c = _db(ctx)
    assert c.execute("SELECT count(*) FROM rights_evidence").fetchone()[0] == 1
    assert second["progress"]["imported"] == 0
