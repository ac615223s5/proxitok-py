"""/foryou, /foryou/rss, /following"""
from __future__ import annotations

from fastapi import APIRouter, Request

from .common import get_cursor, get_scraper, render, rss_response, urls

router = APIRouter()


@router.get("/foryou")
async def foryou(request: Request):
    feed = await get_scraper(request).foryou()
    return render(request, "views/foryou.html", {"title": "For you", "feed": feed})


@router.get("/foryou/rss")
async def foryou_rss(request: Request):
    feed = await get_scraper(request).foryou()
    return rss_response(request, "For you", {
        "title": "For you", "desc": "For you Page", "image": "", "link": urls(request).path("/foryou"),
        "items": feed.items,
    })


@router.get("/following")
async def following(request: Request):
    feed = await get_scraper(request).following(get_cursor(request))
    return render(request, "views/following.html", {"title": "Following / Discover", "feed": feed})
