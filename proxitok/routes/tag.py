"""/tag/{name}, /tag/{name}/rss"""
from __future__ import annotations

from fastapi import APIRouter, Request

from .common import get_cursor, get_scraper, render, rss_response, urls

router = APIRouter()


@router.get("/tag/{name}")
async def tag(request: Request, name: str):
    info, feed = await get_scraper(request).tag(name, get_cursor(request))
    return render(request, "views/tag.html", {"title": info.detail.title or name, "info": info, "feed": feed})


@router.get("/tag/{name}/rss")
async def tag_rss(request: Request, name: str):
    info, feed = await get_scraper(request).tag(name)
    return rss_response(request, name, {
        "title": name, "desc": info.detail.desc, "image": "", "link": urls(request).tag(name), "items": feed.items,
    })
