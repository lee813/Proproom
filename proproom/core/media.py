"""Unified MIME types, extensions, and kind taxonomy.

One implementation per concern (AGENTS.md):
- Single source of truth for MIME/kind/extension mapping
- Magic-byte sniffing and extension fallback
- Kind normalization (e.g. legacy kinds 'broll' -> 'footage', 'observation' -> 'photo')
"""
from __future__ import annotations

from pathlib import Path

LEGACY_KINDS: dict[str, str] = {
    "broll": "footage",
    "observation": "photo",
}

_MIME_TO_EXT: dict[str, str] = {
    "video/webm": ".webm",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/aac": ".m4a",
    "audio/mp4": ".m4a",
}

_EXT_TO_MIME_AND_KIND: dict[str, tuple[str, str]] = {
    ".webm": ("video/webm", "footage"),
    ".mp4": ("video/mp4", "footage"),
    ".mov": ("video/mp4", "footage"),
    ".mkv": ("video/mp4", "footage"),
    ".m4v": ("video/mp4", "footage"),
    ".avi": ("video/mp4", "footage"),
    ".png": ("image/png", "photo"),
    ".jpg": ("image/jpeg", "photo"),
    ".jpeg": ("image/jpeg", "photo"),
    ".svg": ("image/svg+xml", "graphic"),
    ".gif": ("image/gif", "photo"),
    ".webp": ("image/webp", "photo"),
    ".mp3": ("audio/mpeg", "music"),
    ".wav": ("audio/wav", "music"),
    ".flac": ("audio/flac", "music"),
    ".aac": ("audio/aac", "music"),
    ".m4a": ("audio/aac", "music"),
    ".ogg": ("audio/ogg", "music"),
}


# What a kind is made of, for filters that only care whether it's moving, still or sound.
_KIND_FAMILY: dict[str, str] = {
    "footage": "video",
    "music": "audio", "sfx": "audio", "ambience": "audio",
    "photo": "image", "portrait": "image", "document": "image", "screenshot": "image", "artwork": "image",
    "graphic": "image",
}


def kind_family(kind: str | None) -> str | None:
    return _KIND_FAMILY.get(normalize_kind(kind)) if kind else None


def is_media_file(p: Path) -> bool:
    """A file the library can hold, judged by extension (a project folder also holds json, html and js)."""
    return p.suffix.lower() in _EXT_TO_MIME_AND_KIND


def normalize_kind(raw_kind: str | None, inferred: str = "photo") -> str:
    """Normalize legacy or custom kind to canonical (footage, photo, graphic, music)."""
    if not raw_kind:
        return inferred
    k = raw_kind.strip().lower()
    return LEGACY_KINDS.get(k, k)


def sniff_mime(p: Path | str | None, rel_file: str | None = None) -> tuple[str, str]:
    """Return (mime, inferred_kind) by inspecting magic bytes and extension."""
    ext = ""
    if rel_file:
        ext = Path(rel_file).suffix.lower()
    elif p:
        ext = Path(p).suffix.lower()

    head = b""
    if p:
        p_obj = Path(p)
        if p_obj.is_file():
            try:
                with open(p_obj, "rb") as f:
                    head = f.read(64)
            except Exception:
                pass

    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm", "footage"
    if len(head) >= 8 and (head[4:8] == b"ftyp" or b"ftyp" in head[:16]):
        return "video/mp4", "footage"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "photo"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "photo"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif", "photo"
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP":
        return "image/webp", "photo"
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WAVE":
        return "audio/wav", "music"
    if head.startswith(b"ID3") or head.startswith(b"\xff\xfb") or head.startswith(b"\xff\xf3"):
        return "audio/mpeg", "music"
    if head.startswith(b"fLaC"):
        return "audio/flac", "music"
    if head.startswith(b"OggS"):
        return "audio/ogg", "music"
    if head.strip().startswith(b"<?xml") and b"<svg" in head[:64]:
        return "image/svg+xml", "graphic"
    if head.strip().startswith(b"<svg"):
        return "image/svg+xml", "graphic"

    if ext in _EXT_TO_MIME_AND_KIND:
        return _EXT_TO_MIME_AND_KIND[ext]

    return "application/octet-stream", "photo"


def guess_extension(mime: str, blob_path: Path | None = None, fallback_name: str | None = None) -> str:
    """Return file extension (.jpg, .mp4, etc.) for a MIME type, blob path, or fallback name."""
    if blob_path and blob_path.is_file():
        sniffed_mime, _ = sniff_mime(blob_path)
        if sniffed_mime in _MIME_TO_EXT:
            return _MIME_TO_EXT[sniffed_mime]

    mime_clean = (mime or "").lower().split(";")[0].strip()
    if mime_clean in _MIME_TO_EXT:
        return _MIME_TO_EXT[mime_clean]

    for m_prefix, ext_val in _MIME_TO_EXT.items():
        if m_prefix in mime_clean:
            return ext_val

    if fallback_name:
        suf = Path(fallback_name).suffix.lower()
        if suf in _EXT_TO_MIME_AND_KIND:
            return ".jpg" if suf == ".jpeg" else suf

    return ".jpg"
