"""/redirect/search and /redirect/download (HTML form helpers, RedirectController)."""
from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .common import get_scraper, render_error, urls

router = APIRouter()

# Port of RedirectController::to_endpoint. The PHP version checked the short-link pattern first, which
# also matched "www.tiktok.com/tag/..." and "www.tiktok.com/music/..." (sending them to
# /@placeholder/video/tag). Here full URLs are parsed first and short links are only links on
# vm./vt./lite. or under /t/; those are resolved by following TikTok's redirect (Scraper.resolve_short).
_FULL = re.compile(r"^(?:https?://)?(?:(?:www|m)\.)?tiktok\.com/(.+)", re.IGNORECASE)
_USER = re.compile(r"^(@[A-Za-z0-9_.]+)(?:/|$)(.*)")
_VIDEO = re.compile(r"^(?:video|photo)/(\d+)")
_TAG = re.compile(r"^tag/([^ ]+?)(?:/|$)")
_MUSIC = re.compile(r"^music/([^ ]+?)(?:/|$)")
_SHORT = re.compile(
    r"^(?:https?://)?(?:(?:vm|vt|lite)\.tiktok\.com/(?:t/)?|(?:(?:www|m)\.)?tiktok\.com/t/)([A-Za-z0-9]+)/?(?:[?#].*)?$",
    re.IGNORECASE,
)


def _q(s: str) -> str:
    return quote(s, safe="")


def to_endpoint(url: str) -> str:
    """Map a full TikTok URL to a ProxiTok endpoint path ("" if not recognised). Short links -> ""."""
    url = (url or "").strip()
    if _SHORT.match(url):
        return ""
    m = _FULL.match(url)
    if not m:
        return ""
    rest = re.split(r"[?#]", m.group(1), maxsplit=1)[0]
    if u := _USER.match(rest):
        username = u.group(1)[1:]
        if u.group(2) == "":
            return "/@" + _q(username)
        if v := _VIDEO.match(u.group(2)):
            return f"/@{_q(username)}/video/{_q(v.group(1))}"
        return ""
    if t := _TAG.match(rest):
        return "/tag/" + _q(t.group(1))
    if mu := _MUSIC.match(rest):
        slug = mu.group(1)
        # TikTok music URLs are "/music/<title-slug>-<id>"; the scraper wants the numeric id
        digits = re.search(r"(\d+)$", slug)
        return "/music/" + _q(digits.group(1) if digits else slug)
    return ""


def is_short_link(url: str) -> bool:
    return bool(_SHORT.match((url or "").strip()))


@router.get("/redirect/search")
async def search(request: Request):
    endpoint = "/"
    qp = request.query_params
    type_, term = qp.get("type"), qp.get("term")
    if type_ is not None and term is not None:
        term = term.strip()
        if type_ == "url":
            endpoint = to_endpoint(term)
            if not endpoint and is_short_link(term):
                resolved = await get_scraper(request).resolve_short(term)
                endpoint = to_endpoint(resolved)
            if not endpoint:
                return render_error(request, 400, "Invalid TikTok URL")
        elif term:
            if type_ == "user":
                endpoint = "/@" + _q(term[1:] if term.startswith("@") else term)
            elif type_ == "tag":
                endpoint = "/tag/" + _q(term[1:] if term.startswith("#") else term)
            elif type_ == "music":
                endpoint = "/music/" + _q(term)
            elif type_ == "video":
                # The @username part is not used, but it is the schema that TikTok follows
                endpoint = "/@placeholder/video/" + _q(term)
    return RedirectResponse(urls(request).path(endpoint), status_code=302)


@router.get("/redirect/download")
async def download(request: Request):
    qp = request.query_params
    video_id, username, play_addr = qp.get("videoId"), qp.get("authorUsername"), qp.get("playAddr")
    if video_id is None or username is None or play_addr is None:
        return render_error(request, 400, "Request incomplete")
    watermark = qp.get("watermark") == "yes"
    # Third-party "no watermark" downloaders were dropped: both variants proxy playAddr.
    return RedirectResponse(urls(request).download(play_addr, username, video_id, watermark), status_code=302)
