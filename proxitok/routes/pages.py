"""Static pages: home, about, verify, manifest."""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .common import render, urls

router = APIRouter()


@router.get("/")
async def home(request: Request):
    return render(request, "views/home.html", {"title": "Home"})


@router.get("/about")
async def about(request: Request):
    return render(request, "views/about.html", {"title": "About"})


@router.get("/verify")
async def verify(request: Request):
    return render(request, "views/verify.html", {"title": "verify"})


def manifest_data(prefix_urls) -> dict:
    return {
        "name": "ProxiTok",
        "short_name": "ProxiTok",
        "description": "Use TikTok with a privacy-friendly alternative frontend",
        "lang": "en-US",
        "theme_color": "#4040ff",
        "background_color": "#ffffff",
        "display": "standalone",
        "orientation": "portrait-primary",
        "icons": [
            {"src": prefix_urls.path("/android-chrome-192x192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": prefix_urls.path("/android-chrome-512x512.png"), "sizes": "512x512", "type": "image/png"},
        ],
        "start_url": prefix_urls.path("/"),
        "scope": prefix_urls.path("/"),
    }


@router.get("/manifest")
async def manifest(request: Request):
    body = json.dumps(manifest_data(urls(request)), indent=4)
    return Response(body, media_type="application/json")
