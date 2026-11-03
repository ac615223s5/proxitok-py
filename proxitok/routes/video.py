"""/t/{code}: TikTok short-link workaround (VideoController)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .common import get_scraper, render_error, urls
from .redirect import to_endpoint
from .user import render_video

router = APIRouter()


@router.get("/t/{code}")
async def short(request: Request, code: str):
    # Numeric -> treat as a video id like the PHP version did; otherwise it's a real
    # www.tiktok.com/t/<code> short link that has to be resolved via redirect.
    if code.isdigit():
        return await render_video(request, code)
    if not code.isalnum():
        return render_error(request, 400, "Invalid TikTok short link")
    resolved = await get_scraper(request).resolve_short(f"https://www.tiktok.com/t/{code}")
    endpoint = to_endpoint(resolved)
    if not endpoint:
        return render_error(request, 404, "Short link did not resolve to a TikTok video")
    return RedirectResponse(urls(request).path(endpoint), status_code=302)
