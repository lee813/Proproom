"""End-to-end media checks in the isolated fnos test container."""
import asyncio
import logging
import math
import os
import subprocess

import pytest
from PIL import Image, ImageDraw
from proproom.sdk.context import AnalysisContext, EmbedContext
from proproom.plugins.builtin.scenecut import Plugin as Scenecut
from proproom.plugins.builtin.loudness import Plugin as Loudness
from proproom.plugins.builtin.embedgemma2 import Plugin as Embedder


def test_analyzers_on_generated_av_clip(tmp_path):
    clip = tmp_path / "two-scenes.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=red:s=160x120:r=10:d=4",
        "-f", "lavfi", "-i", "color=blue:s=160x120:r=10:d=4",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=8",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(clip),
    ], check=True, capture_output=True)
    ctx = AnalysisContext(plugin="clip-test", log=logging.getLogger("test"), config={},
                          open_representation=lambda _: clip)
    scenes = asyncio.run(Scenecut().segment(ctx, "clip"))
    result = asyncio.run(Loudness().analyze(ctx, "clip"))
    assert len(scenes) >= 2
    assert any(abs(segment.t1 - 4) < 0.5 for segment in scenes)
    assert all(math.isfinite(result.measurements[name]) for name in ("lufs", "lra", "true_peak"))


@pytest.mark.skipif(os.environ.get("PROP_REAL_MODEL") != "1", reason="set PROP_REAL_MODEL=1 for image embedding")
def test_real_model_image_and_text_similarity(tmp_path):
    img = Image.new("RGB", (256, 256), "white")
    ImageDraw.Draw(img).rectangle((40, 40, 216, 216), fill="red")
    image_path = tmp_path / "red-square.png"
    img.save(image_path)
    from proproom.core import hardware
    hw = hardware.detect()
    gpu = hw["tier"] == "nvidia"
    ctx = EmbedContext(plugin="embedgemma2", log=logging.getLogger("test"), config={},
                       device="cuda" if gpu else "cpu", dtype=hw["devices"][0]["dtype"] if gpu else "fp32",
                       models_dir=str(tmp_path / "models"))
    embedder = Embedder()
    batch = asyncio.run(embedder.embed(ctx, [
        {"image": str(image_path), "prompt": "document"},
        {"text": "a red square on a white background", "prompt": "query"},
        {"text": "a photograph of a herd of elephants on a grassy plain", "prompt": "query"},
    ]))
    image, matching, unrelated = batch.vectors
    dot = lambda a, b: sum(x*y for x, y in zip(a, b))
    assert len(image) == 256 and abs(sum(x*x for x in image) - 1) < 1e-3
    assert dot(image, matching) > dot(image, unrelated)
    video_path = tmp_path / "red-square.mp4"
    subprocess.run(["ffmpeg", "-y", "-loop", "1", "-framerate", "5", "-i", str(image_path),
                    "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video_path)],
                   capture_output=True, check=True)
    video_batch = asyncio.run(embedder.embed(ctx, [{"video": str(video_path), "prompt": "document"}]))
    video = video_batch.vectors[0]
    assert len(video) == 256 and abs(sum(x*x for x in video) - 1) < 1e-3
    assert dot(video, matching) > dot(video, unrelated)
