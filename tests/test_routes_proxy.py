"""/stream and /download proxy: allowlist, redirects, Range passthrough, headers."""
import httpx
import pytest

from tests.fake_scraper import client, fake, make_client  # noqa: F401

CDN = "https://v16-webapp-prime.us.tiktok.com/video/tos/abc/?a=1988&mime_type=video_mp4"


def test_stream_missing_url(client):
    r = client.get("/stream")
    assert r.status_code == 400 and "You need to send a URL" in r.text


@pytest.mark.parametrize("url", [
    "https://example.com/x", "https://eviltiktok.com/x", "https://tiktok.com.evil.com/x",
    "http://169.254.169.254/latest", "https://p16.tiktokcdn.com@evil.com/x", "file:///etc/passwd",
    "https://evil.com\\@p16.tiktokcdn.com/x",
])
@pytest.mark.parametrize("endpoint", ["/stream", "/download"])
def test_rejects_foreign_hosts(client, fake, url, endpoint):
    r = client.get(endpoint, params={"url": url})
    assert r.status_code == 400 and r.text == "Not a valid URL"
    assert fake.stream_requests == []


def test_stream_passthrough(client, fake):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 100, headers={
            "content-type": "video/mp4", "accept-ranges": "bytes", "set-cookie": "leak=1", "server": "tt",
        })
    fake.handler = handler
    r = client.get("/stream", params={"url": CDN})
    assert r.status_code == 200
    assert r.content == b"x" * 100
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-length"] == "100"
    assert "set-cookie" not in r.headers and "server" not in r.headers
    req = fake.stream_requests[0]
    assert str(req.url) == CDN
    assert req.headers["referer"] == "https://www.tiktok.com/"
    assert req.headers["user-agent"] == "test-agent"
    assert "range" not in req.headers


def test_stream_range(client, fake):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["range"] == "bytes=0-1023"
        return httpx.Response(206, content=b"y" * 1024, headers={
            "content-type": "video/mp4", "content-range": "bytes 0-1023/5000", "accept-ranges": "bytes",
        })
    fake.handler = handler
    r = client.get("/stream", params={"url": CDN}, headers={"Range": "bytes=0-1023"})
    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 0-1023/5000"
    assert r.headers["content-length"] == "1024"
    assert len(r.content) == 1024


def test_stream_upstream_status_passthrough(client, fake):
    fake.handler = lambda req: httpx.Response(403, content=b"denied", headers={"content-type": "text/plain"})
    r = client.get("/stream", params={"url": CDN})
    assert r.status_code == 403


def test_follows_allowed_redirects(client, fake):
    hops = []

    def handler(req: httpx.Request) -> httpx.Response:
        hops.append(req.url.host)
        if req.url.host == "v16-webapp-prime.us.tiktok.com":
            return httpx.Response(302, headers={"location": "https://v16m.tiktokcdn-us.com/final.mp4"})
        if req.url.path == "/final.mp4":
            return httpx.Response(301, headers={"location": "/final2.mp4"})  # relative hop
        return httpx.Response(200, content=b"ok", headers={"content-type": "video/mp4"})

    fake.handler = handler
    r = client.get("/stream", params={"url": CDN})
    assert r.status_code == 200 and r.content == b"ok"
    assert hops == ["v16-webapp-prime.us.tiktok.com", "v16m.tiktokcdn-us.com", "v16m.tiktokcdn-us.com"]


@pytest.mark.parametrize("location", [
    "https://evil.com/steal", "http://127.0.0.1:8080/admin", "https://eviltiktok.com/x",
    "https://p16.tiktokcdn.com@evil.com/", "file:///etc/passwd", "//evil.com/x",
])
def test_rejects_redirect_to_foreign_host(client, fake, location):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host != "v16-webapp-prime.us.tiktok.com":
            pytest.fail(f"proxy followed redirect to {req.url}")
        return httpx.Response(302, headers={"location": location})

    fake.handler = handler
    r = client.get("/stream", params={"url": CDN})
    assert r.status_code == 400 and r.text == "Not a valid URL"
    assert len(fake.stream_requests) == 1


def test_redirect_loop_limit(client, fake):
    fake.handler = lambda req: httpx.Response(302, headers={"location": CDN})
    r = client.get("/stream", params={"url": CDN})
    assert r.status_code == 502 and "Too many redirects" in r.text
    assert len(fake.stream_requests) == 6  # initial + 5 redirects


def test_upstream_network_error(client, fake):
    def handler(req):
        raise httpx.ConnectError("boom", request=req)
    fake.handler = handler
    r = client.get("/stream", params={"url": CDN})
    assert r.status_code == 502


def test_download_headers(client, fake):
    fake.handler = lambda req: httpx.Response(200, content=b"mp4", headers={"content-type": "video/mp4"})
    r = client.get("/download", params={"url": CDN, "id": "7702947070604332319", "user": "tiktok", "watermark": "1"})
    assert r.status_code == 200 and r.content == b"mp4"
    assert r.headers["content-disposition"] == 'attachment; filename="tiktok-video-7702947070604332319-tiktok.mp4"'


def test_download_filename_injection(client, fake):
    fake.handler = lambda req: httpx.Response(200, content=b"mp4", headers={"content-type": "video/mp4"})
    r = client.get("/download", params={"url": CDN, "id": '1"\r\nX-Evil: 1', "user": "../a b;c"})
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="tiktok-video-1X-Evil1-..abc.mp4"'
    assert "x-evil" not in r.headers


def test_download_missing_url(client):
    assert client.get("/download").status_code == 400


def test_upstream_closed_after_stream(client, fake):
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a"
            yield b"b"

        async def aclose(self):
            closed.append(True)

    fake.handler = lambda req: httpx.Response(200, stream=Stream(), headers={"content-type": "video/mp4"})
    r = client.get("/stream", params={"url": CDN})
    assert r.content == b"ab"
    assert closed


async def test_upstream_closed_on_client_disconnect(fake):
    """Client goes away mid-body (send raises OSError) -> upstream response must still be closed."""
    from proxitok.config import Settings
    from proxitok.main import create_app

    closed = []

    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b"z" * 1024

        async def aclose(self):
            closed.append(True)

    fake.handler = lambda req: httpx.Response(200, stream=Endless(), headers={"content-type": "video/mp4"})
    app = create_app(Settings(_env_file=None), scraper=fake)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message["type"])
        if message["type"] == "http.response.body" and len(sent) > 2:
            raise OSError("client disconnected")

    from urllib.parse import urlencode
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/stream", "raw_path": b"/stream",
        "query_string": urlencode({"url": CDN}).encode(), "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1), "server": ("testserver", 80), "root_path": "", "state": {},
    }
    try:
        await app(scope, receive, send)
    except Exception:
        pass
    assert closed, "upstream stream was not closed after client disconnect"


# ---------------------------------------------------------------- content-type / origin isolation


@pytest.mark.parametrize("url", [
    "https://www.tiktok.com/@tiktok", "https://tiktok.com/", "https://m.tiktok.com/x",
    "https://api16.tiktokv.com/aweme/v1/feed/", "https://webcast.us.tiktok.com/x",
])
def test_rejects_web_app_and_api_hosts(client, fake, url):
    """Regression: /stream served TikTok's web app (HTML+JS) on the instance origin."""
    r = client.get("/stream", params={"url": url})
    assert r.status_code == 400 and r.text == "Not a valid URL"
    assert fake.stream_requests == []


@pytest.mark.parametrize("ctype", ["text/html; charset=utf-8", "application/json", "image/svg+xml",
                                   "text/xml", "application/javascript", None])
@pytest.mark.parametrize("endpoint", ["/stream", "/download"])
def test_refuses_non_media_content(client, fake, ctype, endpoint):
    headers = {"content-type": ctype} if ctype else {}
    fake.handler = lambda req: httpx.Response(200, content=b"<script>alert(1)</script>", headers=headers)
    r = client.get(endpoint, params={"url": CDN})
    assert r.status_code == 502
    assert b"<script>" not in r.content
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("ctype", ["video/mp4", "audio/mpeg", "image/jpeg", "image/webp",
                                   "application/octet-stream"])
def test_media_content_has_security_headers(client, fake, ctype):
    fake.handler = lambda req: httpx.Response(200, content=b"data", headers={"content-type": ctype})
    r = client.get("/stream", params={"url": CDN})
    assert r.status_code == 200 and r.content == b"data"
    assert r.headers["content-type"] == ctype
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "sandbox; default-src 'none'"


def test_upstream_error_body_not_relayed(client, fake):
    fake.handler = lambda req: httpx.Response(403, content=b"<html><script>x()</script></html>",
                                              headers={"content-type": "text/html"})
    r = client.get("/stream", params={"url": CDN})
    assert r.status_code == 403
    assert b"<script>" not in r.content and r.headers["content-type"].startswith("text/plain")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_head_stream_does_not_stream_body(client, fake):
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("HEAD must not read the upstream body")
            yield b""

        async def aclose(self):
            closed.append(True)

    fake.handler = lambda req: httpx.Response(200, stream=Stream(), headers={
        "content-type": "video/mp4", "content-length": "7086434", "accept-ranges": "bytes"})
    r = client.head("/stream", params={"url": CDN})
    assert r.status_code == 200 and r.content == b""
    assert r.headers["content-length"] == "7086434"
    assert r.headers["content-type"] == "video/mp4"
    assert fake.stream_requests[0].method == "GET"
    assert closed
