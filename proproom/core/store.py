"""Content-addressed blob store under <home>/blobs (sha256, immutable, reflink/copy/hardlink delivery).

Handover §5.2, §10.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
from pathlib import Path
from typing import Literal

from .paths import Home

LinkMode = Literal["reflink", "copy", "hardlink", "auto"]


def hash_file(path: Path) -> tuple[str, int]:
    """Compute sha256 hex digest and size in bytes."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def hash_bytes(data: bytes) -> tuple[str, int]:
    """Compute sha256 hex digest and size in bytes."""
    return hashlib.sha256(data).hexdigest(), len(data)


def blob_relpath(sha256: str) -> str:
    """Relative path under <home>: blobs/ab/abcdef..."""
    return f"blobs/{sha256[:2]}/{sha256}"


def blob_abspath(home: Home, sha256: str) -> Path:
    return home.root / blob_relpath(sha256)


def put_bytes(home: Home, data: bytes, mime: str = "application/octet-stream") -> tuple[str, int, str]:
    """Store data in the CAS blob store. Returns (sha256, bytes, relpath)."""
    sha, size = hash_bytes(data)
    target = blob_abspath(home, sha)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = home.dir("tmp") / f"put_{sha}_{os.getpid()}"
        tmp.write_bytes(data)
        os.chmod(tmp, 0o444)  # immutable
        os.replace(tmp, target)
    return sha, size, blob_relpath(sha)


def put_file(home: Home, src_path: Path, mime: str = "application/octet-stream", *, move: bool = False) -> tuple[str, int, str]:
    """Store a file in the CAS blob store. Returns (sha256, bytes, relpath)."""
    sha, size = hash_file(src_path)
    target = blob_abspath(home, sha)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = home.dir("tmp") / f"put_{sha}_{os.getpid()}"
        if move:
            shutil.move(src_path, tmp)
        else:
            shutil.copy2(src_path, tmp)
        os.chmod(tmp, 0o444)  # immutable
        os.replace(tmp, target)
    elif move:
        src_path.unlink(missing_ok=True)
    return sha, size, blob_relpath(sha)


def verify_blob(home: Home, sha256: str) -> str:
    """Check if the blob exists and its sha256 matches. Returns 'ok' | 'mismatch' | 'missing'."""
    p = blob_abspath(home, sha256)
    if not p.is_file():
        return "missing"
    actual_sha, _ = hash_file(p)
    return "ok" if actual_sha == sha256 else "mismatch"


def _reflink(src: Path, dst: Path) -> bool:
    """Attempt clone / reflink on supported filesystems (e.g. btrfs, XFS)."""
    # 1. On Linux, FICLONE ioctl = 0x40049409
    if hasattr(os, "O_RDONLY"):
        try:
            FICLONE = 0x40049409
            import fcntl
            with open(src, "rb") as s, open(dst, "wb") as d:
                fcntl.ioctl(d.fileno(), FICLONE, s.fileno())
            return True
        except Exception:
            pass
    # 2. Try copy_file_range if available
    if hasattr(os, "copy_file_range"):
        try:
            size = src.stat().st_size
            with open(src, "rb") as s, open(dst, "wb") as d:
                offset_in = 0
                offset_out = 0
                while offset_in < size:
                    copied = os.copy_file_range(s.fileno(), d.fileno(), size - offset_in, offset_in, offset_out)
                    if copied == 0:
                        break
                    offset_in += copied
                    offset_out += copied
            return True
        except Exception:
            pass
    return False


def link_or_copy(src: Path, dst: Path, mode: LinkMode = "auto") -> str:
    """Deliver a file from CAS store to a destination path using reflink, copy, or hardlink.

    Returns the actual link mode used: 'reflink', 'copy', or 'hardlink'.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst = dst.parent / f".tmp_{dst.name}_{os.getpid()}"

    used_mode = mode
    if mode == "hardlink":
        try:
            os.link(src, tmp_dst)
            used_mode = "hardlink"
        except OSError:
            shutil.copy2(src, tmp_dst)
            used_mode = "copy"
    elif mode == "copy":
        shutil.copy2(src, tmp_dst)
        used_mode = "copy"
    else:  # auto or reflink
        if _reflink(src, tmp_dst):
            used_mode = "reflink"
        else:
            shutil.copy2(src, tmp_dst)
            used_mode = "copy"

    os.replace(tmp_dst, dst)
    return used_mode
