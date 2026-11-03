"""Offline tests for the scraper package (http parsing, error mapping, cache, Scraper facade).

Network is mocked with respx; browser-backed calls are replaced by a fake ``_browser_json``.
"""
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from proxitok.config import Settings
from proxitok.scraper import cache as cache_mod
from proxitok.scraper.api import Scraper
from proxitok.scraper.cache import JSONCache, MemoryCache, NullCache, make_cache
from proxitok.scraper.errors import ScraperError
from proxitok.scraper.http import TikTokHttp, check_api_json, parse_hydration, scope_section

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def universal_html(scope: dict) -> str:
    blob = json.dumps({"__DEFAULT_SCOPE__": scope})
    return (
        "<html><head></head><body>"
        f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{blob}</script>'
        "</body></html>"
    )


# ---------------------------------------------------------------- hydration parsing


def test_parse_universal_user():
    scope = parse_hydration(universal_html(load("user_tiktok")))
    sec = scope_section(scope, "webapp.user-detail")
    assert sec["userInfo"]["user"]["uniqueId"] == "tiktok"


def test_parse_universal_video():
    scope = parse_hydration(universal_html(load("video_tiktok")))
    sec = scope_section(scope, "webapp.video-detail")
    assert sec["itemInfo"]["itemStruct"]["id"] == "7702947070604332319"


def test_parse_sigi_fallback():
    sigi = {
        "UserModule": {"users": {"bob": {"uniqueId": "bob", "nickname": "Bob", "secUid": "S"}},
                       "stats": {"bob": {"followerCount": 5}}},
        "ItemModule": {"123": {"id": "123", "desc": "hi", "author": "bob", "video": {"playAddr": "https://v"}}},
        "SEOState": {"metaParams": {"title": "T", "description": "D"}},
    }
    html = f'<script id="SIGI_STATE" type="application/json">{json.dumps(sigi)}</script>'
    scope = parse_hydration(html)
    item = scope["webapp.video-detail"]["itemInfo"]["itemStruct"]
    assert item["id"] == "123"
    assert item["author"]["nickname"] == "Bob"  # expanded from UserModule
    assert scope["webapp.video-detail"]["shareMeta"] == {"title": "T", "desc": "D"}


def test_parse_sigi_user_only():
    sigi = {"UserModule": {"users": {"bob": {"uniqueId": "bob"}}, "stats": {"bob": {"followerCount": 5}}}}
    scope = parse_hydration(f'<script id="SIGI_STATE">{json.dumps(sigi)}</script>')
    assert scope["webapp.user-detail"]["userInfo"]["stats"]["followerCount"] == 5


def test_parse_no_data():
    with pytest.raises(ScraperError) as ei:
        parse_hydration("<html><title>Verify</title></html>")
    assert ei.value.http_code == 502 and ei.value.tiktok_code == -1


def test_parse_broken_json():
    with pytest.raises(ScraperError):
        parse_hydration('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{not json</script>')


# ---------------------------------------------------------------- error mapping


@pytest.mark.parametrize(
    "code,http_code,msg",
    [
        (10204, 404, "Video not found"),
        (10221, 404, "User not found or banned"),
        (10202, 404, "User not found"),
        (10000, 502, "Captcha required"),
        (10216, 403, "This video is private"),
    ],
)
def test_scope_section_status_codes(code, http_code, msg):
    with pytest.raises(ScraperError) as ei:
        scope_section({"webapp.video-detail": {"statusCode": code, "statusMsg": "raw tiktok msg"}}, "webapp.video-detail", "u")
    e = ei.value
    assert (e.http_code, e.tiktok_code, e.msg) == (http_code, code, msg)
    assert e.debug["statusMsg"] == "raw tiktok msg"


def test_scope_section_missing_key():
    with pytest.raises(ScraperError) as ei:
        scope_section({"webapp.app-context": {}}, "webapp.user-detail")
    assert ei.value.tiktok_code == -1


def test_check_api_json_empty():
    for body in ({}, None, [], ""):
        with pytest.raises(ScraperError) as ei:
            check_api_json(body)
        assert ei.value.tiktok_code == -1


def test_check_api_json_unknown_code_keeps_tiktok_msg():
    with pytest.raises(ScraperError) as ei:
        check_api_json(load("recommend_user_error"))
    assert ei.value.tiktok_code == 4
    assert ei.value.http_code == 502
    assert ei.value.msg  # TikTok's statusMsg (unknown code)


def test_check_api_json_snake_case_code():
    with pytest.raises(ScraperError) as ei:
        check_api_json({"status_code": 10000, "status_msg": "verify"})
    assert ei.value.tiktok_code == 10000 and ei.value.msg == "Captcha required"


def test_check_api_json_ok():
    d = load("music_item_list")
    assert check_api_json(d) is d


# ---------------------------------------------------------------- cache


async def test_memory_cache_ttl(monkeypatch):
    c = MemoryCache()
    now = [1000.0]
    monkeypatch.setattr(cache_mod.time, "time", lambda: now[0])
    await c.set("k", {"a": 1}, 10)
    assert await c.get("k") == {"a": 1}
    now[0] += 11
    assert await c.get("k") is None


async def test_memory_cache_lru():
    c = MemoryCache(max_entries=2)
    await c.set("a", 1, 60)
    await c.set("b", 2, 60)
    await c.get("a")  # a becomes most recent
    await c.set("c", 3, 60)
    assert await c.get("b") is None
    assert await c.get("a") == 1 and await c.get("c") == 3


async def test_memory_cache_zero_ttl():
    c = MemoryCache()
    await c.set("a", 1, 0)
    assert await c.get("a") is None


async def test_json_cache(tmp_path, monkeypatch):
    c = JSONCache(str(tmp_path / "api"))
    await c.set("user:tiktok", {"x": [1, "é"]}, 60)
    assert await c.get("user:tiktok") == {"x": [1, "é"]}
    assert await c.get("missing") is None
    files = list((tmp_path / "api").glob("*.json"))
    assert len(files) == 1
    real = time.time
    monkeypatch.setattr(cache_mod.time, "time", lambda: real() + 120)
    assert await c.get("user:tiktok") is None
    assert not list((tmp_path / "api").glob("*.json"))  # expired file removed


async def test_null_cache():
    c = NullCache()
    await c.set("a", 1, 60)
    assert await c.get("a") is None


def test_make_cache_kinds(tmp_path):
    assert isinstance(make_cache(Settings(API_CACHE="none")), NullCache)
    assert isinstance(make_cache(Settings(API_CACHE="memory")), MemoryCache)
    assert isinstance(make_cache(Settings(API_CACHE="json", API_CACHE_JSON=str(tmp_path / "c"))), JSONCache)
    # redis without host/url configured -> falls back to memory instead of crashing
    assert isinstance(make_cache(Settings(API_CACHE="redis", REDIS_URL=None, REDIS_HOST=None)), MemoryCache)


# ---------------------------------------------------------------- cookies


async def test_cookie_sync_and_header():
    h = TikTokHttp("UA")
    h.set_cookies([
        {"name": "tt_chain_token", "value": "abc", "domain": ".tiktok.com", "path": "/"},
        {"name": "msToken", "value": "m1", "domain": "www.tiktok.com", "path": "/"},
        {"name": "evil", "value": "x", "domain": ".example.com", "path": "/"},
    ])
    hdr = h.cookie_header()
    assert "tt_chain_token=abc" in hdr and "msToken=m1" in hdr and "evil" not in hdr
    # overwrite keeps a single value
    h.set_cookies([{"name": "tt_chain_token", "value": "new", "domain": ".tiktok.com", "path": "/"}])
    assert h.cookie_header().count("tt_chain_token=") == 1 and "tt_chain_token=new" in h.cookie_header()
    await h.aclose()


# ---------------------------------------------------------------- Scraper facade (mocked network)


@pytest.fixture
async def scraper():
    s = Scraper(Settings(API_CACHE="memory", PROXY=None))
    await s.start()
    yield s
    await s.close()


class FakeBrowser:
    """Replacement for Scraper._browser_json: path -> response dict or exception."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def __call__(self, path, params, attempts=3):
        self.calls.append((path, dict(params)))
        r = self.responses.get(path)
        if isinstance(r, Exception):
            raise r
        if r is None:
            raise ScraperError(502, -1, "Empty response from TikTok")
        return check_api_json(r, path)


def blocked():
    return ScraperError(502, -1, "TikTok returned an empty response (HTTP 403)")


@respx.mock
async def test_user_info_and_feed(scraper):
    page = respx.get("https://www.tiktok.com/@tiktok").mock(
        return_value=httpx.Response(200, text=universal_html(load("user_tiktok")))
    )
    feed_route = respx.get(url__startswith="https://www.tiktok.com/api/creator/item_list/").mock(
        return_value=httpx.Response(200, json=load("creator_item_list"))
    )
    info, feed = await scraper.user("@tiktok")
    assert info.detail.unique_id == "tiktok"
    assert len(feed.items) == 15 and feed.has_more
    oldest = min(i.create_time for i in feed.items)
    assert feed.cursor == str(oldest * 1000)
    q = parse_qs(urlparse(str(feed_route.calls.last.request.url)).query)
    assert q["secUid"][0] == info.detail.sec_uid and q["type"][0] == "1"
    first_cursor = int(q["cursor"][0])
    assert abs(first_cursor - time.time() * 1000) < 60_000  # "0" -> now in ms

    # next page: cursor stepped one second back to avoid repeating the boundary item
    await scraper.user("tiktok", feed.cursor)
    q = parse_qs(urlparse(str(feed_route.calls.last.request.url)).query)
    assert q["cursor"][0] == str(oldest * 1000 - 1000)

    # cached: repeat calls don't refetch
    await scraper.user("tiktok")
    assert page.call_count == 1
    assert feed_route.call_count == 2


@respx.mock
async def test_user_not_found(scraper):
    respx.get("https://www.tiktok.com/@nobody_zz").mock(
        return_value=httpx.Response(200, text=universal_html({"webapp.user-detail": {"statusCode": 10221, "statusMsg": "user banned"}}))
    )
    with pytest.raises(ScraperError) as ei:
        await scraper.user("nobody_zz")
    assert ei.value.http_code == 404 and ei.value.tiktok_code == 10221


async def test_user_invalid_name(scraper):
    with pytest.raises(ScraperError) as ei:
        await scraper.user("../etc/passwd")
    assert ei.value.http_code == 404


@respx.mock
async def test_user_private_has_empty_feed(scraper):
    scope = load("user_tiktok")
    scope["webapp.user-detail"]["userInfo"]["user"]["privateAccount"] = True
    respx.get("https://www.tiktok.com/@tiktok").mock(return_value=httpx.Response(200, text=universal_html(scope)))
    feed_route = respx.get(url__startswith="https://www.tiktok.com/api/creator/item_list/")
    info, feed = await scraper.user("tiktok")
    assert info.detail.private_account and feed.items == [] and not feed.has_more
    assert feed_route.call_count == 0


@respx.mock
async def test_user_feed_failure_keeps_info(scraper):
    respx.get("https://www.tiktok.com/@tiktok").mock(return_value=httpx.Response(200, text=universal_html(load("user_tiktok"))))
    respx.get(url__startswith="https://www.tiktok.com/api/creator/item_list/").mock(return_value=httpx.Response(200, content=b""))
    info, feed = await scraper.user("tiktok")
    assert info.detail.unique_id == "tiktok" and feed.items == []


@respx.mock
async def test_user_network_error(scraper):
    respx.get("https://www.tiktok.com/@tiktok").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ScraperError) as ei:
        await scraper.user("tiktok")
    assert ei.value.http_code == 502


@respx.mock
async def test_video_with_comments_failure(scraper):
    route = respx.get("https://www.tiktok.com/@tiktok/video/7702947070604332319").mock(
        return_value=httpx.Response(200, text=universal_html(load("video_tiktok")))
    )
    # TikTok answers an empty 200 body when it refuses the request
    croute = respx.get(url__startswith="https://www.tiktok.com/api/comment/list/").mock(
        return_value=httpx.Response(200, content=b"")
    )
    item, info, comments = await scraper.video("7702947070604332319", "tiktok")
    assert item.id == "7702947070604332319" and item.has_video
    assert info.detail.unique_id == "tiktok"
    assert comments == [] and item.comments == []
    # cached video + negative-cached comments
    await scraper.video("7702947070604332319", "tiktok")
    assert route.call_count == 1 and croute.call_count == 1


@respx.mock
async def test_video_comments_parsed(scraper):
    respx.get("https://www.tiktok.com/@_/video/7702947070604332319").mock(
        return_value=httpx.Response(200, text=universal_html(load("video_tiktok")))
    )
    croute = respx.get(url__startswith="https://www.tiktok.com/api/comment/list/").mock(
        return_value=httpx.Response(200, json={"status_code": 0, "comments": [
            {"cid": "1", "text": "nice", "create_time": 5, "digg_count": 2,
             "user": {"unique_id": "u1", "nickname": "U", "avatar_thumb": {"url_list": ["https://a"]}}}]})
    )
    scraper._browser_json = FakeBrowser({})  # comments must not go through the browser
    item, _, comments = await scraper.video("7702947070604332319")
    assert [c.text for c in comments] == ["nice"] and comments[0].user.avatar_thumb == "https://a"
    assert item.comments is comments
    assert scraper._browser_json.calls == []
    # Unsigned comment/list only works without the web-app params (device_platform=web_pc
    # makes TikTok demand signatures and return an empty body).
    q = croute.calls.last.request.url.params
    assert q["aweme_id"] == "7702947070604332319" and q["aid"] == "1988"
    assert "device_platform" not in q and "app_name" not in q


@respx.mock
async def test_video_not_found(scraper):
    respx.get("https://www.tiktok.com/@_/video/7000000000000000001").mock(
        return_value=httpx.Response(200, text=universal_html({"webapp.video-detail": {"statusCode": 10204}}))
    )
    with pytest.raises(ScraperError) as ei:
        await scraper.video("7000000000000000001")
    assert ei.value.http_code == 404 and ei.value.tiktok_code == 10204


async def test_video_bad_id(scraper):
    with pytest.raises(ScraperError) as ei:
        await scraper.video("abc")
    assert ei.value.http_code == 404


@respx.mock
async def test_photo_post(scraper):
    scope = load("photo_post")
    vid = scope["webapp.video-detail"]["itemInfo"]["itemStruct"]["id"]
    respx.get(f"https://www.tiktok.com/@_/video/{vid}").mock(return_value=httpx.Response(200, text=universal_html(scope)))
    scraper._browser_json = FakeBrowser({})
    item, _, _ = await scraper.video(vid)
    assert item.is_image_post and item.image_post.images


async def test_tag_with_empty_feed(scraper):
    fake = FakeBrowser({"/api/challenge/detail/": load("challenge_detail"), "/api/challenge/item_list/": blocked()})
    scraper._browser_json = fake
    info, feed = await scraper.tag("fyp")
    assert info.detail.title == "fyp" and info.detail.id
    assert feed.items == []
    item_call = [c for c in fake.calls if c[0] == "/api/challenge/item_list/"][0]
    assert item_call[1]["challengeID"] == info.detail.id


async def test_tag_not_found(scraper):
    scraper._browser_json = FakeBrowser({"/api/challenge/detail/": {"statusCode": 10205}})
    with pytest.raises(ScraperError) as ei:
        await scraper.tag("nonexistenttag")
    assert ei.value.http_code == 404


async def test_tag_blocked_raises(scraper):
    scraper._browser_json = FakeBrowser({"/api/challenge/detail/": blocked()})
    with pytest.raises(ScraperError) as ei:
        await scraper.tag("fyp")
    assert ei.value.tiktok_code == -1


async def test_music_fallback_info(scraper):
    ml = load("music_item_list")
    mid = ml["itemList"][0]["music"]["id"]
    scraper._browser_json = FakeBrowser({"/api/music/detail/": blocked(), "/api/music/item_list/": ml})
    info, feed = await scraper.music(f"some-slug-{mid}")
    assert info.detail.id == mid and info.og.title
    assert len(feed.items) == len(ml["itemList"])


async def test_music_all_blocked(scraper):
    scraper._browser_json = FakeBrowser({"/api/music/detail/": blocked(), "/api/music/item_list/": blocked()})
    with pytest.raises(ScraperError) as ei:
        await scraper.music("7703578453568310029")
    assert ei.value.http_code == 502


async def test_music_fallback_video_count_unknown(scraper):
    """Fallback info must not claim "Videos: 0" - the count is unknown."""
    ml = load("music_item_list")
    scraper._browser_json = FakeBrowser({"/api/music/detail/": blocked(), "/api/music/item_list/": ml})
    info, _ = await scraper.music(ml["itemList"][0]["music"]["id"])
    assert info.stats.video_count is None


async def test_music_empty_feed_detail_blocked_is_not_404(scraper):
    """Regression: a blocked detail call + empty first page is a TikTok failure (502), not "Music not found"."""
    scraper._browser_json = FakeBrowser({"/api/music/detail/": blocked(), "/api/music/item_list/": {"statusCode": 0, "itemList": []}})
    with pytest.raises(ScraperError) as ei:
        await scraper.music("7703578453568310029")
    assert ei.value.http_code == 502


async def test_music_empty_feed_not_found(scraper):
    """404 only when TikTok said so (detail -> 10203) or detail answered without info and the feed is empty."""
    scraper._browser_json = FakeBrowser({"/api/music/detail/": {"statusCode": 10203}, "/api/music/item_list/": {"statusCode": 0, "itemList": []}})
    with pytest.raises(ScraperError) as ei:
        await scraper.music("7703578453568310029")
    assert ei.value.http_code == 404 and ei.value.tiktok_code == 10203
    scraper._cache = NullCache()
    scraper._browser_json = FakeBrowser({"/api/music/detail/": {"statusCode": 0}, "/api/music/item_list/": {"statusCode": 0, "itemList": []}})
    with pytest.raises(ScraperError) as ei:
        await scraper.music("7703578453568310029")
    assert ei.value.http_code == 404


async def test_music_past_last_page_keeps_header(scraper):
    """Regression: an empty later page with detail blocked renders (remembered) info, never a 404."""
    ml = load("music_item_list")
    mid = ml["itemList"][0]["music"]["id"]
    pages = {"0": ml, "99999999": {"statusCode": 0, "itemList": []}}

    async def browser_json(path, params, attempts=3):
        if path == "/api/music/detail/":
            raise blocked()
        return check_api_json(pages[params["cursor"]], path)

    scraper._browser_json = browser_json
    await scraper.music(mid)  # first page remembers the sound's music block
    info, feed = await scraper.music(mid, cursor="99999999")
    assert feed.items == [] and info.detail.id == mid and info.detail.title
    # Without a remembered first page: minimal header, still no 404.
    info, feed = await scraper.music("7000000000000000001", cursor="99999999")
    assert feed.items == [] and info.detail.id == "7000000000000000001"


async def test_media_cache_keys_are_per_process(tmp_path):
    """Regression: cached items carry cookie-bound CDN URLs, so a new process must not reuse them."""
    settings = Settings(API_CACHE="json", API_CACHE_JSON=str(tmp_path), PROXY=None)
    a, b = Scraper(settings), Scraper(settings)
    await a.start()
    await b.start()
    try:
        assert a._media_key("video:1") != b._media_key("video:1")
        assert a._media_key("video:1") == a._media_key("video:1")
        calls = []

        async def factory():
            calls.append(1)
            return {"x": 1}

        await a._cached(a._media_key("video:1"), factory)
        await a._cached(a._media_key("video:1"), factory)
        await b._cached(b._media_key("video:1"), factory)
        assert len(calls) == 2
    finally:
        await a.close()
        await b.close()


async def test_foryou(scraper):
    scraper._browser_json = FakeBrowser({"/api/recommend/item_list/": load("recommend_item_list")})
    feed = await scraper.foryou()
    assert feed.items and feed.cursor == "0"


@respx.mock
async def test_foryou_hydration_fallback(scraper):
    items = load("recommend_item_list")["itemList"][:1]
    respx.get("https://www.tiktok.com/foryou").mock(return_value=httpx.Response(
        200, text=universal_html({"webapp.foryou.video": {"statusCode": 0, "itemList": items}})))
    scraper._browser_json = FakeBrowser({"/api/recommend/item_list/": blocked()})
    feed = await scraper.foryou()
    assert len(feed.items) == 1 and not feed.has_more


async def test_following(scraper):
    fake = FakeBrowser({"/api/user/list/": load("user_list")})
    scraper._browser_json = fake
    uf = await scraper.following()
    assert len(uf.users) == 31 and uf.users[0].detail.unique_id
    assert fake.calls[0][1]["scene"] == "151"


async def test_following_blocked(scraper):
    scraper._browser_json = FakeBrowser({"/api/user/list/": blocked()})
    with pytest.raises(ScraperError) as ei:
        await scraper.following()
    assert ei.value.http_code == 502 and "suggested accounts" in ei.value.msg


# ---------------------------------------------------------------- short links


@respx.mock
async def test_resolve_short(scraper):
    respx.get("https://vm.tiktok.com/ZMabc/").mock(return_value=httpx.Response(
        301, headers={"location": "https://www.tiktok.com/@some.user/video/7123456789012345678?_r=1&u_code=x"}))
    assert await scraper.resolve_short("https://vm.tiktok.com/ZMabc/") == "https://www.tiktok.com/@some.user/video/7123456789012345678"


@respx.mock
async def test_resolve_short_t_path_photo(scraper):
    respx.get("https://www.tiktok.com/t/ZTabc/").mock(return_value=httpx.Response(
        302, headers={"location": "/@u/photo/7123"}))
    assert await scraper.resolve_short("https://www.tiktok.com/t/ZTabc/") == "https://www.tiktok.com/@u/video/7123"


@respx.mock
async def test_resolve_short_empty_username_photo(scraper):
    # Live: vm.tiktok.com/ZMArSK11J/ -> https://www.tiktok.com/@/photo/<id>?_r=1...
    respx.get("https://vm.tiktok.com/ZMphoto/").mock(return_value=httpx.Response(
        302, headers={"location": "https://www.tiktok.com/@/photo/7517015682219855109?_r=1&_d=x"}))
    assert await scraper.resolve_short("https://vm.tiktok.com/ZMphoto/") == \
        "https://www.tiktok.com/@placeholder/video/7517015682219855109"


@respx.mock
async def test_resolve_short_rejects_foreign_redirect(scraper):
    respx.get("https://vt.tiktok.com/ZSx/").mock(return_value=httpx.Response(302, headers={"location": "https://evil.example/@u/video/1"}))
    with pytest.raises(ScraperError) as ei:
        await scraper.resolve_short("https://vt.tiktok.com/ZSx/")
    assert ei.value.http_code == 400


@respx.mock
async def test_resolve_short_unresolved(scraper):
    respx.get("https://vm.tiktok.com/ZMnope/").mock(return_value=httpx.Response(301, headers={"location": "https://www.tiktok.com/?_r=1"}))
    respx.get("https://www.tiktok.com/?_r=1").mock(return_value=httpx.Response(200, text="home"))
    with pytest.raises(ScraperError) as ei:
        await scraper.resolve_short("https://vm.tiktok.com/ZMnope/")
    assert ei.value.http_code == 404


async def test_resolve_short_not_short(scraper):
    for u in ("https://example.com/t/abc", "https://www.tiktok.com/@u/video/1", "https://vm.tiktok.com.evil.com/x"):
        with pytest.raises(ScraperError) as ei:
            await scraper.resolve_short(u)
        assert ei.value.http_code == 400


# ---------------------------------------------------------------- streaming helpers / lifecycle


async def test_stream_helpers(scraper):
    c = scraper.stream_client()
    assert isinstance(c, httpx.AsyncClient) and c.follow_redirects is False
    h = scraper.stream_headers("bytes=0-1")
    assert h["Range"] == "bytes=0-1" and h["Referer"] == "https://www.tiktok.com/" and h["User-Agent"]
    assert "Range" not in scraper.stream_headers()
    assert isinstance(scraper.cookie_header(), str)


async def test_close_idempotent():
    s = Scraper(Settings(API_CACHE="none"))
    await s.start()
    await s.close()
    await s.close()
    assert s.cookie_header() == ""
    with pytest.raises(ScraperError) as ei:
        await s.user("tiktok")
    assert ei.value.http_code == 503
