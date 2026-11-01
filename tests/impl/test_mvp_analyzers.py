"""The built-in region and OCR analyzers on generated media (fnos image: ffmpeg + tesseract; OWLv2 on the GPU)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw, ImageFont

from proproom.plugins.builtin.ocr_flags import Plugin as OcrFlags
from proproom.plugins.builtin.regions import Plugin as Regions
from proproom.sdk.context import AnalysisContext

needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")


def _ctx(path, config=None):
    return AnalysisContext(plugin="t", log=logging.getLogger("t"), config=config or {},
                           open_representation=lambda _: path)


def _font(size):
    return ImageFont.load_default(size=size)


def _frame(subtitle=None, corner=None, size=(960, 540)):
    im = Image.new("RGB", size, (40, 90, 140))
    d = ImageDraw.Draw(im)
    d.ellipse((380, 120, 580, 320), fill=(200, 160, 60))
    if subtitle:
        d.rectangle((120, 440, 840, 510), fill=(0, 0, 0))
        d.text((480, 475), subtitle, font=_font(40), fill="white", anchor="mm")
    if corner:
        d.text((20, 20), corner, font=_font(36), fill="white")
    return im


def _video(tmp_path, frames, name="clip.mp4"):
    for i, im in enumerate(frames):
        im.save(tmp_path / f"f{i:03d}.png")
    out = tmp_path / name
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-framerate", "1", "-i", str(tmp_path / "f%03d.png"),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "10", str(out)], check=True)
    for i in range(len(frames)):
        (tmp_path / f"f{i:03d}.png").unlink()
    return out


def _flags(path):
    return {f.kind: f for f in asyncio.run(OcrFlags().analyze(_ctx(path), "rep")).flags}


@needs_tesseract
def test_changing_bottom_text_is_burned_subs(tmp_path):
    lines = ["THE HARBOUR OPENS AT DAWN", "CRANES LIFT THE CONTAINERS", "SHIPS LEAVE FOR ROTTERDAM",
             "THE NIGHT SHIFT BEGINS NOW", "WORKERS GO HOME AT SIX"]
    flags = _flags(_video(tmp_path, [_frame(subtitle=s) for s in lines]))
    assert "burned_subs" in flags and flags["burned_subs"].severity == "warn"
    assert "watermark" not in flags


@needs_tesseract
def test_a_clean_clip_has_no_flags(tmp_path):
    assert _flags(_video(tmp_path, [_frame() for _ in range(5)])) == {}


@needs_tesseract
def test_fixed_corner_text_is_a_watermark_not_subtitles(tmp_path):
    flags = _flags(_video(tmp_path, [_frame(corner="NEWS 24") for _ in range(5)]))
    assert "watermark" in flags and "burned_subs" not in flags
    assert flags["watermark"].evidence["corner"] == "top_left"


@needs_tesseract
def test_a_still_with_corner_text_is_a_watermark(tmp_path):
    p = tmp_path / "still.png"
    _frame(corner="STOCKPIX").save(p)
    assert "watermark" in _flags(p)


@needs_tesseract
def test_a_page_of_text_is_not_a_watermark(tmp_path):
    page = Image.new("RGB", (960, 1280), "white")
    d = ImageDraw.Draw(page)
    for i, y in enumerate(range(40, 1240, 60)):
        d.text((30, y), f"LINE {i} OF THE PRINTED CHAPTER ABOUT CENTRAL POWER", font=_font(30), fill="black")
    p = tmp_path / "page.png"
    page.save(p)
    assert "watermark" not in _flags(p)


@needs_tesseract
def test_photo_texture_is_not_read_as_text(tmp_path):
    import random
    rnd = random.Random(7)
    im = Image.new("RGB", (960, 540))
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(960 * 540)])
    p = tmp_path / "noise.png"
    im.save(p)
    assert _flags(p) == {}


@pytest.mark.skipif(os.environ.get("PROP_REAL_MODEL") != "1", reason="set PROP_REAL_MODEL=1 to load OWLv2")
def test_regions_finds_a_number_plate_and_nothing_on_a_blank_frame(tmp_path):
    car = Image.new("RGB", (1024, 768), (90, 90, 95))
    d = ImageDraw.Draw(car)
    d.rounded_rectangle((212, 300, 812, 600), radius=60, fill=(150, 20, 20))      # car body
    d.ellipse((260, 540, 400, 680), fill=(20, 20, 20))
    d.ellipse((624, 540, 764, 680), fill=(20, 20, 20))
    d.rectangle((412, 470, 612, 530), fill="white", outline="black", width=4)      # plate
    d.text((512, 500), "AB 123 CD", font=_font(34), fill="black", anchor="mm")
    p = tmp_path / "car.png"
    car.save(p)
    res = asyncio.run(Regions().analyze(_ctx(p), "rep"))
    plates = [r for r in res.regions if r["kind"] == "plate"]
    assert plates, res.regions
    best = max(plates, key=lambda r: r["score"])
    cx, cy = best["x"] + best["w"] / 2, best["y"] + best["h"] / 2
    assert 0.35 < cx < 0.65 and 0.55 < cy < 0.75
    blank = tmp_path / "blank.png"
    Image.new("RGB", (800, 600), (128, 128, 128)).save(blank)
    assert asyncio.run(Regions().analyze(_ctx(blank), "rep")).regions == []


def test_an_mpo_camera_jpeg_is_one_still(tmp_path):
    from proproom.sdk.media import frames, probe
    p = tmp_path / "cam.jpg"
    main, preview = _frame(), _frame(size=(320, 180))
    main.save(p, format="MPO", save_all=True, append_images=[preview])
    assert Image.open(p).format == "MPO" and Image.open(p).n_frames == 2
    assert probe(p)["kind"] == "image"
    (t, im), = frames(p, [0.5])
    assert t is None and im.size == (960, 540)


def test_a_silent_clip_has_no_loudness_and_is_not_a_failure(tmp_path):
    from proproom.plugins.builtin.loudness import Plugin as Loudness
    clip = _video(tmp_path, [_frame() for _ in range(3)])
    res = asyncio.run(Loudness().analyze(_ctx(clip), "rep"))
    assert res.measurements == {}


def test_a_clip_whose_audio_is_digital_silence_has_no_loudness(tmp_path):
    from proproom.plugins.builtin.loudness import Plugin as Loudness
    p = tmp_path / "hush.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=3",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3", "-c:v", "libx264",
                    "-c:a", "aac", str(p)], check=True)
    assert asyncio.run(Loudness().analyze(_ctx(p), "rep")).measurements == {}


def test_a_clip_with_sound_is_measured(tmp_path):
    from proproom.plugins.builtin.loudness import Plugin as Loudness
    p = tmp_path / "tone.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest", "-c:v", "libx264",
                    "-c:a", "aac", str(p)], check=True)
    m = asyncio.run(Loudness().analyze(_ctx(p), "rep")).measurements
    assert -40 < m["lufs"] < 0 and m["true_peak"] <= 1


def test_a_proproom_error_is_recorded_by_code_and_message(ctx, conn, home):
    from support import ingest, install_source
    from proproom.core import analysis
    from proproom.core.plugins import LoadedPlugin
    from proproom.sdk.errors import ProproomError

    class Broken:
        async def analyze(self, ctx_, rep_id):
            raise ProproomError("unsupported_media", "no frames here")

    ingest(conn, home, slug="e", title="E")
    pm = install_source(ctx, "fake", object())
    pm.plugins["broken"] = LoadedPlugin("broken", "1", "0" * 64, ["analyzer"], "dir", {"plugin": {"status": "ready"}},
                                        Broken())
    assert analysis.step(ctx, conn, pm)
    err = conn.execute("SELECT error FROM analysis_run WHERE plugin = 'broken'").fetchone()[0]
    assert json.loads(err) == {"code": "unsupported_media", "message": "no frames here"}
