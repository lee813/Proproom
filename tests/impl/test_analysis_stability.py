"""Prevent repeated worker crashes, and decode big frames scaled down instead of at full size."""
import io
import json
import subprocess
import warnings

import pytest
from PIL import Image

from proproom.core import analysis
from proproom.core.plugins import LoadedPlugin
from proproom.sdk import media
from proproom.sdk.errors import ProproomError
from proproom.sdk.types import AnalysisResult
from support import ingest, install_source


@pytest.mark.parametrize("size,scaled", [((5000, 2000), False), ((5000, 2001), True)])
def test_ten_megapixel_boundary(tmp_path, size, scaled):
    path = tmp_path / "boundary.png"
    Image.new("RGB", size).save(path)
    (t, image), = media.frames(path, [0.5])
    assert t is None
    if scaled:
        w, h = image.size
        assert w * h <= media.MAX_ANALYSIS_PIXELS and abs(w / h - size[0] / size[1]) < 0.01
    else:
        assert image.size == size


def test_video_with_unknown_dimensions_is_refused_before_decode(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "probe", lambda _: {"kind": "video", "width": None, "height": None, "duration": 1})
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **kw: pytest.fail("decoded a frame of unknown size"))
    with pytest.raises(ProproomError) as exc:
        media.frames(tmp_path / "clip.mp4", [0.5])
    assert exc.value.code == "unsupported_media"


@pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
def test_oversized_image_is_decoded_scaled_down(tmp_path, monkeypatch, fmt):
    path = tmp_path / f"frame.{fmt.lower()}"
    Image.new("RGB", (400, 300), "red").save(path, fmt)
    monkeypatch.setattr(media, "MAX_ANALYSIS_PIXELS", 10_000)
    (_, image), = media.frames(path, [0.5])
    w, h = image.size
    assert image.mode == "RGB" and w * h <= 10_000 and abs(w / h - 4 / 3) < 0.05


def test_frame_over_the_decompression_bomb_limit_is_refused_without_a_warning(tmp_path, monkeypatch):
    path = tmp_path / "huge.png"
    Image.new("RGB", (40, 30)).save(path)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)  # Pillow warns past this
    monkeypatch.setattr(media, "MAX_FRAME_PIXELS", 1000)
    monkeypatch.setattr(Image.Image, "convert", lambda *a, **kw: pytest.fail("decoded a refused frame"))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(ProproomError) as exc:
            media.frames(path, [0.5])
    assert exc.value.code == "analysis_frame_too_large" and "40x30" in exc.value.message


def test_oversized_video_frame_is_scaled_by_ffmpeg(monkeypatch, tmp_path):
    png = io.BytesIO()
    Image.new("RGB", (22, 22)).save(png, "PNG")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=png.getvalue(), stderr=b"")

    monkeypatch.setattr(media, "probe", lambda _: {"kind": "video", "width": 100, "height": 100, "duration": 1})
    monkeypatch.setattr(media, "MAX_ANALYSIS_PIXELS", 500)
    monkeypatch.setattr(media.subprocess, "run", run)
    (_, image), = media.frames(tmp_path / "clip.mp4", [0.5])
    assert image.size == (22, 22)
    assert calls[0][calls[0].index("-vf") + 1] == "scale=22:22"


def test_interrupted_attempt_is_recorded_and_eventually_stops_retrying(ctx, conn, home):
    class Crashes:
        async def analyze(self, ctx_, rep_id):
            raise SystemExit("simulated process death")

    ingest(conn, home, slug="one", title="One")
    pm = install_source(ctx, "fake", object())
    pm.plugins["crashes"] = LoadedPlugin("crashes", "1", "0" * 64, ["analyzer"], "dir",
                                        {"plugin": {"status": "ready"}}, Crashes())
    for attempt in range(1, analysis.MAX_ATTEMPTS + 1):
        with pytest.raises(SystemExit):
            analysis.step(ctx, conn, pm)
        row = conn.execute("SELECT state, attempts, error, next_attempt FROM analysis_run WHERE plugin = 'crashes'").fetchone()
        assert row["state"] == "failed" and row["attempts"] == attempt
        assert json.loads(row["error"])["code"] == "interrupted" and row["next_attempt"]
        assert not analysis.step(ctx, conn, pm)
        conn.execute("UPDATE analysis_run SET next_attempt = '2000-01-01T00:00:00Z'")
    assert not analysis.step(ctx, conn, pm)


def test_successful_attempt_replaces_interruption_marker(ctx, conn, home):
    class Works:
        async def analyze(self, ctx_, rep_id):
            return AnalysisResult()

    ingest(conn, home, slug="two", title="Two")
    pm = install_source(ctx, "fake", object())
    pm.plugins["works"] = LoadedPlugin("works", "1", "0" * 64, ["analyzer"], "dir",
                                      {"plugin": {"status": "ready"}}, Works())
    assert analysis.step(ctx, conn, pm)
    row = conn.execute("SELECT state, attempts, error, next_attempt FROM analysis_run WHERE plugin = 'works'").fetchone()
    assert tuple(row) == ("done", 1, None, None)
