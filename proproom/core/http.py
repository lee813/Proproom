"""Outbound HTTP for sources: User-Agent with project URL + instance id + contact,
per-plugin domain allow-lists, rate-limiting and backoff.

Handover §11.4, §12.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from ..sdk.errors import ProproomError

_RATE_LOCKS: dict[str, float] = {}
_LOCK = asyncio.Lock()


def build_user_agent(instance_id: str = "", contact: str = "") -> str:
    base = f"Proproom/0.0.1 (https://github.com/lee813/Proproom; instance:{instance_id or 'unknown'})"
    if contact:
        return f"{base} ({contact})"
    return base


def check_domain(url: str, allowed_domains: list[str] | None) -> None:
    """Validate that the URL's host matches the plugin's declared domains."""
    if not allowed_domains:
        return
    host = (urlparse(url).hostname or "").lower()
    for d in allowed_domains:
        d = d.lower()
        if host == d or host.endswith("." + d):
            return
    raise ProproomError(
        code="domain_disallowed",
        message=f"Access to host '{host}' was rejected; not in declared domain allowlist ({allowed_domains})",
        retryable=False,
    )


class RateLimitedClient:
    def __init__(
        self,
        user_agent: str,
        rate_per_sec: float = 1.0,
        allowed_domains: list[str] | None = None,
        timeout: float = 30.0,
    ):
        self.user_agent = user_agent
        self.rate_per_sec = rate_per_sec
        self.min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0
        self.allowed_domains = allowed_domains
        self.timeout = timeout
        self._last_call: dict[str, float] = {}

    async def _throttle(self, domain: str) -> None:
        if self.min_interval <= 0:
            return
        async with _LOCK:
            now = time.monotonic()
            last = self._last_call.get(domain, 0.0)
            elapsed = now - last
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self._last_call[domain] = time.monotonic()

    async def get_json(self, url: str, params: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None) -> Any:
        check_domain(url, self.allowed_domains)
        domain = (urlparse(url).hostname or "").lower()
        await self._throttle(domain)

        hdrs = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            hdrs.update(headers)

        backoffs = [2.0, 5.0, 10.0]
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            for attempt in range(len(backoffs) + 1):
                resp = await client.get(url, params=params, headers=hdrs)
                final_host = (urlparse(str(resp.url)).hostname or "").lower()
                check_domain(str(resp.url), self.allowed_domains)

                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else backoffs[min(attempt, len(backoffs) - 1)]
                    if attempt < len(backoffs):
                        await asyncio.sleep(delay)
                        continue
                    raise ProproomError(
                        code="rate_limited",
                        message=f"Rate limit exceeded (429) requesting {url}",
                        retryable=True,
                        retry_after=delay,
                    )

                if resp.status_code >= 400:
                    raise ProproomError(
                        code=f"http_{resp.status_code}",
                        message=f"HTTP {resp.status_code} requesting {url}: {resp.text[:300]}",
                        retryable=resp.status_code in (500, 502, 503, 504),
                    )

                return resp.json()

    async def get_text(self, url: str, params: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None) -> str:
        check_domain(url, self.allowed_domains)
        domain = (urlparse(url).hostname or "").lower()
        await self._throttle(domain)

        hdrs = {"User-Agent": self.user_agent}
        if headers:
            hdrs.update(headers)

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            resp = await client.get(url, params=params, headers=hdrs)
            check_domain(str(resp.url), self.allowed_domains)
            resp.raise_for_status()
            return resp.text
