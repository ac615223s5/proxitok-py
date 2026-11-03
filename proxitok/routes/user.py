"""/@{username}, /@{username}/video/{id}, /@{username}/rss"""
from __future__ import annotations

import re

from fastapi import APIRouter, Request

from .common import get_cursor, get_scraper, render, render_error, rss_response, urls

router = APIRouter()

_VIDEO_ID = re.compile(r"\w+")


@router.get("/@{username}")
async def user(request: Request, username: str):
    info, feed = await get_scraper(request).user(username, get_cursor(request))
    return render(request, "views/user.html", {"title": info.detail.nickname, "info": info, "feed": feed})


async def render_video(request: Request, video_id: str, username: str | None = None, embed: bool = False):
    """UserController::video / EmbedController::v2."""
    item, info, comments = await get_scraper(request).video(video_id, username)
    if comments and not item.comments:
        item.comments = comments
    return render(request, "views/video.html", {
        "title": "Video", "item": item, "info": info, "layout": "embed" if embed else "hero",
    })


@router.get("/@{username}/video/{video_id}")
async def video(request: Request, username: str, video_id: str):
    if not _VIDEO_ID.fullmatch(video_id):
        return render_error(request, 404, "That endpoint doesn't exist")
    return await render_video(request, video_id, None if username == "placeholder" else username)


@router.get("/@{username}/rss")
async def user_rss(request: Request, username: str):
    info, feed = await get_scraper(request).user(username)
    u = urls(request)
    image = u.stream(info.detail.avatar_larger) if info.detail.avatar_larger else ""
    return rss_response(request, username, {
        "title": info.detail.nickname, "desc": info.detail.signature, "image": image,
        "link": u.user(username), "items": feed.items,
    })
