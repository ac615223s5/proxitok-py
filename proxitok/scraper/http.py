"""Plain-HTTP side of the scraper: one shared httpx client + page hydration parsing.

The same :class:`httpx.AsyncClient` (and therefore the same cookie jar) is used for page
hydration, unsigned API calls and the /stream proxy, because TikTok's CDN only serves
``playAddr``/``downloadAddr`` to the cookie jar (``tt_chain_token``) that produced the URL.
The client defaults to ``follow_redirects=False`` (the stream proxy checks every hop against its
allowlist); hydration requests opt in per request.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Optional
from urllib.parse import urlencode

import httpx

from .errors import ScraperError

log = logging.getLogger(__name__)

BASE_URL = "https://www.tiktok.com"
REFERER = "https://www.tiktok.com/"

# Query params TikTok's web app sends on every API call (unsigned subset that the
# creator/item_list endpoint accepts).
API_BASE_PARAMS: dict[str, str] = {
    "aid": "1988",
    "app_language": "en",
    "app_name": "tiktok_web",
    "device_platform": "web_pc",
    "region": "US",
    "language": "en",
}

_UNIVERSAL_RE = re.compile(
    r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S
)
_SIGI_RE = re.compile(r'<script[^>]+id="SIGI_STATE"[^>]*>(.*?)</script>', re.S)

SHORT_HOSTS = ("vm.tiktok.com", "vt.tiktok.com")
_VIDEO_PATH_RE = re.compile(r"^/@([^/?#]*)/(?:video|photo)/(\d+)")  # TikTok may emit "/@/photo/<id>"
_USER_PATH_RE = re.compile(r"^/@([^/?#]+)/?$")


def _host_ok(host: Optional[str]) -> bool:
    host = (host or "").lower()
    return host == "tiktok.com" or host.endswith(".tiktok.com")


# ----------------------------------------------------------------------------- hydration parsing


def _sigi_to_scope(sigi: dict) -> dict:
    """Best-effort conversion of the legacy ``SIGI_STATE`` blob into the
    ``__DEFAULT_SCOPE__`` shape (only the keys the scraper reads)."""
    scope: dict[str, Any] = {}
    users = (sigi.get("UserModule") or {}).get("users") or {}
    ustats = (sigi.get("UserModule") or {}).get("stats") or {}
    seo = (sigi.get("SEOState") or {}).get("metaParams") or {}
    share_meta = {"title": seo.get("title", ""), "desc": seo.get("description", "")}
    items = sigi.get("ItemModule") or {}
    item_list = (sigi.get("ItemList") or {})

    if users and not items or (users and "user-post" in item_list):
        name, user = next(iter(users.items()))
        scope["webapp.user-detail"] = {
            "statusCode": 0,
            "userInfo": {"user": user, "stats": ustats.get(name, {})},
            "shareMeta": share_meta,
        }
    if items:
        vid, item = next(iter(items.items()))
        item = dict(item)
        author = item.get("author")
        if isinstance(author, str):  # SIGI stores the author's uniqueId; expand from UserModule
            item["author"] = users.get(author, {"uniqueId": author})
        scope["webapp.video-detail"] = {
            "statusCode": 0,
            "itemInfo": {"itemStruct": item},
            "shareMeta": share_meta,
        }
    return scope


def parse_hydration(html: str) -> dict:
    """Return TikTok's ``__DEFAULT_SCOPE__`` dict from a page's HTML.

    Falls back to the legacy ``SIGI_STATE`` script (converted to the same shape).
    Raises ScraperError(502, -1) if neither is present / parseable.
    """
    m = _UNIVERSAL_RE.search(html)
    if m:
        try:
            data = json.loads(m.group(1))
            scope = data.get("__DEFAULT_SCOPE__")
            if isinstance(scope, dict):
                return scope
        except (ValueError, AttributeError):
            pass
    m = _SIGI_RE.search(html)
    if m:
        try:
            sigi = json.loads(m.group(1))
            if isinstance(sigi, dict):
                return _sigi_to_scope(sigi)
        except ValueError:
            pass
    raise ScraperError(
        502, -1,
        "Could not find TikTok page data (the page layout changed or TikTok served a verification page)",
        {"html_excerpt": html[:300]},
    )


def scope_section(scope: dict, key: str, url: str = "") -> dict:
    """Pick ``scope[key]`` and raise ScraperError for TikTok error status codes."""
    sec = scope.get(key)
    if not isinstance(sec, dict):
        raise ScraperError(502, -1, f"TikTok page data has no '{key}' section", {"url": url, "keys": list(scope)})
    code = sec.get("statusCode", 0)
    try:
        code = int(code or 0)
    except (TypeError, ValueError):
        code = 0
    if code != 0:
        raise _code_error(code, _s(sec.get("statusMsg")), {"url": url})
    return sec


def _code_error(code: int, tiktok_msg: str, debug: dict) -> ScraperError:
    """Known codes get our (clearer) message; TikTok's own statusMsg is kept in debug."""
    debug = dict(debug, statusCode=code, statusMsg=tiktok_msg)
    if code in ScraperError.KNOWN_CODES:
        return ScraperError.from_tiktok_code(code, debug=debug)
    return ScraperError.from_tiktok_code(code, tiktok_msg or f"TikTok error {code}", debug)


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ("" if v is None else str(v))


def check_api_json(data: Any, url: str = "") -> dict:
    """Validate a TikTok web API JSON body: dict with statusCode/status_code == 0."""
    if not isinstance(data, dict) or not data:
        raise ScraperError(502, -1, "Empty response from TikTok", {"url": url})
    code = data.get("statusCode", data.get("status_code", 0))
    try:
        code = int(code or 0)
    except (TypeError, ValueError):
        code = 0
    if code != 0:
        raise _code_error(code, _s(data.get("statusMsg") or data.get("status_msg")), {"url": url})
    return data


# ----------------------------------------------------------------------------- client


class TikTokHttp:
    def __init__(self, user_agent: str, proxy: Optional[str] = None, timeout: float = 20.0) -> None:
        self.user_agent = user_agent
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "en-US,en;q=0.9",
            },
            proxy=proxy or None,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    # -------------------------------------------------------------- cookies

    def cookie_items(self) -> list[tuple[str, str, str, str]]:
        """(name, value, domain, path) for every cookie in the jar."""
        return [(c.name, c.value or "", c.domain, c.path) for c in self.client.cookies.jar]

    def cookie_header(self) -> str:
        seen: dict[str, str] = {}
        for c in self.client.cookies.jar:
            if _host_ok(c.domain.lstrip(".")):
                seen[c.name] = c.value or ""
        return "; ".join(f"{k}={v}" for k, v in seen.items())

    def set_cookies(self, cookies: Iterable[dict]) -> None:
        """Import cookies in Playwright's format ({name, value, domain, path})."""
        jar = self.client.cookies
        for c in cookies:
            domain = c.get("domain") or ".tiktok.com"
            if not _host_ok(domain.lstrip(".")):
                continue
            # Drop any same-name cookie on another domain variant so only one value is sent.
            for old in list(jar.jar):
                if old.name == c["name"] and old.domain.lstrip(".") == domain.lstrip(".") and old.domain != domain:
                    jar.jar.clear(old.domain, old.path, old.name)
            jar.set(c["name"], c.get("value", ""), domain=domain, path=c.get("path") or "/")

    # -------------------------------------------------------------- requests

    async def get_page(self, url: str) -> str:
        try:
            r = await self.client.get(
                url,
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                follow_redirects=True,
            )
        except httpx.HTTPError as e:
            raise ScraperError(502, None, f"Could not reach TikTok: {e.__class__.__name__}", {"url": url, "error": str(e)}) from e
        if r.status_code >= 400 and r.status_code != 404:
            raise ScraperError(502, None, f"TikTok returned HTTP {r.status_code}", {"url": url, "status": r.status_code})
        return r.text

    async def hydration(self, url: str) -> dict:
        """Fetch a TikTok page and return its ``__DEFAULT_SCOPE__``."""
        html = await self.get_page(url)
        scope = parse_hydration(html)
        return scope

    async def get_api(self, path: str, params: dict[str, str]) -> dict:
        """Unsigned GET to a TikTok web API (works for /api/creator/item_list/)."""
        q = dict(API_BASE_PARAMS)
        q.update(params)
        url = f"{BASE_URL}{path}?{urlencode(q)}"
        try:
            r = await self.client.get(url, headers={"Referer": REFERER, "Accept": "application/json, text/plain, */*"})
        except httpx.HTTPError as e:
            raise ScraperError(502, None, f"Could not reach TikTok: {e.__class__.__name__}", {"url": url, "error": str(e)}) from e
        if not r.content:
            raise ScraperError(502, -1, f"Empty response from TikTok (HTTP {r.status_code})", {"url": url, "status": r.status_code})
        try:
            data = r.json()
        except ValueError:
            raise ScraperError(502, -1, "TikTok returned a non-JSON response", {"url": url, "status": r.status_code, "body": r.text[:300]})
        return check_api_json(data, url)

    async def resolve_short(self, url: str, max_hops: int = 8) -> str:
        """Follow a short link's redirects (only tiktok.com hosts) to the canonical URL."""
        u = httpx.URL(url if "://" in url else "https://" + url)
        host = (u.host or "").lower()
        is_short = host in SHORT_HOSTS or (_host_ok(host) and u.path.startswith("/t/"))
        if not is_short:
            raise ScraperError(400, None, "Not a TikTok short link", {"url": url})
        current = u
        for _ in range(max_hops):
            m = _VIDEO_PATH_RE.match(current.path)
            if m and (current.host or "").lower() not in SHORT_HOSTS:
                return f"{BASE_URL}/@{m.group(1) or 'placeholder'}/video/{m.group(2)}"
            try:
                r = await self.client.get(current, headers={"Accept": "text/html,*/*"}, follow_redirects=False)
            except httpx.HTTPError as e:
                raise ScraperError(502, None, f"Could not reach TikTok: {e.__class__.__name__}", {"url": str(current)}) from e
            loc = r.headers.get("location")
            if r.status_code not in (301, 302, 303, 307, 308) or not loc:
                break
            nxt = current.join(loc)
            if nxt.scheme not in ("http", "https") or not _host_ok(nxt.host):
                raise ScraperError(400, None, "Short link redirects outside tiktok.com", {"url": str(nxt)})
            current = nxt
        m = _USER_PATH_RE.match(current.path)
        if m and (current.host or "").lower() not in SHORT_HOSTS:
            return f"{BASE_URL}/@{m.group(1)}"
        raise ScraperError(404, None, "Short link did not resolve to a TikTok video", {"url": url, "final": str(current)})


__all__ = ["TikTokHttp", "parse_hydration", "scope_section", "check_api_json", "API_BASE_PARAMS", "BASE_URL", "REFERER"]
