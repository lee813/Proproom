"""Web page capture source: explicit URLs, browser screenshots, and MHTML evidence."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlparse

from ....sdk.context import SourceContext
from ....sdk.errors import ProproomError
from ....sdk.types import Candidate, CandidatePage, EnumerateRequest, RepresentationOffer, ResolvedSource, RightsEvidence, SearchRequest, SourceRef, TransferRequest


_CONSENT = re.compile(
    r"^(?:reject(?: all| optional)?|decline(?: all)?|(?:essential|necessary) only|"
    r"(?:use|accept|allow) (?:only )?(?:essential|necessary)(?: cookies)?(?: only)?|"
    r"refuser tout|tout refuser|continuer sans accepter|alles ablehnen|alle ablehnen|"
    r"拒绝全部|全部拒绝|仅使用必要(?:的)?(?:cookie|cookies)?)$", re.I
)


def _options(ctx: SourceContext, revision: str) -> dict[str, Any]:
    try:
        data = json.loads(revision) if revision else {}
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProproomError("bad_argument", "Capture options must be a JSON object") from exc
    if not isinstance(data, dict):
        raise ProproomError("bad_argument", "Capture options must be a JSON object")
    if "options" in data and "expected" in data:
        data = data["options"]
    allowed = {"viewport_width", "viewport_height", "device_scale_factor", "scroll_text", "scroll_y", "clip", "highlight"}
    if set(data) - allowed:
        raise ProproomError("bad_argument", f"Unknown capture options: {sorted(set(data) - allowed)}")
    result = {"viewport_width": ctx.config.get("viewport_width", 1200),
              "viewport_height": ctx.config.get("viewport_height", 900),
              "device_scale_factor": ctx.config.get("device_scale_factor", 2), **data}
    for key, minimum, maximum in (("viewport_width", 200, 4096), ("viewport_height", 200, 4096), ("device_scale_factor", 1, 3)):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
            raise ProproomError("bad_argument", f"Invalid {key}")
    if not isinstance(result["viewport_width"], int) or not isinstance(result["viewport_height"], int):
        raise ProproomError("bad_argument", "Viewport dimensions must be integers")
    if "scroll_text" in result and (not isinstance(result["scroll_text"], str) or not result["scroll_text"].strip()):
        raise ProproomError("bad_argument", "scroll_text must be nonempty text")
    if "scroll_y" in result and (isinstance(result["scroll_y"], bool) or not isinstance(result["scroll_y"], (int, float)) or result["scroll_y"] < 0):
        raise ProproomError("bad_argument", "scroll_y must be nonnegative")
    if "scroll_text" in result and "scroll_y" in result:
        raise ProproomError("bad_argument", "Specify scroll_text or scroll_y, not both")
    for name in ("clip", "highlight"):
        if name in result:
            box = result[name]
            if not isinstance(box, dict) or set(box) != {"x", "y", "width", "height"}:
                raise ProproomError("bad_argument", f"{name} must have x, y, width and height")
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in box.values()):
                raise ProproomError("bad_argument", f"{name} coordinates must be numeric")
            if box["x"] < 0 or box["y"] < 0 or not 0 < box["width"] <= 4096 or not 0 < box["height"] <= 4096:
                raise ProproomError("bad_argument", f"Invalid {name} dimensions")
    return result


def _evidence(url: str | None, final_url: str) -> RightsEvidence:
    if url:
        parsed = urlparse(url)
        if (parsed.scheme in ("http", "https") and parsed.hostname in ("nationalarchives.gov.uk", "www.nationalarchives.gov.uk")
                and parsed.path.lower().rstrip("/") == "/doc/open-government-licence/version/3"):
            return RightsEvidence(basis="license", license_id="ogl-uk-3.0", license_url=url, jurisdiction="GB",
                                  original_text=url, locator='[rel~="license"]', origin="source")
        if parsed.scheme in ("http", "https") and parsed.hostname in ("creativecommons.org", "www.creativecommons.org"):
            match = re.fullmatch(r"/licenses/(by(?:-nc)?(?:-nd|-sa)?)/(\d+\.\d+)/?", parsed.path.lower())
            if match:
                return RightsEvidence(basis="license", license_id=f"CC-{match.group(1)}-{match.group(2)}", license_url=url,
                                      original_text=url, locator='[rel~="license"]', origin="source")
            if parsed.path.lower().rstrip("/") == "/publicdomain/zero/1.0":
                return RightsEvidence(basis="public_domain", pd_reason="cc0", license_id="CC0-1.0", license_url=url,
                                      original_text=url, locator='[rel~="license"]', origin="source")
    return RightsEvidence(basis="quotation", locator=final_url, origin="machine")


@asynccontextmanager
async def _page(ctx: SourceContext, url: str, options: dict[str, Any]):
    """Restrict every Chromium request, including redirects, using CDP Fetch interception."""
    ctx.http.check_url(url)
    await ctx.http.prepare_request(url)
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise ProproomError("browser_unavailable", "Install the capture browser dependencies") from exc
    async with async_playwright() as playwright:
        try:
            if ctx.config.get("cdp_url"):
                browser = await playwright.chromium.connect_over_cdp(ctx.config["cdp_url"])
            else:
                browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:
            raise ProproomError("browser_unavailable", f"Chromium is unavailable: {exc}") from exc
        context = None
        pending: set[asyncio.Task] = set()
        errors: list[Exception] = []
        try:
            context = await browser.new_context(viewport={"width": options["viewport_width"], "height": options["viewport_height"]},
                                                device_scale_factor=options["device_scale_factor"],
                                                user_agent=ctx.http.user_agent, service_workers="block", accept_downloads=False)
            # A browser WebSocket bypasses HTTP request interception; disable it.
            async def block_websocket(socket):
                await socket.close()
            await context.route_web_socket("**/*", block_websocket)
            await context.add_init_script("""() => {
                window.RTCPeerConnection = undefined;
                window.webkitRTCPeerConnection = undefined;
                window.WebTransport = undefined;
                window.open = () => null;
            }""")
            page = await context.new_page()
            async def guard_context(route):
                try:
                    ctx.http.check_url(route.request.url)
                    if route.request.is_navigation_request() and route.request.frame != page.main_frame:
                        raise ProproomError("capture_request_blocked", "Subframe and popup navigations are disabled")
                except ProproomError:
                    await route.abort()
                except Exception as exc:
                    errors.append(exc)
                    await route.abort()
                else:
                    await route.continue_()
            await context.route("**/*", guard_context)
            session = await context.new_cdp_session(page)

            async def route(event):
                request_id = event["requestId"]
                request = event["request"]
                try:
                    ctx.http.check_url(request["url"])
                    if request["method"] not in ("GET", "HEAD"):
                        raise ProproomError("capture_request_blocked", "Capture allows only GET and HEAD")
                except ProproomError:
                    await session.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
                else:
                    await session.send("Fetch.continueRequest", {"requestId": request_id})

            def paused(event):
                task = asyncio.create_task(route(event))
                pending.add(task)
                def done(future):
                    pending.discard(future)
                    if not future.cancelled() and future.exception() is not None:
                        errors.append(future.exception())
                task.add_done_callback(done)
            session.on("Fetch.requestPaused", paused)
            await session.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
            response = None
            backoffs = (5, 10, 20, 40)
            for attempt in range(len(backoffs) + 1):
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if response is None or response.status != 429:
                    break
                value = response.headers.get("retry-after")
                delay = float(backoffs[min(attempt, len(backoffs) - 1)])
                if value:
                    try:
                        delay = max(0.0, float(value))
                    except ValueError:
                        try:
                            until = parsedate_to_datetime(value)
                            delay = max(0.0, (until - datetime.now(timezone.utc)).total_seconds())
                        except (TypeError, ValueError, OverflowError):
                            delay = float(backoffs[min(attempt, len(backoffs) - 1)])
                if attempt == len(backoffs):
                    raise ProproomError("rate_limited", "Capture page returned HTTP 429", retryable=True, retry_after=delay)
                await asyncio.sleep(delay)
            if response is None or response.status >= 400:
                status = response.status if response is not None else 0
                raise ProproomError("capture_http_error", f"Page returned HTTP {status}", retryable=status in (500, 502, 503, 504))
            ctx.http.check_url(page.url)
            dialogs = page.locator('dialog, [role="dialog"], [id*="cookie" i], [class*="cookie" i]').filter(
                has_text=re.compile(r"cookie|consent|privacy|datenschutz|confidentialité", re.I))
            for index in range(min(await dialogs.count(), 10)):
                dialog = dialogs.nth(index)
                if not await dialog.is_visible():
                    continue
                button = dialog.get_by_role("button", name=_CONSENT).first
                if await button.count() and await button.is_visible():
                    await button.click(timeout=2000)
                    break
                text = await dialog.inner_text()
                if not re.search(r"paywall|subscribe|subscription|sign in|log in|abonnez", text, re.I):
                    await dialog.evaluate("el => { el.style.display = 'none'; }")
                    break
            if "scroll_text" in options:
                await page.get_by_text(options["scroll_text"], exact=False).first.scroll_into_view_if_needed(timeout=5000)
            elif "scroll_y" in options:
                await page.evaluate("y => window.scrollTo(0, y)", options["scroll_y"])
            if pending:
                await asyncio.gather(*tuple(pending))
            if errors:
                raise errors[0]
            yield page, session
        finally:
            if context is not None:
                await context.close()
            if pending:
                await asyncio.gather(*tuple(pending))
            await browser.close()


async def _page_data(page) -> dict[str, Any]:
    data = await page.evaluate("""() => ({
      title: document.title,
      publisher: document.querySelector('meta[property="og:site_name"]')?.content || null,
      license: document.querySelector('link[rel~="license"], a[rel~="license"]')?.href || null,
      text: document.body ? document.body.innerText : ''
    })""")
    if not data["title"].strip():
        raise ProproomError("capture_missing_title", "Page has no title")
    return {"title": data["title"], "publisher": data["publisher"], "license": data["license"],
            "final_url": page.url, "text_sha256": hashlib.sha256(data["text"].encode("utf-8")).hexdigest()}


class CaptureSource:
    def __init__(self) -> None:
        self._snapshots: dict[str, tuple[float, bytes]] = {}
        self._snapshot_lock = threading.Lock()

    def _prune_snapshots(self) -> None:
        deadline = time.monotonic() - 120
        for token in [key for key, (created, _) in self._snapshots.items() if created < deadline]:
            del self._snapshots[token]

    async def search(self, ctx: SourceContext, req: SearchRequest) -> CandidatePage:
        if req.cursor:
            raise ProproomError("bad_argument", "Capture has no search pagination")
        ctx.http.check_url(req.query)
        return CandidatePage(items=[Candidate(ref=SourceRef("capture", req.query), title=req.query,
                                              rights=RightsEvidence(basis="unknown"))])

    async def enumerate(self, ctx: SourceContext, req: EnumerateRequest) -> CandidatePage:
        raise ProproomError("not_implemented", "Capture does not support bulk enumeration")

    async def resolve(self, ctx: SourceContext, ref: SourceRef) -> ResolvedSource:
        if ref.source != "capture":
            raise ProproomError("bad_argument", "Invalid capture source")
        options = _options(ctx, ref.revision)
        async with _page(ctx, ref.external_id, options) as (page, _):
            data = await _page_data(page)
        evidence = _evidence(data["license"], data["final_url"])
        revision = json.dumps({"options": options, "expected": data, "token": secrets.token_urlsafe(24)}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        raw = {"requested_url": ref.external_id, "final_url": data["final_url"], "title": data["title"],
               "publisher": data["publisher"], "retrieved": datetime.now(timezone.utc).isoformat(), "capture_options": options}
        return ResolvedSource(ref=SourceRef("capture", ref.external_id, revision), title=data["title"], kind="screenshot",
                              metadata={"final_url": data["final_url"], "raw": raw}, source_url=ref.external_id,
                              publisher=data["publisher"], evidence=[evidence],
                              offers=[RepresentationOffer("original", "image/png", data["final_url"]),
                                      RepresentationOffer("snapshot", "multipart/related", data["final_url"])])

    async def transfer(self, ctx: SourceContext, req: TransferRequest, sink) -> None:
        try:
            state = json.loads(req.ref.revision)
            expected, token = state["expected"], state["token"]
        except (TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ProproomError("bad_argument", "Capture transfer requires a resolved reference") from exc
        options = _options(ctx, req.ref.revision)
        if req.offer.role == "snapshot":
            with self._snapshot_lock:
                self._prune_snapshots()
                stored = self._snapshots.pop(token, None)
            if stored is None:
                raise ProproomError("capture_session_expired", "Matching screenshot snapshot is unavailable", retryable=True)
            await sink.write(stored[1])
            return
        if req.offer.role != "original":
            raise ProproomError("bad_argument", "Unknown capture representation")
        async with _page(ctx, req.ref.external_id, options) as (page, session):
            if await _page_data(page) != expected:
                raise ProproomError("capture_changed", "Page changed after rights were assessed", retryable=True)
            # Both artifacts come from the same page and browser session.  The
            # snapshot precedes our annotation so it remains unaltered evidence.
            result = await session.send("Page.captureSnapshot", {"format": "mhtml"})
            snapshot = result["data"].encode("utf-8")
            if len(snapshot) > 50 * 1024 * 1024:
                raise ProproomError("capture_too_large", "Page snapshot exceeds 50 MiB")
            if "highlight" in options:
                await page.evaluate("""box => {
                  const el = document.createElement('div'); el.id = 'proproom-capture-highlight';
                  Object.assign(el.style, {position:'fixed', left:box.x+'px', top:box.y+'px', width:box.width+'px',
                    height:box.height+'px', border:'4px solid red', boxSizing:'border-box', pointerEvents:'none', zIndex:'2147483647'});
                  document.documentElement.appendChild(el);
                }""", options["highlight"])
            content = await page.screenshot(type="png", clip=options.get("clip"))
            await sink.write(content)
            with self._snapshot_lock:
                self._prune_snapshots()
                if sum(len(item[1]) for item in self._snapshots.values()) + len(snapshot) > 100 * 1024 * 1024:
                    raise ProproomError("capture_busy", "Pending capture snapshots exceed 100 MiB", retryable=True)
                self._snapshots[token] = (time.monotonic(), snapshot)



Plugin = CaptureSource
