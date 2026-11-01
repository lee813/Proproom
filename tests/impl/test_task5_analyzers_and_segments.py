"""Tests for Task 5: Scenecut segmenter, loudness analyzer, and flag generation."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
import pytest

from proproom.core import analysis, db, dedupe, store
from proproom.core.plugins import LoadedPlugin, PluginManager
from proproom.plugins.builtin.loudness import Plugin as LoudnessPlugin
from proproom.plugins.builtin.scenecut import Plugin as ScenecutPlugin
from proproom.sdk.context import AnalysisContext
from proproom.sdk.types import AnalysisResult, FlagResult, SegmentProposal


def test_scenecut_segmenter_with_test_video(tmp_path):
    # Create a 4-second synthetic test video using ffmpeg
    vid_path = tmp_path / "test.mp4"
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=10",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(vid_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    segmenter = ScenecutPlugin()
    ctx = AnalysisContext(
        plugin="scenecut",
        log=None,
        config={},
        open_representation=lambda rep_id: vid_path,
    )

    proposals = asyncio.run(segmenter.segment(ctx, "rep1"))
    assert len(proposals) >= 1
    for p in proposals:
        assert p.t0 >= 0
        assert p.t1 > p.t0
        assert (p.t1 - p.t0) >= 2.0
        assert p.algo == "scenecut"


def test_loudness_analyzer_with_test_audio(tmp_path):
    # Create a 3-second synthetic test audio using ffmpeg
    audio_path = tmp_path / "test.wav"
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=3",
        "-c:a", "pcm_s16le", str(audio_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    analyzer = LoudnessPlugin()
    ctx = AnalysisContext(
        plugin="loudness",
        log=None,
        config={},
        open_representation=lambda rep_id: audio_path,
    )

    res = asyncio.run(analyzer.analyze(ctx, "rep1"))
    measurements = getattr(res, "measurements", {})
    assert "lufs" in measurements
    assert isinstance(measurements["lufs"], float)


def test_analyze_representation_persists_flags_and_segments(ctx, home, tmp_path):
    # Create test video and audio
    vid_path = tmp_path / "sample.mp4"
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=5:size=320x240:rate=10",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(vid_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    conn = db.connect(home.db)
    conn.execute("BEGIN IMMEDIATE")
    sha, size, rel = store.put_file(home, vid_path, mime="video/mp4", move=False)
    dedupe.register_blob(conn, sha, size, "video/mp4", stored=True, storage_path=rel)
    aid, rid = dedupe.register_asset(conn, slug="vid-1", kind="footage", title="Sample Video")
    rep_id = dedupe.register_representation(conn, asset_id=aid, blob_sha256=sha, role="original", duration=5.0)

    # Setup plugin manager with a custom analyzer and scenecut
    class MockFlagAnalyzer:
        async def analyze(self, c, r_id):
            return AnalysisResult(flags=[FlagResult(kind="watermark", severity="warn", evidence={"test": 1})])

    pm = PluginManager(home.root, "test")
    pm.plugins["mock_flag"] = LoadedPlugin("mock_flag", "0.1", "0" * 64, ["analyzer"], "builtin", {}, MockFlagAnalyzer())
    pm.plugins["scenecut"] = LoadedPlugin("scenecut", "0.1", "0" * 64, ["segmenter"], "builtin", {}, ScenecutPlugin())
    ctx.plugin_manager = pm

    conn.execute("COMMIT")
    while analysis.step(ctx, conn, pm):
        pass

    # Verify flag persisted
    flags = conn.execute("SELECT kind, severity, target_id FROM flag WHERE target_id = ?", (rep_id,)).fetchall()
    assert len(flags) == 1
    assert flags[0]["kind"] == "watermark"
    assert flags[0]["severity"] == "warn"

    # Verify segment persisted
    segments = conn.execute("SELECT t0, t1, algo FROM segment WHERE representation_id = ?", (rep_id,)).fetchall()
    assert len(segments) >= 1
    assert segments[0]["algo"] == "scenecut"
    assert segments[0]["t1"] - segments[0]["t0"] >= 2.0
    conn.close()
