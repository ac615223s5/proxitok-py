"""/music/{id}"""
from __future__ import annotations

from fastapi import APIRouter, Request

from .common import get_cursor, get_scraper, render

router = APIRouter()


@router.get("/music/{music_id}")
async def music(request: Request, music_id: str):
    info, feed = await get_scraper(request).music(music_id, get_cursor(request))
    return render(request, "views/music.html", {"title": "Music", "info": info, "feed": feed})
