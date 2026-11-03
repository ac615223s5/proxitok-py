"""Playwright-backed access to TikTok web APIs that require request signing.

One headless Chromium page is parked on tiktok.com. API calls run ``window.fetch`` inside
that page, so TikTok's own SDK signs them (appends msToken / X-Bogus / X-Gnarly / X-Dynosaur), and
the query parameters mirror the ones the web app itself sends (captured from the page's
first API request: device_id, odinId, browser_version, ...).

Browser launch is a stock Playwright launch: no user-agent override, no automation-flag
changes. TikTok rejects a share of requests from it (HTTP 403, empty body) even for the
page's *own* requests, so callers get a bounded retry and then a ScraperError.

Cookies are kept in sync with the httpx jar in both directions so that the CDN video URLs
returned here stream through the httpx-based /stream proxy (they are bound to ``tt_chain_token``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from .errors import ScraperError
from .http import check_api_json

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Browser, BrowserContext, Page, Playwright

log = logging.getLogger(__name__)

PARK_URL = "https://www.tiktok.com/explore"

# Params the web app sends with every call; copied from its own first request.
_COMMON_KEYS = (
    "WebIdLastTime", "aid", "app_language", "app_name", "browser_language", "browser_name",
    "browser_online", "browser_platform", "browser_version", "channel", "cookie_enabled",
    "data_collection_enabled", "device_id", "device_platform", "focus_state", "history_len",
    "is_fullscreen", "is_page_visible", "language", "odinId", "os", "region", "screen_height",
    "screen_width", "tz_name", "user_is_login", "webcast_language",
)
_FALLBACK_BASE = {
    "aid": "1988", "app_language": "en", "app_name": "tiktok_web", "browser_language": "en-US",
    "browser_name": "Mozilla", "browser_online": "true", "browser_platform": "Linux x86_64",
    "channel": "tiktok_web", "cookie_enabled": "true", "device_platform": "web_pc",
    "focus_state": "true", "history_len": "2", "is_fullscreen": "false", "is_page_visible": "true",
    "language": "en", "os": "linux", "region": "US", "user_is_login": "false", "webcast_language": "en",
}

# In-page fetch with its own abort timer (slightly shorter than the Python-side wait), so a hung TikTok
# response doesn't leave a pending request behind. Network errors / aborts are returned, not thrown.
_JS_FETCH = """async ({url, ms}) => {
  try {
    const r = await fetch(url, {credentials: 'include', signal: AbortSignal.timeout(ms),
                                headers: {'Accept': 'application/json, text/plain, */*'}});
    const t = await r.text();
    return {status: r.status, body: t};
  } catch (e) {
    return {status: 0, body: '', error: String(e && e.name || e), aborted: !!(e && (e.name === 'TimeoutError' || e.name === 'AbortError'))};
  }
}"""

ATTEMPTS = 3
# Relaunch Chromium only after this many evaluate timeouts in a row (a single hung TikTok response
# just fails that call; the page itself is still usable).
MAX_CONSECUTIVE_TIMEOUTS = 3


def _consume_exception(task: "asyncio.Task") -> None:
    if not task.cancelled():
        task.exception()  # mark as retrieved; callers awaiting the task still get it


class BrowserSession:
    """Single shared Chromium page. API calls are serialized by an asyncio.Lock (bounded wait).

    The launch runs in its own task and callers await it through ``asyncio.shield``: a caller that is
    cancelled (e.g. the comments timeout) does not abort a half-finished launch, and the session only
    counts as started once the launch fully finished (page parked, params captured).
    """

    def __init__(
        self,
        headless: bool = True,
        timeout: float = 30.0,
        proxy: Optional[str] = None,
        get_cookies: Optional[Callable[[], list[tuple[str, str, str, str]]]] = None,
        put_cookies: Optional[Callable[[list[dict]], None]] = None,
        lock_timeout: Optional[float] = None,
    ) -> None:
        self.headless = headless
        self.timeout = timeout
        self.lock_timeout = timeout if lock_timeout is None else lock_timeout
        self.proxy = proxy
        self._get_cookies = get_cookies
        self._put_cookies = put_cookies
        self._lock = asyncio.Lock()
        self._pw: Optional["Playwright"] = None
        self._browser: Optional["Browser"] = None
        self._ctx: Optional["BrowserContext"] = None
        self._page: Optional["Page"] = None
        self._base: dict[str, str] = {}
        self._ready = False
        self._starting: Optional[asyncio.Task] = None
        self._timeouts = 0
        self._closed = False
        self.last_start: float = 0.0
        self.launches = 0

    # ------------------------------------------------------------------ lifecycle

    @property
    def started(self) -> bool:
        return self._ready and self._page is not None and not self._page.is_closed()

    async def _launch(self) -> tuple[Any, Any, Any, dict[str, str]]:
        """Launch Chromium, park a page on tiktok.com and capture the web app's API params.

        Works only on locals; on any failure *or cancellation* everything it opened is closed again.
        Returns (browser, context, page, base_params).
        """
        assert self._pw is not None
        browser = ctx = None
        try:
            launch: dict[str, Any] = {"headless": self.headless}
            if self.proxy:
                launch["proxy"] = {"server": self.proxy}
            browser = await self._pw.chromium.launch(**launch)
            self.launches += 1
            ctx = await browser.new_context(locale="en-US")
            ctx.set_default_timeout(self.timeout * 1000)
            await self._push_cookies(ctx)
            page = await ctx.new_page()
            captured: dict[str, str] = {}

            def on_request(req) -> None:
                if captured or "/api/" not in req.url:
                    return
                u = urlparse(req.url)
                if not (u.hostname or "").endswith("tiktok.com"):
                    return
                q = parse_qs(u.query)
                if "device_id" in q:
                    captured.update({k: v[0] for k, v in q.items() if k in _COMMON_KEYS})

            page.on("request", on_request)
            try:
                await page.goto(PARK_URL, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                # Wait (bounded) until the web app has issued its first API call and set msToken.
                deadline = time.monotonic() + min(self.timeout, 15.0)
                while time.monotonic() < deadline:
                    names = {c["name"] for c in await ctx.cookies()}
                    if captured and "msToken" in names:
                        break
                    await asyncio.sleep(0.5)
            finally:
                page.remove_listener("request", on_request)
            base = dict(_FALLBACK_BASE)
            base.update(captured)
            if not captured:
                log.warning("Did not observe TikTok's own API params; using fallback params")
            await self._pull_cookies(ctx)
            return browser, ctx, page, base
        except BaseException:
            for obj in (ctx, browser):
                if obj is not None:
                    try:
                        await obj.close()
                    except BaseException:  # pragma: no cover - best effort while unwinding
                        pass
            raise

    async def _start(self) -> None:
        if self._closed:
            raise ScraperError(503, None, "Browser session is shut down")
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:  # pragma: no cover
            raise ScraperError(503, None, "Playwright is not installed") from e
        await self._teardown()
        try:
            if self._pw is None:
                self._pw = await async_playwright().start()
            browser, ctx, page, base = await self._launch()
        except ScraperError:
            raise
        except Exception as e:
            raise ScraperError(503, None, f"Could not start the browser: {e.__class__.__name__}: {e}"[:300]) from e
        if self._closed:  # close() ran while we were launching
            for obj in (ctx, browser):
                try:
                    await obj.close()
                except Exception:
                    pass
            raise ScraperError(503, None, "Browser session is shut down")
        self._browser, self._ctx, self._page, self._base = browser, ctx, page, base
        self._timeouts = 0
        self._ready = True
        self.last_start = time.time()
        log.info("TikTok browser session started")

    async def _teardown(self) -> None:
        for obj in (self._ctx, self._browser):
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
        self._page = self._ctx = self._browser = None
        self._ready = False
        self._base = {}

    async def close(self) -> None:
        self._closed = True
        task, self._starting = self._starting, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        async with self._lock:
            await self._teardown()
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
                self._pw = None

    async def _ensure(self) -> None:
        """Start the browser if needed. The launch is shielded from the caller's cancellation and shared
        by concurrent callers; a failed launch is retried by the next call."""
        if self.started:
            return
        if self._starting is None or self._starting.done():
            self._starting = asyncio.create_task(self._start())
            self._starting.add_done_callback(_consume_exception)
        await asyncio.shield(self._starting)

    # ------------------------------------------------------------------ cookies

    async def _push_cookies(self, ctx=None) -> None:
        """httpx jar -> browser, so both share one tt_chain_token / ttwid identity."""
        ctx = ctx or self._ctx
        if not (self._get_cookies and ctx):
            return
        cookies = []
        for name, value, domain, path in self._get_cookies():
            if not domain or "tiktok.com" not in domain:
                continue
            cookies.append({"name": name, "value": value, "domain": domain if domain.startswith(".") else "." + domain,
                            "path": path or "/", "secure": True, "sameSite": "None"})
        if cookies:
            try:
                await ctx.add_cookies(cookies)
            except Exception as e:  # pragma: no cover
                log.warning("Could not push cookies into browser: %s", e)

    async def _pull_cookies(self, ctx=None) -> None:
        """browser -> httpx jar (msToken, ttwid, tt_chain_token, ...)."""
        ctx = ctx or self._ctx
        if not (self._put_cookies and ctx):
            return
        try:
            cookies = await ctx.cookies("https://www.tiktok.com")
        except Exception:
            return
        self._put_cookies([{"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c["path"]} for c in cookies])

    # ------------------------------------------------------------------ calls

    def _url(self, path: str, params: dict[str, str]) -> str:
        q = dict(self._base)
        q.update({k: str(v) for k, v in params.items()})
        return f"{path}?{urlencode(q)}"

    async def _with_restart(self, fn, timeout: Optional[float] = None):
        """Run ``fn()`` (needs a live page). A crashed/closed page is relaunched once; a timeout just
        fails the call (504) - Chromium is only relaunched after MAX_CONSECUTIVE_TIMEOUTS in a row."""
        for attempt in (1, 2):
            await self._ensure()
            try:
                return await asyncio.wait_for(fn(), timeout=timeout or self.timeout)
            except asyncio.TimeoutError:
                self._timeouts += 1
                log.warning("Browser call timed out (%d in a row)", self._timeouts)
                if self._timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                    await self._teardown()  # next call relaunches
                raise ScraperError(504, None, "Timed out waiting for TikTok (browser)")
            except ScraperError:
                raise
            except Exception as e:
                log.warning("Browser error (attempt %d): %s; relaunching", attempt, e)
                await self._teardown()
                if attempt == 2:
                    raise ScraperError(503, None, f"Browser failure: {e.__class__.__name__}", {"error": str(e)[:300]}) from e

    async def load_page(self, url: str) -> str:
        """Load ``url`` in a fresh tab of the shared context and return its HTML once TikTok's page
        data is present. Used when plain HTTP gets the WAF challenge: the browser runs it, and the
        resulting WAF cookies are pulled into the httpx jar so later plain requests pass too."""
        try:
            async with asyncio.timeout(self.lock_timeout):
                await self._lock.acquire()
        except TimeoutError:
            raise ScraperError(503, None, "The TikTok browser is busy with other requests; try again in a moment",
                               {"url": url}) from None
        try:
            async def run() -> str:
                await self._push_cookies(self._ctx)
                page = await self._ctx.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                    try:
                        await page.wait_for_selector(
                            "#__UNIVERSAL_DATA_FOR_REHYDRATION__, #SIGI_STATE", state="attached",
                            timeout=max(1.0, self.timeout - 2.0) * 1000,
                        )
                    except Exception:
                        pass  # caller's parse_hydration reports the failure with an excerpt
                    return await page.content()
                finally:
                    await page.close()

            html = await self._with_restart(run)
            await self._pull_cookies()
            return html
        finally:
            self._lock.release()

    async def fetch_json(self, path: str, params: dict[str, str], attempts: int = ATTEMPTS) -> dict:
        """Signed GET of a TikTok web API from inside the page. Returns the validated JSON dict.

        Raises ScraperError: TikTok statusCode != 0 (mapped), -1 for empty bodies / HTTP 403 after
        ``attempts`` tries, 504 on timeout, 503 if the browser can't run or stayed busy with other
        requests for longer than ``lock_timeout``.
        """
        try:
            async with asyncio.timeout(self.lock_timeout):
                await self._lock.acquire()
        except TimeoutError:
            raise ScraperError(503, None, "The TikTok browser is busy with other requests; try again in a moment",
                               {"path": path}) from None
        try:
            last: Optional[ScraperError] = None
            for i in range(max(1, attempts)):
                if i:
                    await asyncio.sleep(1.0 * i)
                await self._ensure()  # fills self._base before the URL is built
                url = self._url(path, params)
                arg = {"url": url, "ms": int(max(1.0, self.timeout - 1.0) * 1000)}
                res = await self._with_restart(lambda: self._page.evaluate(_JS_FETCH, arg))
                if res.get("aborted"):
                    self._timeouts += 1
                    if self._timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                        await self._teardown()
                    raise ScraperError(504, None, "Timed out waiting for TikTok (browser)", {"path": path})
                self._timeouts = 0
                status, body = res.get("status"), res.get("body") or ""
                if not body:
                    last = ScraperError(
                        502, -1,
                        f"TikTok returned an empty response (HTTP {status}); it is probably rate limiting or "
                        "blocking automated access right now",
                        {"path": path, "status": status, "attempts": i + 1},
                    )
                    continue
                try:
                    data = json.loads(body)
                except ValueError:
                    last = ScraperError(502, -1, "TikTok returned a non-JSON response", {"path": path, "status": status, "body": body[:300]})
                    continue
                await self._pull_cookies()
                return check_api_json(data, path)
            await self._pull_cookies()
            assert last is not None
            raise last
        finally:
            self._lock.release()


__all__ = ["BrowserSession"]
