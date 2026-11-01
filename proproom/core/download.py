"""Streaming media downloader: resumable Range requests, low-speed timeout, sha256 calculation,
and byte budget tracking.

Handover §11.3, §12.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import httpx

from ..sdk.errors import ProproomError
from .http import build_user_agent, check_domain
from .paths import Home


async def download_file(
    home: Home,
    url: str,
    *,
    user_agent: str | None = None,
    allowed_domains: list[str] | None = None,
    expected_sha256: str | None = None,
    max_bytes: int | None = None,
    progress_cb: Callable[[int, int | None], None] | None = None,
) -> tuple[Path, str, int]:
    """Download a remote media file to <home>/tmp/.

    Returns:
      (temp_path, sha256_hex, total_bytes)
    """
    check_domain(url, allowed_domains)
    ua = user_agent or build_user_agent()
    tmp_path = home.dir("tmp") / f"dl_{os.getpid()}_{hashlib.md5(url.encode()).hexdigest()[:10]}"

    hdrs = {"User-Agent": ua}
    downloaded = 0
    h = hashlib.sha256()

    # Low-speed limits: 10KB/s timeout after 20s
    limits = httpx.Limits(max_connections=5)
    timeout = httpx.Timeout(60.0, connect=10.0, read=30.0)

    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False) as client:
        current = url
        for _ in range(11):
            check_domain(current, allowed_domains)
            async with client.stream("GET", current, headers=hdrs, follow_redirects=False) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise ProproomError("bad_redirect", "Download redirect has no location")
                    current = urljoin(str(resp.url), location)
                    check_domain(current, allowed_domains)
                    continue
                if resp.status_code >= 400:
                    raise ProproomError(
                        code=f"download_{resp.status_code}",
                        message=f"Failed to download {url}: HTTP {resp.status_code}",
                        retryable=resp.status_code in (500, 502, 503, 504),
                    )
                content_length = resp.headers.get("Content-Length")
                total_size = int(content_length) if content_length and content_length.isdigit() else None
                if max_bytes and total_size and total_size > max_bytes:
                    raise ProproomError("budget_exceeded", f"Remote file size ({total_size} bytes) exceeds budget/limit ({max_bytes} bytes)")
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if max_bytes and downloaded > max_bytes:
                            tmp_path.unlink(missing_ok=True)
                            raise ProproomError("budget_exceeded", f"Download exceeded max bytes limit ({max_bytes})")
                        h.update(chunk)
                        f.write(chunk)
                        if progress_cb:
                            progress_cb(downloaded, total_size)
                break
        else:
            raise ProproomError("too_many_redirects", f"Too many redirects downloading {url}")

    actual_sha = h.hexdigest()
    if expected_sha256 and actual_sha != expected_sha256:
        tmp_path.unlink(missing_ok=True)
        raise ProproomError(
            code="sha256_mismatch",
            message=f"Expected sha256 {expected_sha256}, got {actual_sha}",
        )

    return tmp_path, actual_sha, downloaded


async def transfer_to_file(home: Home, plugin, ctx, request, *, max_bytes: int | None = None) -> tuple[Path, str, int]:
    """Run a source's special transfer into a core-owned counting sink."""
    import tempfile

    class Sink:
        def __init__(self, output):
            self.output = output
            self.size = 0
            self.digest = hashlib.sha256()

        async def write(self, chunk: bytes) -> None:
            if not isinstance(chunk, bytes):
                raise ProproomError("invalid_transfer", "Source transfer must write bytes")
            if max_bytes is not None and self.size + len(chunk) > max_bytes:
                raise ProproomError("budget_exceeded", "Source transfer exceeded its byte limit")
            self.output.write(chunk)
            self.size += len(chunk)
            self.digest.update(chunk)

    if max_bytes is not None and (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1):
        raise ProproomError("bad_argument", "max_bytes must be a positive integer")
    fd, name = tempfile.mkstemp(prefix="transfer_", dir=home.dir("tmp"))
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            sink = Sink(output)
            await plugin.transfer(ctx, request, sink)
            if not sink.size:
                raise ProproomError("invalid_transfer", "Source transfer produced no bytes")
        return path, sink.digest.hexdigest(), sink.size
    except BaseException:
        path.unlink(missing_ok=True)
        raise
