"""Shared helpers for route modules."""
from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, Response

from ..config import Settings
from ..scraper.api import Scraper
from ..templating import Urls


def get_scraper(request: Request) -> Scraper:
    return request.app.state.scraper


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def urls(request: Request) -> Urls:
    return Urls(get_settings(request).APP_URL)


def get_cursor(request: Request) -> str:
    """Opaque cursor string from ``?cursor=`` ("0" when absent/empty)."""
    return request.query_params.get("cursor") or "0"


def render(request: Request, name: str, context: Optional[dict[str, Any]] = None, status_code: int = 200,
           **kwargs: Any) -> Response:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code, **kwargs)


def render_error(request: Request, http_code: int, msg: str, tiktok_code: Optional[int] = None,
                 debug: Optional[Any] = None) -> Response:
    """Port of ErrorHandler::showText / showMeta. Debug info only shown when APP_DEBUG is on."""
    show_debug = debug if get_settings(request).APP_DEBUG else None
    return render(request, "views/error.html", {
        "title": "Error",
        "http_code": http_code,
        "msg": msg,
        "tiktok_code": tiktok_code,
        "response": show_debug,
    }, status_code=http_code)


_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9_.-]")


def safe_filename(name: str) -> str:
    """Keep only [A-Za-z0-9_.-] (prevents header injection / path tricks in Content-Disposition)."""
    return _UNSAFE_FILENAME.sub("", name)


def rss_response(request: Request, filename: str, context: dict[str, Any]) -> Response:
    """Misc::rss(): application/rss+xml served as an attachment."""
    settings = get_settings(request)
    ctx = dict(context)
    if not settings.APP_URL:
        # RSS readers need absolute URLs
        ctx["url_base"] = str(request.base_url).rstrip("/")
        ctx["link"] = ctx["url_base"] + ctx.get("link", "")
        if ctx.get("image"):
            ctx["image"] = ctx["url_base"] + ctx["image"]
    fname = safe_filename(filename.replace(" ", "_")) or "feed"
    return render(request, "views/rss.xml", ctx, media_type="application/rss+xml",
                  headers={"Content-Disposition": f'attachment; filename="{fname}.rss"'})


__all__ = ["HTMLResponse", "get_scraper", "get_settings", "urls", "get_cursor", "render", "render_error",
           "safe_filename", "rss_response"]
