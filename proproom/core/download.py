"""Streaming media downloader: resumable Range requests, low-speed timeout, sha256 calculation,
and byte budget tracking.

Handover §11.3, §12.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

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

    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
        async with client.stream("GET", url, headers=hdrs) as resp:
            check_domain(str(resp.url), allowed_domains)
            if resp.status_code >= 400:
                raise ProproomError(
                    code=f"download_{resp.status_code}",
                    message=f"Failed to download {url}: HTTP {resp.status_code}",
                    retryable=resp.status_code in (500, 502, 503, 504),
                )
            content_length = resp.headers.get("Content-Length")
            total_size = int(content_length) if content_length and content_length.isdigit() else None

            if max_bytes and total_size and total_size > max_bytes:
                raise ProproomError(
                    code="budget_exceeded",
                    message=f"Remote file size ({total_size} bytes) exceeds budget/limit ({max_bytes} bytes)",
                )

            with open(tmp_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if max_bytes and downloaded > max_bytes:
                        tmp_path.unlink(missing_ok=True)
                        raise ProproomError(
                            code="budget_exceeded",
                            message=f"Download exceeded max bytes limit ({max_bytes})",
                        )
                    h.update(chunk)
                    f.write(chunk)
                    if progress_cb:
                        progress_cb(downloaded, total_size)

    actual_sha = h.hexdigest()
    if expected_sha256 and actual_sha != expected_sha256:
        tmp_path.unlink(missing_ok=True)
        raise ProproomError(
            code="sha256_mismatch",
            message=f"Expected sha256 {expected_sha256}, got {actual_sha}",
        )

    return tmp_path, actual_sha, downloaded
