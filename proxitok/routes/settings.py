"""/settings (theme only; the PHP "Downloader" setting was dropped with the third-party downloaders)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..templating import THEMES, theme_from_cookie
from .common import render, urls

router = APIRouter()

COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # Cookies::set -> 30 days


def _redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(urls(request).path("/settings"), status_code=303)


def set_cookie(request: Request, response: RedirectResponse, name: str, value: str) -> None:
    response.set_cookie(name, value, max_age=COOKIE_MAX_AGE, expires=COOKIE_MAX_AGE, path="/",
                        secure=request.url.scheme == "https", httponly=True, samesite="lax")


@router.get("/settings")
async def index(request: Request):
    return render(request, "views/settings.html", {
        "title": "Settings", "themes": THEMES, "current_theme": theme_from_cookie(request.cookies.get("theme")),
    })


@router.post("/settings/general")
async def general(request: Request):
    form = await request.form()
    response = _redirect(request)
    theme = form.get("theme")
    if isinstance(theme, str) and theme in THEMES:
        set_cookie(request, response, "theme", theme)
    return response


@router.post("/settings/api")
async def api(request: Request):
    # Kept for compatibility with old forms; there are no API settings any more.
    return _redirect(request)


@router.post("/settings/misc")
async def misc(request: Request):
    form = await request.form()
    response = _redirect(request)
    sw = form.get("misc-sw")
    if sw in ("yes", "no"):
        set_cookie(request, response, "misc-sw", sw)
    return response
