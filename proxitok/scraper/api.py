"""Scraper facade - the ONLY scraper interface routes may use.

Implementation: plain-HTTP hydration + unsigned creator/item_list (``http.py``) for user and
video pages; a Playwright page (``browser.py``) for the endpoints that need TikTok's signing
(tag, music, For You, suggested users, comments). Raw TikTok JSON is cached (``cache.py``).

General rules for every data method:
  * Returns normalized models from ``proxitok.scraper.models``; never raw dicts.
  * Raises :class:`proxitok.scraper.errors.ScraperError` on any failure (TikTok statusCode != 0,
    empty body, network error, browser timeout). Never returns None.
  * ``cursor`` is the opaque string from the previous ``Feed.cursor`` / ``UserFeed.cursor``
    ("0" or "" = first page). Routes pass ``request.query_params.get("cursor", "0")`` through unchanged.
  * Results may be cached (key = method + args) for ``Settings.CACHE_TTL`` seconds.
  * Methods are safe to call concurrently; browser-backed calls are serialized internally.

Live findings (2026-11-03) the implementation must respect:
  * Plain httpx (desktop Chrome UA) works for page hydration: ``/@user`` and ``/@user/video/<id>``
    (also photo posts under /video/) -> <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">.
    User hydration has userInfo but ``itemList`` is EMPTY -> the feed must come from an API.
  * User feed: ``GET /api/creator/item_list/?aid=1988&secUid=..&count=15&cursor=<ms timestamp>&type=1``
    works UNSIGNED over plain httpx (15 items, newest first). First page cursor = now in ms; next =
    oldest createTime*1000 (see Feed.from_creator_list). ``/api/post/item_list/`` returns an empty body
    even when TikTok's own page requests it (headless) - do not rely on it.
  * In-page ``window.fetch`` (Playwright page on tiktok.com) gets signed (msToken, X-Bogus, X-Gnarly
    appended by TikTok's SDK) and returned JSON for: challenge/detail, recommend/item_list,
    music/item_list. Returned EMPTY body for: post/item_list, challenge/item_list (tag fyp),
    music/detail, user/detail. comment/list was not tested in-page; unsigned it is empty.
    recommend/user returned statusCode 4.
  * ``/api/user/list/`` (suggested accounts, used for Following/Discover) was only obtained by
    intercepting the page's own request (page.on("response")) on /@tiktok; a direct call was not tested.
  * CDN video URLs (playAddr/downloadAddr on *.tiktok.com / *.tiktokcdn*.com) return 403 unless the
    request carries the SAME cookie jar (tt_chain_token, ttwid) as the fetch that produced the URL,
    plus ``Referer: https://www.tiktok.com/``. Covers/avatars/music playUrl work without cookies.
    -> items obtained via the browser need its cookies synced into the httpx jar, and cached items are
    only streamable while that jar is alive (persist/reuse it; don't rotate cookies per request).
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional
from urllib.parse import quote

from .browser import BrowserSession
from .cache import Cache, NullCache, make_cache
from .errors import ScraperError
from .http import BASE_URL, REFERER, TikTokHttp, scope_section
from .models import Comment, Feed, Item, Music, MusicInfo, OgMeta, TagInfo, UserFeed, UserInfo

if TYPE_CHECKING:  # pragma: no cover
    import httpx

    from ..config import Settings

log = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_DIGITS_RE = re.compile(r"^\d{1,25}$")

COMMENTS_TIMEOUT = 12.0
NEGATIVE_TTL = 120  # seconds to remember "comments unavailable" for a video


def _clean_cursor(cursor: Optional[str]) -> str:
    c = (cursor or "").strip()
    return c if _DIGITS_RE.match(c) else "0"


class Scraper:
    """Single shared instance per app, created in the FastAPI lifespan:

    .. code-block:: python

        scraper = Scraper(settings)
        await scraper.start()
        app.state.scraper = scraper
        ...
        await scraper.close()
    """

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self._http: Optional[TikTokHttp] = None
        self._browser: Optional[BrowserSession] = None
        self._cache: Cache = NullCache()
        self._ttl = int(settings.CACHE_TTL)
        # Per-process namespace for cache entries that contain CDN media URLs (playAddr/downloadAddr).
        # Those URLs only stream with this process's cookie jar (tt_chain_token), so they must not be
        # served from a JSON/Redis cache by a restarted process or another worker.
        self._session_ns = secrets.token_hex(6)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Create the shared httpx client (UA, PROXY, cookie jar) and the cache backend.

        Must be cheap/non-fatal: the Playwright browser is started lazily on the first call that
        needs it, so the app boots even if Chromium is unavailable (those routes then raise
        ScraperError(http_code=503)).
        """
        if self._http is not None:
            return
        s = self.settings
        self._http = TikTokHttp(s.USER_AGENT, s.PROXY, timeout=max(5.0, float(s.BROWSER_TIMEOUT)))
        self._cache = make_cache(s)
        self._browser = BrowserSession(
            headless=s.BROWSER_HEADLESS,
            timeout=float(s.BROWSER_TIMEOUT),
            proxy=s.PROXY,
            get_cookies=self._http.cookie_items,
            put_cookies=self._http.set_cookies,
        )

    async def close(self) -> None:
        """Close browser (if launched), httpx clients and cache connections. Idempotent."""
        browser, http, cache = self._browser, self._http, self._cache
        self._browser, self._http, self._cache = None, None, NullCache()
        if browser is not None:
            await browser.close()
        if http is not None:
            await http.aclose()
        await cache.close()

    # ------------------------------------------------------------------ internals

    @property
    def http(self) -> TikTokHttp:
        if self._http is None:
            raise ScraperError(503, None, "Scraper not started")
        return self._http

    @property
    def browser(self) -> BrowserSession:
        if self._browser is None:
            raise ScraperError(503, None, "Scraper not started")
        return self._browser

    async def _cached(self, key: str, factory: Callable[[], Awaitable[Any]], ttl: Optional[int] = None) -> Any:
        """Return cached raw JSON for ``key`` or compute it with ``factory`` (only successes are stored)."""
        hit = await self._cache.get(key)
        if hit is not None:
            return hit
        value = await factory()
        await self._cache.set(key, value, self._ttl if ttl is None else ttl)
        return value

    def _media_key(self, key: str) -> str:
        """Cache key for raw JSON that carries cookie-bound media URLs (see ``_session_ns``)."""
        return f"{key}@{self._session_ns}"

    async def _ensure_cookies(self) -> None:
        """Make sure the httpx jar has a TikTok identity before the browser copies it."""
        if not any(name == "tt_chain_token" for name, *_ in self.http.cookie_items()):
            try:
                await self.http.get_page(f"{BASE_URL}/explore")
            except ScraperError:
                pass

    async def _browser_json(self, path: str, params: dict[str, str], attempts: int = 3) -> dict:
        if not self.browser.started:
            await self._ensure_cookies()
        return await self.browser.fetch_json(path, params, attempts=attempts)

    # ------------------------------------------------------------------ pages

    async def user(self, username: str, cursor: str = "0") -> tuple[UserInfo, Feed]:
        """Profile + one page of posts.

        username: without leading "@". cursor: "0" first page, else previous ``Feed.cursor``
        (a millisecond timestamp string for this feed).
        Info from ``/@username`` hydration (webapp.user-detail); og from its shareMeta.
        Feed from /api/creator/item_list/ using the secUid. Private accounts: return info with an
        empty Feed (has_more=False) rather than raising.
        If the feed request fails the info is still returned with an empty Feed (logged).
        Raises ScraperError(404, 10221 [live value for nonexistent user] / 10202) when missing/banned.
        """
        name = (username or "").lstrip("@").strip()
        if not _USERNAME_RE.match(name):
            raise ScraperError.from_tiktok_code(10202, debug={"username": username})
        url = f"{BASE_URL}/@{quote(name)}"

        async def fetch_info() -> dict:
            sec = scope_section(await self.http.hydration(url), "webapp.user-detail", url)
            return {"userInfo": sec.get("userInfo") or {}, "shareMeta": sec.get("shareMeta") or {}}

        raw = await self._cached(f"user:{name.lower()}", fetch_info)
        info = UserInfo.from_tiktok(raw["userInfo"], raw["shareMeta"])
        if not info.detail.unique_id:
            raise ScraperError.from_tiktok_code(10202, debug={"url": url})
        if info.detail.private_account or not info.detail.sec_uid:
            return info, Feed(items=[], has_more=False, cursor="0")

        cur = _clean_cursor(cursor)
        sec_uid = info.detail.sec_uid

        async def fetch_feed() -> dict:
            # Feed.cursor is the oldest item's createTime in ms; the API compares inclusively at
            # second granularity -> step one second back so the boundary item isn't repeated.
            api_cursor = str(int(time.time() * 1000)) if cur == "0" else str(max(int(cur) - 1000, 1))
            return await self.http.get_api(
                "/api/creator/item_list/",
                {"secUid": sec_uid, "count": "15", "cursor": api_cursor, "type": "1"},
            )

        try:
            feed = Feed.from_creator_list(await self._cached(self._media_key(f"ufeed:{sec_uid}:{cur}"), fetch_feed))
        except ScraperError as e:
            log.warning("user feed for %s failed: %s", name, e)
            feed = Feed(items=[], has_more=False, cursor="0")
        return info, feed

    async def video(self, video_id: str, username: Optional[str] = None) -> tuple[Item, UserInfo, list[Comment]]:
        """Single post page (video or photo post).

        Fetches ``/@{username or '_'}/video/{video_id}`` hydration (webapp.video-detail).
        Returns (item, author_info, comments):
          * item: Item with ``item.comments`` also set to the comments list.
          * author_info: UserInfo.from_author(itemStruct.author, authorStats, shareMeta, authorStatsV2)
            -> templates use info.detail.unique_id / nickname / avatar_thumb and info.og.
          * comments: best effort from /api/comment/list/ (aweme_id=video_id, count=20); [] on any
            failure - never raises because of comments.
        Raises ScraperError(404, 10204) if the video doesn't exist, (403, 10216) if private. Live: nonexistent id -> statusCode 10204;
        a placeholder username ("_") in the URL works fine.
        """
        vid = (video_id or "").strip()
        if not _DIGITS_RE.match(vid):
            raise ScraperError.from_tiktok_code(10204, debug={"video_id": video_id})
        user = (username or "").lstrip("@").strip()
        if not _USERNAME_RE.match(user):
            user = "_"
        url = f"{BASE_URL}/@{quote(user)}/video/{vid}"

        async def fetch() -> dict:
            sec = scope_section(await self.http.hydration(url), "webapp.video-detail", url)
            struct = (sec.get("itemInfo") or {}).get("itemStruct") or {}
            if not struct.get("id"):
                raise ScraperError.from_tiktok_code(10204, debug={"url": url})
            return {"itemStruct": struct, "shareMeta": sec.get("shareMeta") or {}}

        raw = await self._cached(self._media_key(f"video:{vid}"), fetch)
        st = raw["itemStruct"]
        item = Item.from_tiktok(st)
        info = UserInfo.from_author(st.get("author"), st.get("authorStats"), raw["shareMeta"], st.get("authorStatsV2"))
        comments = await self._comments(vid)
        item.comments = comments
        return item, info, comments

    async def _comments(self, vid: str) -> list[Comment]:
        key = f"comments:{vid}"
        hit = await self._cache.get(key)
        if hit is not None:
            return [Comment.from_tiktok(c) for c in hit]
        try:
            data = await asyncio.wait_for(
                self._browser_json("/api/comment/list/", {"aweme_id": vid, "count": "20", "cursor": "0", "from_page": "video"}, attempts=1),
                timeout=COMMENTS_TIMEOUT,
            )
            raw = [c for c in (data.get("comments") or []) if isinstance(c, dict)]
            await self._cache.set(key, raw, self._ttl)
        except (ScraperError, asyncio.TimeoutError) as e:
            log.info("comments for %s unavailable: %s", vid, e)
            await self._cache.set(key, [], NEGATIVE_TTL)
            return []
        except Exception as e:  # never fail the video page because of comments
            log.warning("comments for %s crashed: %r", vid, e)
            return []
        return [Comment.from_tiktok(c) for c in raw]

    async def tag(self, name: str, cursor: str = "0") -> tuple[TagInfo, Feed]:
        """Hashtag page. Info via /api/challenge/detail/?challengeName=name (signed, in-page).
        Feed via /api/challenge/item_list/?challengeID=<id>&count=30&cursor=<cursor> (signed).
        NOTE: challenge/item_list returned an empty body in the live probe -> on empty feed return the
        info with an empty Feed instead of failing the whole page (templates show "No items").
        Raises ScraperError(404, 10205) when the tag doesn't exist.
        """
        tag = (name or "").lstrip("#").strip()
        if not tag or len(tag) > 150 or any(c in tag for c in "/?#&"):
            raise ScraperError.from_tiktok_code(10205, debug={"tag": name})

        async def fetch_info() -> dict:
            return await self._browser_json("/api/challenge/detail/", {"challengeName": tag, "from_page": "hashtag"})

        raw = await self._cached(f"tag:{tag.lower()}", fetch_info)
        info = TagInfo.from_tiktok(raw)
        if not info.detail.id:
            raise ScraperError.from_tiktok_code(10205, debug={"tag": tag})

        cur = _clean_cursor(cursor)
        try:
            data = await self._cached(
                self._media_key(f"tfeed:{info.detail.id}:{cur}"),
                lambda: self._browser_json(
                    "/api/challenge/item_list/",
                    {"challengeID": info.detail.id, "count": "30", "cursor": cur, "coverFormat": "2", "from_page": "hashtag"},
                    attempts=2,
                ),
            )
            feed = Feed.from_tiktok(data)
        except ScraperError as e:
            log.info("tag feed for %s unavailable: %s", tag, e)
            feed = Feed(items=[], has_more=False, cursor="0")
        return info, feed

    async def music(self, music_id: str, cursor: str = "0") -> tuple[MusicInfo, Feed]:
        """Sound page. Feed via /api/music/item_list/?musicID=..&count=30&cursor=.. (signed, works).
        Info via /api/music/detail/?musicId=.. (returned EMPTY in probe) -> fall back to
        MusicInfo.from_music(first_item.music) when detail is unavailable (``stats.video_count`` is then
        None = unknown). See ``_music_fallback_info`` for when 404 vs. the blocking error is raised.
        """
        mid = (music_id or "").strip()
        m = re.search(r"(\d{6,25})$", mid)  # accept "<slug>-<id>" too
        if not m:
            raise ScraperError.from_tiktok_code(10203, debug={"music_id": music_id})
        mid = m.group(1)
        cur = _clean_cursor(cursor)

        info: Optional[MusicInfo] = None
        detail_err: Optional[ScraperError] = None
        try:
            raw = await self._cached(f"music:{mid}", lambda: self._browser_json("/api/music/detail/", {"musicId": mid, "from_page": "music"}, attempts=1))
            cand = MusicInfo.from_tiktok(raw)
            if cand.detail.id:
                info = cand
        except ScraperError as e:
            detail_err = e
            log.info("music detail for %s unavailable: %s", mid, e)

        feed_err: Optional[ScraperError] = None
        data: dict = {}
        try:
            data = await self._cached(
                self._media_key(f"mfeed:{mid}:{cur}"),
                lambda: self._browser_json("/api/music/item_list/", {"musicID": mid, "count": "30", "cursor": cur, "coverFormat": "2", "from_page": "music"}),
            )
            feed = Feed.from_tiktok(data)
        except ScraperError as e:
            feed_err = e
            feed = Feed(items=[], has_more=False, cursor="0")

        if info is None:
            info = await self._music_fallback_info(mid, cur, data, feed_err, detail_err)
        return info, feed

    async def _music_fallback_info(self, mid: str, cur: str, data: dict, feed_err: Optional[ScraperError],
                                   detail_err: Optional[ScraperError]) -> MusicInfo:
        """MusicInfo when /api/music/detail/ gave nothing: from a feed item's ``music`` (remembered so later
        pages of the same sound keep the header), else a minimal header on later pages. A 404 is only
        reported when TikTok itself said the sound doesn't exist (or a first page is empty without any
        transient failure); a blocked request is reported as that error (502), not as "not found"."""
        key = f"musicitem:{mid}"
        raws = [i.get("music") for i in (data.get("itemList") or []) if isinstance(i, dict) and isinstance(i.get("music"), dict)]
        raw_music = next((m for m in raws if Music.from_tiktok(m).id == mid), None) or (raws[0] if raws else None)
        if raw_music is not None:
            await self._cache.set(key, raw_music, self._ttl)
        else:
            raw_music = await self._cache.get(key)
        if raw_music is not None:
            return MusicInfo.from_music(Music.from_tiktok(raw_music))

        errors = [e for e in (feed_err, detail_err) if e is not None]
        if any(e.http_code == 404 for e in errors):
            raise next(e for e in errors if e.http_code == 404)
        if feed_err is not None:
            raise feed_err
        if cur != "0":
            # Past the last page (or an empty later page): the sound exists, there just are no more items.
            return MusicInfo(detail=Music(id=mid, title="Sound"), og=OgMeta(title="Sound"))
        if detail_err is not None:
            raise detail_err
        raise ScraperError.from_tiktok_code(10203, debug={"music_id": mid})

    async def foryou(self) -> Feed:
        """For You feed via /api/recommend/item_list/?count=30&from_page=fyp (signed, works).
        No real pagination (each call returns fresh items); has_more reflects TikTok's flag and
        cursor stays "0". Not cached. Falls back to the (single-item) feed embedded in the
        /foryou page hydration when the signed call is refused.
        """
        try:
            data = await self._browser_json("/api/recommend/item_list/", {"count": "30", "from_page": "fyp"})
            feed = Feed.from_tiktok(data)
            if feed.items:
                feed.cursor = "0"
                return feed
            err = ScraperError(502, -1, "TikTok returned an empty For You feed")
        except ScraperError as e:
            err = e
        url = f"{BASE_URL}/foryou"
        try:
            sec = scope_section(await self.http.hydration(url), "webapp.foryou.video", url)
            feed = Feed.from_tiktok({"itemList": sec.get("itemList") or []})
            if feed.items:
                return Feed(items=feed.items, has_more=False, cursor="0")
        except ScraperError as e2:
            log.info("foryou hydration fallback failed: %s", e2)
        raise err

    async def following(self, cursor: str = "0") -> UserFeed:
        """Following / Discover page: suggested accounts (/api/user/list/ with the same params TikTok's
        own profile page uses: scene=151, from_page=user; anonymous sessions have no real "following").
        Only the first page is available (cursor is ignored, has_more is always False). Best effort;
        raise ScraperError(502, msg="...explanation...") if TikTok returns nothing.
        """
        async def fetch() -> dict:
            return await self._browser_json(
                "/api/user/list/",
                {"count": "30", "minCursor": "0", "maxCursor": "0", "scene": "151", "from_page": "user"},
            )

        try:
            data = await self._cached("suggested_users", fetch)
        except ScraperError as e:
            raise ScraperError(
                e.http_code if e.http_code in (503, 504) else 502, e.tiktok_code,
                "TikTok did not return suggested accounts (anonymous sessions have no following list and "
                "TikTok often blocks this request for automated browsers). " + e.msg,
                e.debug,
            ) from e
        feed = UserFeed.from_tiktok(data)
        if not feed.users:
            raise ScraperError(502, -1, "TikTok returned no suggested accounts")
        return UserFeed(users=feed.users, has_more=False, cursor="0")

    async def resolve_short(self, url: str) -> str:
        """Follow redirects of a short link (vm.tiktok.com/XXX, vt.tiktok.com/XXX,
        www.tiktok.com/t/XXX) and return the canonical https://www.tiktok.com/@user/video/<id> URL
        (query string stripped; ``https://www.tiktok.com/@user`` if it points at a profile).
        Only tiktok.com hosts are followed. Raises ScraperError(400) if the
        URL isn't a TikTok short link, (404) if it doesn't resolve to a video/user.
        """
        return await self._cached(f"short:{url}", lambda: self.http.resolve_short(url))

    # ------------------------------------------------------------------ media streaming

    def stream_client(self) -> "httpx.AsyncClient":
        """Shared httpx.AsyncClient for the /stream and /download proxy.

        Carries the TikTok session cookie jar (ttwid, tt_chain_token, msToken - synced from the
        browser) and PROXY. ``follow_redirects=False`` - the proxy follows redirects manually so it
        can re-check the domain allowlist on every hop. Do NOT close it; owned by the Scraper.
        Use with ``client.stream("GET", url, headers=scraper.stream_headers(range_header))``.
        """
        return self.http.client

    def stream_headers(self, range_header: Optional[str] = None) -> dict[str, str]:
        """Headers for CDN requests: User-Agent, ``Referer: https://www.tiktok.com/``, Accept,
        and ``Range`` if given. Cookies come from the client jar (not duplicated here).
        """
        h = {"User-Agent": self.settings.USER_AGENT, "Referer": REFERER, "Accept": "*/*"}
        if range_header:
            h["Range"] = range_header
        return h

    def cookie_header(self) -> str:
        """Current TikTok cookies as a ``Cookie:`` header value ("k=v; k2=v2"), for callers that use
        another HTTP client."""
        return self.http.cookie_header() if self._http is not None else ""


__all__ = ["Scraper"]
