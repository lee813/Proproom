"""§11.2 / §13: analyzers and segmenters run from the worker's backlog, outside any write transaction.

Contract (decided here):
- Ingest never runs a plugin. Every stored image/video/audio representation of an active asset is analyzed later
  by `worker.tick`, once per (plugin, plugin version), recorded in `analysis_run`.
- A run replaces the plugin's earlier unreviewed flags and boxes on that representation; a reviewed flag stays.
- Core validates what a plugin returns. A result out of contract is rejected whole (nothing half-saved) and the
  pair is retried with backoff, at most `analysis.MAX_ATTEMPTS` times. Stub plugins never run.
- A plugin whose manifest `sdk` range excludes the core's SDK is not loaded, and capabilities says why.
"""
from __future__ import annotations

from support import ingest, install_source

from proproom.api.registry import REGISTRY
from proproom.core import analysis, db
from proproom.core.plugins import LoadedPlugin, sdk_compatible
from proproom.sdk import SDK_VERSION
from proproom.sdk.types import (AnalysisResult, FlagResult, RepresentationOffer, ResolvedSource, RightsEvidence,
                                SegmentProposal)


class _Analyzer:
    def __init__(self, result=None, fail=None):
        self.calls, self.result, self.fail = [], result, fail

    async def analyze(self, ctx, rep_id):
        self.calls.append(rep_id)
        if self.fail:
            raise RuntimeError(self.fail)
        return self.result or AnalysisResult(
            flags=[FlagResult(kind="watermark", severity="warn", score=0.9)],
            regions=[{"kind": "face", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4, "score": 0.8}],
            tags=["harbour"])


def _install(ctx, name, inst, *, types=("analyzer",), status="ready", version="1.0"):
    pm = ctx.plugin_manager or install_source(ctx, "fake", object())
    pm.plugins[name] = LoadedPlugin(name, version, "0" * 64, list(types), "dir",
                                    {"plugin": {"status": status}}, inst)
    return pm


def _drain(ctx, limit=50):
    from proproom.core import worker
    for _ in range(limit):
        if not worker.tick(ctx):
            return
    raise AssertionError("worker never went idle")


def test_ingest_runs_no_plugin_and_the_worker_analyzes_once(ctx, conn, home, fake_download):
    a = _Analyzer()
    pm = install_source(ctx, "fake", _FetchSrc())
    _install(ctx, "probe", a)
    res = REGISTRY["fetch"](ctx, {"source": "fake", "external_id": "P"})
    assert a.calls == []
    assert analysis.backlog(conn, pm) == 1
    _drain(ctx)
    _drain(ctx)
    assert len(a.calls) == 1
    rep = conn.execute("SELECT id FROM representation WHERE asset_id = ?", (res["asset_id"],)).fetchone()["id"]
    assert conn.execute("SELECT kind FROM flag WHERE target_id = ?", (rep,)).fetchone()["kind"] == "watermark"
    face = conn.execute("SELECT kind, x, w, origin, plugin FROM region WHERE representation_id = ?", (rep,)).fetchone()
    assert tuple(face) == ("face", 0.1, 0.3, "machine", "probe")
    assert conn.execute("SELECT origin FROM tag WHERE asset_id = ? AND tag = 'harbour'",
                        (res["asset_id"],)).fetchone()[0] == "machine"
    assert conn.execute("SELECT state FROM analysis_run WHERE representation_id = ?", (rep,)).fetchone()[0] == "done"
    assert analysis.backlog(conn, pm) == 0


def test_a_new_plugin_version_analyzes_again(ctx, conn, home):
    ingest(conn, home, slug="v", title="V")
    a = _Analyzer()
    _install(ctx, "probe", a, version="1.0")
    _drain(ctx)
    _install(ctx, "probe", a, version="1.1")
    _drain(ctx)
    assert len(a.calls) == 2


def test_a_failing_analyzer_is_recorded_and_retried_with_backoff(ctx, conn, home):
    ingest(conn, home, slug="f", title="F")
    a = _Analyzer(fail="model not downloaded")
    _install(ctx, "probe", a)
    _drain(ctx)
    row = conn.execute("SELECT state, attempts, error, next_attempt FROM analysis_run").fetchone()
    assert row["state"] == "failed" and row["attempts"] == 1 and "model not downloaded" in row["error"]
    assert row["next_attempt"] and len(a.calls) == 1  # not retried before the backoff is due
    for _ in range(analysis.MAX_ATTEMPTS + 2):
        conn.execute("UPDATE analysis_run SET next_attempt = '2000-01-01T00:00:00.000Z'")
        conn.commit()
        _drain(ctx)
    assert len(a.calls) == analysis.MAX_ATTEMPTS


def test_an_out_of_contract_result_saves_nothing(ctx, conn, home):
    ingest(conn, home, slug="bad", title="Bad")
    bad = AnalysisResult(flags=[FlagResult(kind="logo", severity="warn")],
                         regions=[{"kind": "tattoo", "x": 0, "y": 0, "w": 0.5, "h": 0.5}])
    _install(ctx, "probe", _Analyzer(result=bad))
    _drain(ctx)
    assert conn.execute("SELECT count(*) FROM flag").fetchone()[0] == 0
    assert "tattoo" in conn.execute("SELECT error FROM analysis_run").fetchone()[0]


def test_stub_plugins_never_run(ctx, conn, home):
    ingest(conn, home, slug="s", title="S")
    a = _Analyzer()
    _install(ctx, "probe", a, status="stub")
    _drain(ctx)
    assert a.calls == []


def test_core_rejects_segments_shorter_than_two_seconds(ctx, conn, home):
    ingest(conn, home, slug="clip", title="Clip", kind="footage", mime="video/mp4")

    class Seg:
        async def segment(self, ctx_, rep_id):
            return [SegmentProposal(t0=0.0, t1=1.0, algo="probe", algo_version="1", params={})]

    _install(ctx, "seg", Seg(), types=("segmenter",))
    _drain(ctx)
    assert conn.execute("SELECT count(*) FROM segment").fetchone()[0] == 0
    assert conn.execute("SELECT state FROM analysis_run WHERE plugin = 'seg'").fetchone()[0] == "failed"


def test_sdk_is_frozen_at_1_and_ranges_are_enforced(ctx, home):
    assert SDK_VERSION.split(".")[0] == "1"
    assert sdk_compatible(">=1.0,<2") and sdk_compatible(None) and not sdk_compatible(">=2.0")
    d = home.dir("plugins") / "future"
    d.mkdir()
    (d / "plugin.toml").write_text('[plugin]\nname = "future"\nversion = "1"\nsdk = ">=2.0,<3"\ntypes = ["hook"]\n')
    (d / "__init__.py").write_text("class Plugin:\n    pass\n")
    caps = REGISTRY["capabilities"](ctx, {"schemas": False})
    assert "future" not in {p["name"] for p in caps["plugins"]}
    assert "future" in caps["plugins_rejected"]


class _FetchSrc:
    async def resolve(self, ctx_, ref):
        return ResolvedSource(ref=ref, title="T", kind="photo", metadata={},
                              evidence=[RightsEvidence(basis="license", license_id="CC BY 4.0")],
                              offers=[RepresentationOffer("original", "image/jpeg", "https://example.org/p.jpg")])


def test_a_new_version_replaces_its_unreviewed_output_and_keeps_reviews(ctx, conn, home):
    ingest(conn, home, slug="r", title="R")
    two = AnalysisResult(flags=[FlagResult(kind="watermark", severity="warn"), FlagResult(kind="logo", severity="warn")],
                         regions=[{"kind": "face", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}])
    _install(ctx, "probe", _Analyzer(result=two), version="1.0")
    _drain(ctx)
    conn.execute("UPDATE flag SET resolution = 'dismissed', resolved_by = 'editor' WHERE kind = 'logo'")
    conn.commit()
    none = AnalysisResult(flags=[FlagResult(kind="logo", severity="warn")])
    _install(ctx, "probe", _Analyzer(result=none), version="1.1")
    _drain(ctx)
    flags = conn.execute("SELECT kind, resolution FROM flag ORDER BY kind").fetchall()
    assert [tuple(f) for f in flags] == [("logo", "dismissed")]  # stale watermark gone, the dismissal is not re-raised
    assert conn.execute("SELECT count(*) FROM region").fetchone()[0] == 0
