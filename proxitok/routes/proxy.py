"""/stream and /download: media proxy for TikTok CDN URLs (ProxyController).

Security:
  * Only http(s) URLs on TikTok's *media* hosts are fetched (no userinfo, no non-standard ports): any
    subdomain of MEDIA_CDN_DOMAINS, and on tiktok.com / tiktokv.com only video hosts whose first label
    looks like ``v16-webapp-prime`` / ``v19`` / ``v16m`` (never www.tiktok.com or the API hosts).
  * Redirects are followed manually (max MAX_REDIRECTS) and every hop is re-validated, so an allowed host
    can't bounce the proxy to an internal / foreign address (SSRF).
  * Successful responses must be media (video/*, audio/*, raster image/*, or octet-stream); anything else
    (HTML, JSON, SVG, ...) is refused, upstream error bodies are never relayed, and every response carries
    ``X-Content-Type-Options: nosniff`` and a sandboxing CSP, so nothing proxied can run script on this
    instance's origin.
"""
from __future__ import annotations

import logging
import re
from typing import AsyncIterator, Optional
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from .common import get_scraper, safe_filename

log = logging.getLogger(__name__)

router = APIRouter()

# Any subdomain of these is a media CDN (images, video, audio).
MEDIA_CDN_DOMAINS: tuple[str, ...] = ("tiktokcdn.com", "tiktokcdn-us.com", "ibyteimg.com", "muscdn.com")
# On these only video hosts like v16-webapp-prime.us.tiktok.com / v19-webapp.tiktok.com / v16m.tiktokv.com.
VIDEO_HOST_DOMAINS: tuple[str, ...] = ("tiktok.com", "tiktokv.com")
_VIDEO_LABEL_RE = re.compile(r"^v\d+[a-z0-9-]*$")
# Kept for reference / backwards compatibility (all registrable domains the proxy may talk to).
VALID_TIKTOK_DOMAINS: tuple[str, ...] = MEDIA_CDN_DOMAINS + VIDEO_HOST_DOMAINS

MEDIA_TYPE_PREFIXES = ("video/", "audio/", "image/")
BLOCKED_MEDIA_TYPES = ("image/svg+xml",)
OCTET_TYPES = ("application/octet-stream", "binary/octet-stream")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
}
MAX_REDIRECTS = 5
REDIRECT_CODES = {301, 302, 303, 307, 308}
PASSTHROUGH_HEADERS = (
    "content-type", "content-length", "content-range", "accept-ranges", "content-encoding",
    "cache-control", "last-modified", "etag",
)


def _text(msg: str, status: int) -> Response:
    return PlainTextResponse(msg, status_code=status, headers=SECURITY_HEADERS)


def _host_allowed(host: Optional[str]) -> bool:
    if not host:
        return False
    host = host.lower().rstrip(".")
    if any(host.endswith("." + d) for d in MEDIA_CDN_DOMAINS):
        return True
    for d in VIDEO_HOST_DOMAINS:
        if host.endswith("." + d):
            return bool(_VIDEO_LABEL_RE.match(host.split(".", 1)[0]))
    return False


def media_type_allowed(content_type: Optional[str]) -> bool:
    """True if an upstream Content-Type may be relayed (media only, no SVG/HTML/JSON/...)."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if not ct or ct in BLOCKED_MEDIA_TYPES:
        return False
    return ct.startswith(MEDIA_TYPE_PREFIXES) or ct in OCTET_TYPES


def is_valid_url(url: Optional[str]) -> bool:
    """True if ``url`` may be fetched by the proxy."""
    if not url or not isinstance(url, str):
        return False
    if any(c in url for c in "\\\r\n\t ") or any(ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https"):
        return False
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        return False
    if port is not None and port not in (80, 443):
        return False
    if not _host_allowed(parts.hostname):
        return False
    # Defence in depth: the HTTP client must parse the same host.
    try:
        hx = httpx.URL(url)
    except Exception:
        return False
    return hx.scheme in ("http", "https") and _host_allowed(hx.host) and not hx.userinfo


class ProxyError(Exception):
    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status = status
        self.msg = msg


async def open_upstream(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> httpx.Response:
    """GET ``url`` (streaming), following redirects manually with the allowlist checked on each hop."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not is_valid_url(current):
            raise ProxyError(400, "Not a valid URL")
        req = client.build_request("GET", current, headers=headers)
        try:
            resp = await client.send(req, stream=True, follow_redirects=False)
        except httpx.HTTPError as e:
            log.info("proxy upstream error for %s: %r", current, e)
            raise ProxyError(502, "Upstream request failed") from e
        if resp.status_code in REDIRECT_CODES and resp.headers.get("location"):
            nxt = urljoin(current, resp.headers["location"])
            await resp.aclose()
            current = nxt
            continue
        return resp
    raise ProxyError(502, "Too many redirects")


async def _body(resp: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in resp.aiter_raw():
            yield chunk
    finally:
        await resp.aclose()


class UpstreamResponse(StreamingResponse):
    """StreamingResponse that always closes the upstream httpx response, also when the client disconnects
    mid-stream (Starlette skips background tasks in that case)."""

    def __init__(self, upstream: httpx.Response, headers: dict[str, str]) -> None:
        super().__init__(_body(upstream), status_code=upstream.status_code, headers=headers)
        self.upstream = upstream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.upstream.aclose()


async def proxy(request: Request, url: Optional[str], extra_headers: Optional[dict[str, str]] = None) -> Response:
    if not url:
        return _text("You need to send a URL", 400)
    if not is_valid_url(url):
        return _text("Not a valid URL", 400)
    scraper = get_scraper(request)
    headers = scraper.stream_headers(request.headers.get("range"))
    try:
        resp = await open_upstream(scraper.stream_client(), url, headers)
    except ProxyError as e:
        return _text(e.msg, e.status)
    if not 200 <= resp.status_code < 300:
        # Never relay upstream error bodies (they may be HTML); keep the status for the client.
        status = resp.status_code
        await resp.aclose()
        return _text(f"Upstream returned HTTP {status}", status if 400 <= status < 600 else 502)
    if not media_type_allowed(resp.headers.get("content-type")):
        ct = resp.headers.get("content-type", "")
        await resp.aclose()
        log.info("proxy refused non-media content-type %r for %s", ct, url)
        return _text("Upstream did not return media", 502)
    out_headers = {k: v for k in PASSTHROUGH_HEADERS if (v := resp.headers.get(k)) is not None}
    out_headers.update(SECURITY_HEADERS)
    if extra_headers:
        out_headers.update(extra_headers)
    if request.scope.get("proxitok.head"):
        await resp.aclose()
        return Response(status_code=resp.status_code, headers=out_headers)
    return UpstreamResponse(resp, out_headers)


@router.get("/stream")
async def stream(request: Request):
    return await proxy(request, request.query_params.get("url"))


def download_filename(video_id: str, user: str) -> str:
    return safe_filename(f"tiktok-video-{video_id}-{user}") + ".mp4"


@router.get("/download")
async def download(request: Request):
    qp = request.query_params
    filename = download_filename(qp.get("id", ""), qp.get("user", ""))
    return await proxy(request, qp.get("url"), {"Content-Disposition": f'attachment; filename="{filename}"'})
