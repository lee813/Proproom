"""Outbound HTTP for sources: User-Agent with project URL + instance id + contact,
per-plugin domain allow-lists, rate-limiting and backoff.

Handover §11.4, §12.
"""
from __future__ import annotations

import asyncio
import time
import threading
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from ..sdk.errors import ProproomError

_RATE_LOCKS: dict[str, float] = {}
_LOCK = threading.Lock()


def build_user_agent(instance_id: str = "", contact: str = "") -> str:
    base = f"Proproom/0.0.1 (https://github.com/lee813/Proproom; instance:{instance_id or 'unknown'})"
    if contact:
        return f"{base} ({contact})"
    return base


def check_domain(url: str, allowed_domains: list[str] | None) -> None:
    """Validate that the URL's host matches the plugin's declared domains."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ProproomError("invalid_url", "Only credential-free HTTP(S) URLs are allowed")
    if not allowed_domains:
        raise ProproomError(
            code="domain_disallowed",
            message="No domains declared; network access is disabled",
            retryable=False,
        )
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
        rate_key: str | None = None,
    ):
        self.user_agent = user_agent
        self.rate_per_sec = rate_per_sec
        self.min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0
        self.allowed_domains = allowed_domains
        self.timeout = timeout
        self.rate_key = rate_key

    def check_url(self, url: str) -> None:
        check_domain(url, self.allowed_domains)

    async def prepare_request(self, url: str) -> None:
        self.check_url(url)
        await self._throttle((urlparse(url).hostname or "").lower())

    async def _throttle(self, domain: str) -> None:
        if self.min_interval <= 0:
            return
        # Reserve a slot under a thread lock: plugin calls may use different event loops.
        with _LOCK:
            now = time.monotonic()
            key = self.rate_key or domain
            scheduled = max(now, _RATE_LOCKS.get(key, 0.0) + self.min_interval)
            _RATE_LOCKS[key] = scheduled
        if scheduled > now:
            await asyncio.sleep(scheduled - now)

    async def _get_with_redirects(self, client: httpx.AsyncClient, url: str, params: dict[str, Any] | None, headers: dict[str, str]) -> httpx.Response:
        current = url
        for _ in range(11):
            self.check_url(current)
            await self._throttle((urlparse(current).hostname or "").lower())
            response = await client.get(current, params=params, headers=headers, follow_redirects=False)
            if not response.is_redirect:
                return response
            location = response.headers.get("location")
            if not location:
                raise ProproomError("bad_redirect", "Redirect response has no location")
            current = urljoin(str(response.url), location)
            self.check_url(current)
            params = None
        raise ProproomError("too_many_redirects", f"Too many redirects requesting {url}")

    async def get_json(self, url: str, params: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None) -> Any:
        check_domain(url, self.allowed_domains)
        hdrs = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            hdrs.update(headers)

        backoffs = [2.0, 5.0, 10.0]
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            for attempt in range(len(backoffs) + 1):
                resp = await self._get_with_redirects(client, url, params, hdrs)

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
        hdrs = {"User-Agent": self.user_agent}
        if headers:
            hdrs.update(headers)

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            resp = await self._get_with_redirects(client, url, params, hdrs)
            resp.raise_for_status()
            return resp.text
