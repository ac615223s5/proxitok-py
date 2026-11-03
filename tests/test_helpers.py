"""Unit tests for templating helpers, URL builders, to_endpoint and the proxy allowlist."""
import pytest

from proxitok.routes.common import safe_filename
from proxitok.routes.proxy import download_filename, is_valid_url
from proxitok.routes.redirect import is_short_link, to_endpoint
from proxitok.scraper.models import TextExtra
from proxitok.templating import (
    Urls, bool_to_str, number, php_date, render_desc, safe_link, theme_from_cookie,
)


# ---------------------------------------------------------------- number()

@pytest.mark.parametrize("value,expected", [
    (0, "0"), (999, "999"), (1000, "1000"), (1001, "1K"), (1234, "1.2K"), (1999, "1.9K"),
    (12_345, "12.3K"), (999_999, "999.9K"), (1_000_000, "1M"), (1_500_000, "1.5M"),
    (96_555_924, "96.5M"), (466_847_641, "466.8M"), (2_000_000_000, "2B"), (3_400_000_000_000, "3.4T"),
    (1000.5, "1K"), (None, "0"), ("2500", "2.5K"), (12.5, "12.5"),
])
def test_number(value, expected):
    assert number(value) == expected


def test_bool_to_str():
    assert bool_to_str(True) == "yes" and bool_to_str(False) == "no" and bool_to_str(0) == "no"


def test_theme_from_cookie():
    assert theme_from_cookie("card") == "card"
    assert theme_from_cookie("default") == "default"
    assert theme_from_cookie("../../etc/passwd") == "default"
    assert theme_from_cookie(None) == "default"


# ---------------------------------------------------------------- dates

def test_php_date_formats():
    ts = 1793482137  # 2026-10-31 21:28:57 UTC
    assert php_date("M d, Y", ts) == "Oct 31, 2026"
    assert php_date("M d, Y H:i:s e", ts) == "Oct 31, 2026 21:28:57 UTC"
    assert php_date("r", ts) == "Sat, 31 Oct 2026 21:28:57 +0000"
    assert php_date("M d, Y", "5") == "Jan 01, 1970"
    assert php_date("M d, Y", None) == "Jan 01, 1970"


# ---------------------------------------------------------------- UrlBuilder

def test_urls_root_relative():
    u = Urls("")
    assert u.path("/x") == "/x"
    assert u.user("tiktok") == "/@tiktok"
    assert u.tag("fyp") == "/tag/fyp"
    assert u.video_internal("tiktok", "123") == "/@tiktok/video/123"
    assert Urls.video_external("tiktok", "123") == "https://www.tiktok.com/@tiktok/video/123"
    assert u.stream("https://a.tiktokcdn.com/v?x=1&y=2") == "/stream?url=https%3A%2F%2Fa.tiktokcdn.com%2Fv%3Fx%3D1%26y%3D2"
    assert u.download("https://a.tiktok.com/v", "us er", "1", True) == \
        "/download?url=https%3A%2F%2Fa.tiktok.com%2Fv&id=1&user=us%20er&watermark=1"
    assert u.download("u", "x", "1", False).endswith("&user=x")
    assert u.assets("css", "bulma.min.css", True) == "/styles/vendor/bulma.min.css"
    assert u.assets("js", "navbar.js") == "/scripts/navbar.js"
    with pytest.raises(ValueError):
        u.assets("img", "x.png")


def test_urls_with_app_url_and_escaping():
    u = Urls("https://pt.example")
    assert u.user("tiktok") == "https://pt.example/@tiktok"
    assert u.tag("a/b?c") == "https://pt.example/tag/a%2Fb%3Fc"


def test_safe_link():
    assert safe_link("tiktok.com/discovermore") == "https://tiktok.com/discovermore"
    assert safe_link("https://x.com") == "https://x.com"
    assert not safe_link("javascript:alert(1)").startswith("javascript")
    assert safe_link("") == ""


# ---------------------------------------------------------------- render_desc

def _te(desc, needle, type_, **kw):
    """TextExtra covering ``needle`` in desc with UTF-16 offsets."""
    idx = desc.index(needle)
    start = len(desc[:idx].encode("utf-16-le")) // 2
    end = start + len(needle.encode("utf-16-le")) // 2
    return TextExtra(type=type_, start=start, end=end, **kw)


def test_render_desc_links_with_utf16_offsets():
    desc = "ready? 👀🎃 notes from @Mason Smith #diy #diyfun"
    extras = [
        _te(desc, "@Mason Smith", TextExtra.USER, user_unique_id="dadsocial"),
        _te(desc, "#diy ", TextExtra.HASHTAG, hashtag_name="diy"),
        _te(desc, "#diyfun", TextExtra.HASHTAG, hashtag_name="diyfun"),
    ]
    extras[1].end -= 1  # exclude the trailing space
    out = str(render_desc(desc, extras))
    assert '<a href="/@dadsocial">@Mason Smith</a>' in out
    assert '<a href="/tag/diy">#diy</a> ' in out
    assert '<a href="/tag/diyfun">#diyfun</a>' in out
    assert "👀🎃" in out


def test_render_desc_escapes_html():
    desc = '<script>alert(1)</script> "x" & #tag'
    out = str(render_desc(desc, [_te(desc, "#tag", TextExtra.HASHTAG, hashtag_name="tag")]))
    assert "<script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert "&#34;x&#34; &amp;" in out
    assert '<a href="/tag/tag">#tag</a>' in out


def test_render_desc_malicious_extras():
    # hashtag name with quotes / html must not break out of the attribute
    desc = "hi #x"
    out = str(render_desc(desc, [_te(desc, "#x", TextExtra.HASHTAG, hashtag_name='"><img src=x onerror=alert(1)>')]))
    assert "<img" not in out
    assert 'href="/tag/%22%3E%3Cimg' in out


def test_render_desc_regex_metachars_fallback():
    # offsets unusable -> token search; regex metacharacters in the name must be literal (re.escape)
    desc = "look #a.b and #axb"
    out = str(render_desc(desc, [TextExtra(type=TextExtra.HASHTAG, start=0, end=0, hashtag_name="a.b")]))
    assert out.count("<a ") == 1
    assert '<a href="/tag/a.b">#a.b</a>' in out
    assert "#axb" in out and 'href="/tag/axb"' not in out
    # word boundary: #hi must not match inside #hii
    out = str(render_desc("#hii #hi", [TextExtra(type=TextExtra.HASHTAG, hashtag_name="hi")]))
    assert out == '#hii <a href="/tag/hi">#hi</a>'
    # garbage pattern chars don't raise
    assert str(render_desc("a (b", [TextExtra(type=TextExtra.HASHTAG, hashtag_name="(")])) == "a (b"


def test_render_desc_empty_and_overlaps():
    assert str(render_desc("", [])) == ""
    assert str(render_desc(None)) == ""
    desc = "#abc"
    e1 = _te(desc, "#abc", TextExtra.HASHTAG, hashtag_name="abc")
    out = str(render_desc(desc, [e1, e1]))
    assert out.count("<a ") == 1
    # unknown type / missing target is ignored
    assert str(render_desc("x", [TextExtra(type=5, start=0, end=1)])) == "x"


# ---------------------------------------------------------------- to_endpoint

@pytest.mark.parametrize("url,expected", [
    ("https://www.tiktok.com/@tiktok", "/@tiktok"),
    ("https://www.tiktok.com/@tiktok/", "/@tiktok"),
    ("https://www.tiktok.com/@tiktok?lang=en", "/@tiktok"),
    ("https://www.tiktok.com/@some.user_1/video/7702947070604332319", "/@some.user_1/video/7702947070604332319"),
    ("https://www.tiktok.com/@tiktok/video/7702947070604332319?is_from_webapp=1", "/@tiktok/video/7702947070604332319"),
    ("https://www.tiktok.com/@tatemcrae/photo/7702926850120600845", "/@tatemcrae/video/7702926850120600845"),
    ("tiktok.com/@tiktok", "/@tiktok"),
    ("https://m.tiktok.com/@tiktok", "/@tiktok"),
    ("https://www.tiktok.com/tag/fyp", "/tag/fyp"),
    ("https://www.tiktok.com/tag/fyp/", "/tag/fyp"),
    ("https://www.tiktok.com/music/original-sound-7123456789", "/music/7123456789"),
    ("https://www.tiktok.com/music/7123456789", "/music/7123456789"),
    ("https://www.tiktok.com/@tiktok/live", ""),
    ("https://www.tiktok.com/explore", ""),
    ("https://example.com/@tiktok", ""),
    ("https://www.tiktok.com.evil.com/@tiktok", ""),
    ("not a url", ""),
    ("https://vm.tiktok.com/ZMabc123/", ""),  # short links are resolved separately
])
def test_to_endpoint(url, expected):
    assert to_endpoint(url) == expected


@pytest.mark.parametrize("url,expected", [
    ("https://vm.tiktok.com/ZMabc123/", True),
    ("vm.tiktok.com/ZMabc123", True),
    ("https://vt.tiktok.com/ZSabc/", True),
    ("https://www.tiktok.com/t/ZTabc123/", True),
    ("https://lite.tiktok.com/t/ZTabc123", True),
    ("https://www.tiktok.com/tag/fyp", False),
    ("https://www.tiktok.com/@tiktok", False),
    ("https://vm.tiktok.com.evil.com/ZMabc", False),
    ("https://vm.tiktok.com/ZM<script>", False),
])
def test_is_short_link(url, expected):
    assert is_short_link(url) is expected


# ---------------------------------------------------------------- proxy allowlist

@pytest.mark.parametrize("url", [
    "https://v16-webapp-prime.us.tiktok.com/video/tos/x/?a=1",
    "https://p16-sign.tiktokcdn-us.com/a.jpeg",
    "https://p16-sign-va.tiktokcdn.com/a.jpeg",
    "https://v19-webapp.tiktok.com/x",
    "https://v16m.tiktokv.com/x",
    "http://v16m.tiktokcdn-us.com/x",
    "https://sf16.ibyteimg.com/x.png",
    "http://p16.muscdn.com/img/musically-maliva-obj/1.jpeg",
    "https://P16.TikTokCDN.com/x",
    "https://p16.tiktokcdn.com./x",
    "https://p16.tiktokcdn.com:443/x",
])
def test_valid_urls(url):
    assert is_valid_url(url)


@pytest.mark.parametrize("url", [
    None, "", "not a url",
    "https://example.com/x",
    "https://eviltiktok.com/x",
    "https://tiktok.com.evil.com/x",
    # web app / API hosts are not media hosts (would serve TikTok's HTML+JS on our origin)
    "https://www.tiktok.com/@tiktok",
    "https://tiktok.com/x",
    "https://m.tiktok.com/x",
    "https://api16.tiktokv.com/x",
    "https://webcast.us.tiktok.com/x",
    "https://tiktokcdn.com/x",
    "https://tiktokcdn.com.attacker.net/x",
    "https://nottiktokcdn-us.com/x",
    "ftp://p16.tiktokcdn.com/x",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "//p16.tiktokcdn.com/x",
    "https://p16.tiktokcdn.com@evil.com/x",
    "https://user:pass@p16.tiktokcdn.com/x",
    "https://evil.com\\@p16.tiktokcdn.com/x",
    "https://evil.com#@p16.tiktokcdn.com/x",
    "https://p16.tiktokcdn.com:8080/x",
    "https://127.0.0.1/x",
    "http://169.254.169.254/latest/meta-data",
    "https://p16.tiktokcdn.com\r\nX-Injected: 1/x",
    "https://p16.tiktokcdn.com:99999/x",
])
def test_invalid_urls(url):
    assert not is_valid_url(url)


def test_download_filename_sanitized():
    assert download_filename("123", "tiktok") == "tiktok-video-123-tiktok.mp4"
    assert download_filename('1"; x=\r\n', "../../ev il") == "tiktok-video-1x-....evil.mp4"
    assert safe_filename("a b/c\\d\"e") == "abcde"
