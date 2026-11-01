"""Frame access for analyzer plugins: still images directly, videos through ffprobe/ffmpeg (handover §13)."""
from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .errors import ProproomError


def probe(path: Path) -> dict:
    """{"kind": "image"|"video"|"audio", "duration": seconds or None, "width": int|None, "height": int|None,
    "audio": bool}."""
    try:
        with Image.open(path) as im:
            # An MPO (camera JPEG with an embedded preview) has two frames but is one still, not an animation.
            if getattr(im, "n_frames", 1) == 1 or im.format == "MPO":
                return {"kind": "image", "duration": None, "width": im.width, "height": im.height, "audio": False}
    except (UnidentifiedImageError, OSError):
        pass
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                          "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise ProproomError("unsupported_media", f"ffprobe cannot read {path.name}: {res.stderr.strip()[:200]}")
    info = json.loads(res.stdout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    duration = float(info["format"]["duration"]) if info.get("format", {}).get("duration") else None
    if video:
        return {"kind": "video", "duration": duration, "width": video.get("width"), "height": video.get("height"),
                "audio": audio}
    if audio:
        return {"kind": "audio", "duration": duration, "width": None, "height": None, "audio": True}
    raise ProproomError("unsupported_media", f"{path.name} has no image, video or audio stream")


def decode_timeout(duration: float | None) -> float:
    """Seconds to allow one full decode pass (scene or loudness scan): a minute, plus the media's own length."""
    return 60.0 + (duration or 0.0)


def frames(path: Path, positions: list[float]) -> list[tuple[float | None, Image.Image]]:
    """RGB frames at the given fractions of a video's duration, or the image itself once (time None)."""
    info = probe(path)
    if info["kind"] == "image":
        with Image.open(path) as im:
            return [(None, im.convert("RGB"))]
    if info["kind"] != "video" or not info["duration"]:
        raise ProproomError("unsupported_media", f"{path.name} has no frames to analyze")
    out = []
    for pos in positions:
        t = round(max(0.0, min(pos, 1.0)) * info["duration"], 3)
        res = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                              "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, timeout=60)
        if res.returncode != 0 or not res.stdout:
            raise ProproomError("unsupported_media", f"ffmpeg cannot read a frame at {t}s: {res.stderr[:200]!r}")
        out.append((t, Image.open(io.BytesIO(res.stdout)).convert("RGB")))
    return out
