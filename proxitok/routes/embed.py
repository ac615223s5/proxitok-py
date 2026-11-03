"""/embed/v2/{id}"""
from __future__ import annotations

from fastapi import APIRouter, Request

from .common import render_error
from .user import render_video

router = APIRouter()


@router.get("/embed/v2/{video_id}")
async def embed_v2(request: Request, video_id: str):
    if not video_id.isdigit():
        return render_error(request, 404, "That endpoint doesn't exist")
    return await render_video(request, video_id, embed=True)
