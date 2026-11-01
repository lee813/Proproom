"""Frame access for analyzer plugins: still images directly, videos through ffprobe/ffmpeg (handover §13)."""
from __future__ import annotations

import io
import json
import subprocess
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .errors import ProproomError

# Decoding a large frame can require several full-sized buffers at once, in addition to the models, so bigger frames
# are decoded scaled down to this many pixels. Analyzers report boxes as fractions of the frame, so they stay valid.
MAX_ANALYSIS_PIXELS = 10_000_000
# Frames over Pillow's decompression-bomb limit (about 89 MP) are refused, not decoded.
MAX_FRAME_PIXELS = Image.MAX_IMAGE_PIXELS


def probe(path: Path) -> dict:
    """{"kind": "image"|"video"|"audio", "duration": seconds or None, "width": int|None, "height": int|None,
    "audio": bool}."""
    try:
        # Only the header is read here; frames() refuses what is too big to decode, so Pillow's warning is noise.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            im = Image.open(path)
        with im:
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


def _fit(width: int, height: int) -> tuple[int, int] | None:
    """The size to decode a frame at so it has at most MAX_ANALYSIS_PIXELS, or None when it already fits."""
    if width * height <= MAX_ANALYSIS_PIXELS:
        return None
    scale = (MAX_ANALYSIS_PIXELS / (width * height)) ** 0.5
    return max(1, int(width * scale)), max(1, int(height * scale))


def frames(path: Path, positions: list[float]) -> list[tuple[float | None, Image.Image]]:
    """RGB frames at the given fractions of a video's duration, or the image itself once (time None). A frame over
    MAX_ANALYSIS_PIXELS comes back scaled down to fit; one over MAX_FRAME_PIXELS is refused."""
    info = probe(path)
    width, height = info["width"], info["height"]
    if info["kind"] in ("image", "video") and (not width or not height or width < 0 or height < 0):
        raise ProproomError("unsupported_media", f"Cannot determine frame dimensions for {path.name}")
    if info["kind"] in ("image", "video") and width * height > MAX_FRAME_PIXELS:
        raise ProproomError("analysis_frame_too_large",
                            f"Frame {width}x{height} exceeds the analysis limit of {MAX_FRAME_PIXELS} pixels")
    size = _fit(width, height) if info["kind"] in ("image", "video") else None
    if info["kind"] == "image":
        with Image.open(path) as im:
            if size:
                im.draft("RGB", size)  # JPEG decodes straight at 1/2, 1/4 or 1/8 size, never below `size`
                im.thumbnail(size)
            return [(None, im.convert("RGB"))]
    if info["kind"] != "video" or not info["duration"]:
        raise ProproomError("unsupported_media", f"{path.name} has no frames to analyze")
    scale = ["-vf", f"scale={size[0]}:{size[1]}"] if size else []
    out = []
    for pos in positions:
        t = round(max(0.0, min(pos, 1.0)) * info["duration"], 3)
        res = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1", *scale,
                              "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, timeout=60)
        if res.returncode != 0 or not res.stdout:
            raise ProproomError("unsupported_media", f"ffmpeg cannot read a frame at {t}s: {res.stderr[:200]!r}")
        out.append((t, Image.open(io.BytesIO(res.stdout)).convert("RGB")))
    return out
