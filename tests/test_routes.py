"""Route tests through TestClient with the fixture-backed FakeScraper."""
import json
import re

import pytest

from proxitok.scraper.errors import ScraperError
from tests.fake_scraper import PHOTO_ID, VIDEO_ID, client, fake, make_client  # noqa: F401

HTML = "text/html; charset=utf-8"


# ---------------------------------------------------------------- static pages

@pytest.mark.parametrize("path,needle", [
    ("/", "Welcome to ProxiTok!"),
    ("/about", "Frontend version"),
    ("/verify", "VERIFY_CODE"),
    ("/settings", 'name="theme"'),
])
def test_static_pages(client, path, needle):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"] == HTML
    assert needle in r.text
    assert '<link rel="stylesheet" href="/styles/vendor/bulma.min.css">' in r.text


def test_home_form(client):
    r = client.get("/")
    assert 'action="/redirect/search" method="GET"' in r.text
    for t in ("url", "user", "tag", "music", "video"):
        assert f'<option value="{t}">' in r.text


def test_manifest(make_client):
    r = make_client().get("/manifest")
    assert r.status_code == 200 and r.headers["content-type"] == "application/json"
    data = r.json()
    assert data == {
        "name": "ProxiTok", "short_name": "ProxiTok",
        "description": "Use TikTok with a privacy-friendly alternative frontend", "lang": "en-US",
        "theme_color": "#4040ff", "background_color": "#ffffff", "display": "standalone",
        "orientation": "portrait-primary",
        "icons": [
            {"src": "/android-chrome-192x192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/android-chrome-512x512.png", "sizes": "512x512", "type": "image/png"},
        ],
        "start_url": "/", "scope": "/",
    }
    data = make_client(APP_URL="https://pt.example/").get("/manifest").json()
    assert data["start_url"] == "https://pt.example/"
    assert data["icons"][0]["src"] == "https://pt.example/android-chrome-192x192.png"


@pytest.mark.parametrize("path,ctype", [
    ("/favicon.ico", "image/vnd.microsoft.icon"),
    ("/favicon-16x16.png", "image/png"),
    ("/favicon-32x32.png", "image/png"),
    ("/apple-touch-icon.png", "image/png"),
    ("/android-chrome-192x192.png", "image/png"),
    ("/android-chrome-512x512.png", "image/png"),
    ("/robots.txt", "text/plain"),
    ("/embed.js", "javascript"),
    ("/styles/misc.css", "text/css"),
    ("/styles/vendor/bulma.min.css", "text/css"),
    ("/styles/themes/card.css", "text/css"),
    ("/scripts/navbar.js", "javascript"),
    ("/scripts/themes/card.js", "javascript"),
])
def test_static_files(client, path, ctype):
    r = client.get(path)
    assert r.status_code == 200
    assert ctype in r.headers["content-type"]


def test_app_url_prefix(make_client):
    r = make_client(APP_URL="https://pt.example").get("/")
    assert 'href="https://pt.example/styles/misc.css"' in r.text
    assert 'action="https://pt.example/redirect/search"' in r.text


# ---------------------------------------------------------------- errors

def test_404_page(client):
    r = client.get("/this/does/not/exist")
    assert r.status_code == 404
    assert r.headers["content-type"] == HTML
    assert "That endpoint doesn&#39;t exist" in r.text
    assert "There was an error processing your request!" in r.text


def test_405_rendered(client):
    r = client.post("/about")
    assert r.status_code == 405 and "There was an error" in r.text


def test_scraper_error_user_not_found(client):
    r = client.get("/@missing")
    assert r.status_code == 404
    assert "HTTP Code: 404" in r.text
    assert "API error code 10221" in r.text


@pytest.mark.parametrize("err,status,needles,absent", [
    (ScraperError(502, -1, "Empty response from TikTok"), 502,
     ["API error code -1 (Empty response from TikTok)", 'href="/verify">What does this mean?'], []),
    (ScraperError(502, 10000, "Captcha required"), 502, ["API error code 10000", "What does this mean?"], []),
    (ScraperError(403, 10216, "This video is private"), 403, ["API error code 10216"], ["What does this mean?"]),
    (ScraperError(504, None, "Browser timed out <b>x</b>"), 504, ["Browser timed out &lt;b&gt;x&lt;/b&gt;"], ["API error code"]),
])
def test_scraper_error_rendering(client, fake, err, status, needles, absent):
    fake.error = err
    for path in ("/foryou", "/tag/fyp", "/music/1", "/following", f"/@tiktok/video/{VIDEO_ID}", "/@tiktok/rss"):
        r = client.get(path)
        assert r.status_code == status, path
        assert r.headers["content-type"] == HTML
        assert f"HTTP Code: {status}" in r.text
        for n in needles:
            assert n in r.text
        for a in absent:
            assert a not in r.text


def test_debug_info_only_with_app_debug(make_client, fake):
    fake.error = ScraperError(502, -1, "Empty", debug={"url": "https://www.tiktok.com/x", "status": 200})
    r = make_client().get("/foryou")
    assert "Show debug info" not in r.text
    r = make_client(APP_DEBUG=True).get("/foryou")
    assert "Show debug info" in r.text and "https://www.tiktok.com/x" in r.text


# ---------------------------------------------------------------- user

def test_user_page(client, fake):
    r = client.get("/@tiktok")
    assert r.status_code == 200 and r.headers["content-type"] == HTML
    t = r.text
    assert "<title>TikTok - ProxiTok</title>" in t
    assert '<meta property="og:title" content="TikTok on TikTok" />' in t
    assert '<meta property="og:url" content="/@tiktok" />' in t
    assert '<link rel="alternate" type="application/rss+xml" title="TikTok" href="/@tiktok/rss" />' in t
    assert 'href="https://tiktok.com/discovermore"' in t
    assert "Followers: 96.5M" in t
    assert "Category: Others" in t
    assert "gg-check-o" in t  # verified
    # default theme feed
    assert t.count('<article class="media">') == 15
    assert "Download video" in t and "/download?url=" in t
    assert '<a href="/@dadsocial">@Mason Smith</a>' in t
    # pagination: creator list cursor is a ms timestamp; no "First" on the first page
    assert re.search(r'class="button is-success" href="\?cursor=\d{13}">Next</a>', t)
    assert "First</a>" not in t
    assert fake.calls[-1] == ("user", "tiktok", "0")


def test_feed_links_music_page(client, fake):
    t = client.get("/@tiktok").text
    assert re.search(r'<p class="mb-1"><a href="/music/\d+">', t)


def test_user_cursor_passthrough(client, fake):
    r = client.get("/@tiktok?cursor=1793000000000")
    assert r.status_code == 200
    assert fake.calls[-1] == ("user", "tiktok", "1793000000000")
    assert '<a class="button is-danger" href="?cursor=0">First</a>' in r.text


def test_user_trailing_slash(client):
    r = client.get("/@tiktok/", follow_redirects=False)
    assert r.status_code in (200, 307)


def test_card_theme(client):
    client.cookies.set("theme", "card")
    r = client.get("/@tiktok")
    t = r.text
    assert '<link rel="stylesheet" href="/styles/themes/card.css">' in t
    assert '<script src="/scripts/themes/card.js"></script>' in t
    assert t.count('onclick="openVideo(this.id)"') == 15
    for attr in ("data-video_url", "data-video_download_watermark", "data-video_download_nowatermark",
                 "data-video_share_url", "data-desc", "data-createtime", "data-music_title", "data-music_url"):
        assert attr + '="' in t
    for id_ in ("modal", "modal-background", "modal-close", "item_title", "video", "item_date", "share_input",
                "download_watermark", "download_nowatermark", "audio_title", "audio", "back-button", "next-button"):
        assert f'id="{id_}"' in t
    assert '<article class="media">' not in t


def test_invalid_theme_cookie_falls_back(client):
    client.cookies.set("theme", "../evil")
    r = client.get("/@tiktok")
    assert r.status_code == 200 and '<article class="media">' in r.text


def test_user_rss(client):
    r = client.get("/@tiktok/rss")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/rss+xml"
    assert r.headers["content-disposition"] == 'attachment; filename="tiktok.rss"'
    t = r.text
    assert t.startswith('<?xml version="1.0" encoding="utf-8" ?>')
    assert "<title>TikTok</title>" in t
    assert t.count("<item>") == 15
    assert "<link>http://testserver/@tiktok</link>" in t
    assert re.search(r"<pubDate>\w{3}, \d{2} \w{3} \d{4} \d{2}:\d{2}:\d{2} \+0000</pubDate>", t)
    assert "<dc:creator>@tiktok</dc:creator>" in t
    import xml.etree.ElementTree as ET
    ET.fromstring(t.encode())  # well-formed


def test_rss_uses_app_url(make_client):
    t = make_client(APP_URL="https://pt.example").get("/@tiktok/rss").text
    assert "<link>https://pt.example/@tiktok</link>" in t
    assert "https://pt.example/stream?url=" in t


# ---------------------------------------------------------------- video

def test_video_page(client, fake):
    r = client.get(f"/@tiktok/video/{VIDEO_ID}")
    assert r.status_code == 200 and r.headers["content-type"] == HTML
    t = r.text
    assert fake.calls[-1] == ("video", VIDEO_ID, "tiktok")
    assert "<video controls autoplay" in t and 'type="video/mp4"' in t
    assert '<section class="hero is-fullheight">' in t
    assert "Watermark</a>" in t and "No watermark</a>" in t
    assert 'href="https://www.tiktok.com/@tiktok/video/' + VIDEO_ID + '"' in t
    assert '<meta property="og:url" content="/@tiktok/video/' + VIDEO_ID + '" />' in t
    # comments, escaped
    assert "Comments</p>" in t
    assert "&lt;script&gt;alert(&#39;c&#39;)&lt;/script&gt; nice" in t
    assert "<script>alert" not in t
    assert "1.5K" in t
    # title = trimmed desc
    assert "<title>already planning next halloween?" in t


def test_video_placeholder_user(client, fake):
    assert client.get(f"/@placeholder/video/{VIDEO_ID}").status_code == 200
    assert fake.calls[-1] == ("video", VIDEO_ID, None)


def test_video_bad_id(client):
    assert client.get("/@tiktok/video/12-3").status_code == 404


def test_video_not_found(client):
    r = client.get("/@tiktok/video/abc")
    assert r.status_code == 404 and "API error code 10204" in r.text


def test_photo_post_carousel(client):
    r = client.get(f"/@tatemcrae/video/{PHOTO_ID}")
    t = r.text
    assert r.status_code == 200
    assert '<section class="carousel" aria-label="carousel"' in t
    assert f'id="{PHOTO_ID}-image1"' in t and f'id="{PHOTO_ID}-image2"' in t
    assert f'href="#{PHOTO_ID}-image2">2</a>' in t
    assert "<video" not in t
    assert "Watermark</a>" not in t  # no downloads for galleries


def test_embed(client, fake):
    r = client.get(f"/embed/v2/{VIDEO_ID}")
    assert r.status_code == 200
    assert '<section class="hero is-fullheight">' not in r.text
    assert "navbar" not in r.text
    assert "<video controls autoplay" in r.text
    assert client.get("/embed/v2/abc").status_code == 404


def test_short_t_route(client, fake):
    # numeric code -> treated as a video id (PHP behaviour)
    r = client.get(f"/t/{VIDEO_ID}")
    assert r.status_code == 200 and "<video" in r.text
    # real short code -> resolved via scraper.resolve_short, then redirected
    fake.short_map["ZTabc"] = f"https://www.tiktok.com/@tiktok/video/{VIDEO_ID}"
    r = client.get("/t/ZTabc", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/@tiktok/video/{VIDEO_ID}"
    assert fake.calls[-1] == ("resolve_short", "https://www.tiktok.com/t/ZTabc")
    r = client.get("/t/ZZunknown", follow_redirects=False)
    assert r.status_code == 404
    assert client.get("/t/a-b").status_code == 400


# ---------------------------------------------------------------- tag / music / foryou / following

def test_tag_page_empty_feed(client, fake):
    r = client.get("/tag/fyp")
    assert r.status_code == 200
    t = r.text
    assert '<p class="title">fyp</p>' in t
    assert "Views: " in t and "No items sent by TikTok!" in t
    assert "Is commerce: no" in t
    assert 'href="/tag/fyp/rss"' in t
    assert '<a disabled class="button is-success"' in t
    assert 'og:image' not in t  # fyp has no profile image
    assert fake.calls[-1] == ("tag", "fyp", "0")


def test_tag_not_found(client):
    assert client.get("/tag/missing").status_code == 404


def test_tag_rss(client):
    r = client.get("/tag/fyp/rss")
    assert r.status_code == 200 and r.headers["content-type"] == "application/rss+xml"
    assert r.headers["content-disposition"] == 'attachment; filename="fyp.rss"'
    assert "<link>http://testserver/tag/fyp</link>" in r.text


def test_rss_filename_sanitized(client):
    r = client.get('/tag/a%22b%0d%0aX:%20y/rss')
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="abX_y.rss"'


def test_music_page(client, fake):
    r = client.get("/music/7123?cursor=30")
    assert r.status_code == 200
    assert "Videos: 12.3K" in r.text
    assert fake.calls[-1] == ("music", "7123", "30")
    assert '<a class="button is-danger" href="?cursor=0">First</a>' in r.text


def test_foryou(client):
    r = client.get("/foryou")
    assert r.status_code == 200
    assert r.text.count('<article class="media">') == 6
    assert 'href="/foryou/rss"' in r.text


def test_foryou_rss(client):
    r = client.get("/foryou/rss")
    assert r.status_code == 200 and r.headers["content-type"] == "application/rss+xml"
    assert r.headers["content-disposition"] == 'attachment; filename="For_you.rss"'
    assert "<description><![CDATA[For you Page]]></description>" in r.text
    assert r.text.count("<item>") == 6


def test_following(client, fake):
    r = client.get("/following?cursor=abc")
    assert r.status_code == 200
    assert '<p class="title">Discover</p>' in r.text
    assert 'class="card-footer-item">Go</a>' in r.text
    assert '<div class="card">' in r.text
    assert fake.calls[-1] == ("following", "abc")


# ---------------------------------------------------------------- settings

def test_settings_theme_cookie(client):
    r = client.post("/settings/general", data={"theme": "card"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings"
    sc = r.headers["set-cookie"]
    assert sc.startswith("theme=card;")
    assert "Max-Age=2592000" in sc and "Path=/" in sc and "HttpOnly" in sc
    assert "Secure" not in sc  # http request
    r = client.get("/settings")
    assert '<option value="card" selected>Card</option>' in r.text
    assert "Downloader" not in r.text


def test_settings_secure_cookie_on_https(fake):
    from fastapi.testclient import TestClient
    from proxitok.config import Settings
    from proxitok.main import create_app
    c = TestClient(create_app(Settings(_env_file=None), scraper=fake), base_url="https://testserver")
    r = c.post("/settings/general", data={"theme": "default"}, follow_redirects=False)
    assert "Secure" in r.headers["set-cookie"]


def test_settings_rejects_unknown_theme(client):
    r = client.post("/settings/general", data={"theme": "<evil>"}, follow_redirects=False)
    assert r.status_code == 303 and "set-cookie" not in r.headers


@pytest.mark.parametrize("path", ["/settings/api", "/settings/misc"])
def test_settings_other_posts(client, path):
    r = client.post(path, data={"api-downloader": "x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings"


def test_settings_redirect_uses_app_url(make_client):
    r = make_client(APP_URL="https://pt.example").post("/settings/general", data={"theme": "card"},
                                                       follow_redirects=False)
    assert r.headers["location"] == "https://pt.example/settings"


# ---------------------------------------------------------------- redirect/search

@pytest.mark.parametrize("type_,term,location", [
    ("user", "tiktok", "/@tiktok"),
    ("user", "@tiktok", "/@tiktok"),
    ("user", "  tiktok  ", "/@tiktok"),
    ("user", "a/b?c", "/@a%2Fb%3Fc"),
    ("tag", "#fyp", "/tag/fyp"),
    ("tag", "fyp", "/tag/fyp"),
    ("music", "7123", "/music/7123"),
    ("video", VIDEO_ID, f"/@placeholder/video/{VIDEO_ID}"),
    ("url", "https://www.tiktok.com/@tiktok", "/@tiktok"),
    ("url", f"https://www.tiktok.com/@tiktok/video/{VIDEO_ID}?lang=en", f"/@tiktok/video/{VIDEO_ID}"),
    ("url", "https://www.tiktok.com/tag/fyp", "/tag/fyp"),
    ("url", "https://www.tiktok.com/music/original-sound-7123", "/music/7123"),
    ("unknown", "x", "/"),
    ("user", "", "/"),
])
def test_redirect_search(client, type_, term, location):
    r = client.get("/redirect/search", params={"type": type_, "term": term}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == location


def test_redirect_search_no_params(client):
    r = client.get("/redirect/search", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"


def test_redirect_search_short_links(client, fake):
    fake.short_map["ZMabc"] = f"https://www.tiktok.com/@tiktok/video/{VIDEO_ID}"
    for term in ("https://vm.tiktok.com/ZMabc/", "vt.tiktok.com/ZMabc", "https://www.tiktok.com/t/ZMabc/"):
        r = client.get("/redirect/search", params={"type": "url", "term": term}, follow_redirects=False)
        assert r.status_code == 302, term
        assert r.headers["location"] == f"/@tiktok/video/{VIDEO_ID}"
    assert fake.calls[-1][0] == "resolve_short"
    # unresolvable short link -> scraper error page
    r = client.get("/redirect/search", params={"type": "url", "term": "https://vm.tiktok.com/nope"},
                   follow_redirects=False)
    assert r.status_code == 404


@pytest.mark.parametrize("term", ["https://example.com/@tiktok", "https://www.tiktok.com/explore", "hello"])
def test_redirect_search_invalid_url(client, term):
    r = client.get("/redirect/search", params={"type": "url", "term": term}, follow_redirects=False)
    assert r.status_code == 400
    assert "Invalid TikTok URL" in r.text


def test_redirect_search_app_url(make_client):
    r = make_client(APP_URL="https://pt.example").get("/redirect/search", params={"type": "user", "term": "x"},
                                                      follow_redirects=False)
    assert r.headers["location"] == "https://pt.example/@x"


# ---------------------------------------------------------------- redirect/download

def test_redirect_download(client):
    params = {"videoId": "1", "authorUsername": "u", "playAddr": "https://v16.tiktok.com/v"}
    r = client.get("/redirect/download", params=params, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/download?url=https%3A%2F%2Fv16.tiktok.com%2Fv&id=1&user=u"
    r = client.get("/redirect/download", params={**params, "watermark": "yes"}, follow_redirects=False)
    assert r.headers["location"].endswith("&watermark=1")


def test_redirect_download_incomplete(client):
    r = client.get("/redirect/download", params={"videoId": "1"}, follow_redirects=False)
    assert r.status_code == 400 and "Request incomplete" in r.text


# ---------------------------------------------------------------- lifespan

def test_lifespan_starts_and_closes_scraper(make_client, fake):
    with make_client() as c:
        assert fake.started
        assert c.get("/").status_code == 200
    assert fake.closed


# ---------------------------------------------------------------- regressions (review fixes)


def test_music_page_hides_unknown_video_count(client, fake, monkeypatch):
    """Fallback info (music/detail unavailable) has video_count=None -> no made-up "Videos: 0"."""
    from proxitok.scraper.models import Feed, MusicInfo
    from tests.fake_scraper import load

    async def music(music_id, cursor="0"):
        feed = Feed.from_tiktok(load("music_item_list"))
        return MusicInfo.from_music(feed.items[0].music), feed

    monkeypatch.setattr(fake, "music", music)
    r = client.get("/music/7123")
    assert r.status_code == 200
    assert "Videos:" not in r.text
    assert '<article class="media">' in r.text


@pytest.mark.parametrize("path", ["/", "/about", "/@tiktok", "/@tiktok/rss", "/manifest", "/tag/fyp",
                                  f"/@tiktok/video/{VIDEO_ID}", "/favicon.ico", "/styles/themes/card.css"])
def test_head_like_get(client, path):
    """Regression: HEAD was 405 everywhere (PHP's router answered HEAD like GET)."""
    g = client.get(path)
    h = client.head(path)
    assert g.status_code == 200
    assert h.status_code == 200, path
    assert h.content == b""
    assert h.headers["content-type"] == g.headers["content-type"]


def test_head_unknown_path_404(client):
    assert client.head("/nope/nope").status_code == 404


def test_post_only_route_still_405_for_get(client):
    assert client.get("/settings/general").status_code in (404, 405)
