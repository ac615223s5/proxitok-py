"""FastAPI application factory. Run with: ``uv run uvicorn proxitok.main:app``."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .config import Settings, get_settings
from .routes import embed, foryou, music, pages, proxy, redirect, settings as settings_routes, tag, user, video
from .routes.common import render_error
from .scraper.api import Scraper
from .scraper.errors import ScraperError
from .templating import BASE_DIR, create_templates

log = logging.getLogger("proxitok")

STATIC_DIR = BASE_DIR / "static"
ROOT_FILES = (
    "favicon.ico", "favicon-16x16.png", "favicon-32x32.png", "apple-touch-icon.png",
    "android-chrome-192x192.png", "android-chrome-512x512.png", "robots.txt", "embed.js",
)


class HeadAsGetMiddleware:
    """Answer HEAD like GET without a body (bramus/router in the PHP app did the same).

    The request is routed as GET on a *copy* of the scope (the server keeps seeing HEAD, so it also
    knows not to send a body); body chunks are dropped here and ``scope["proxitok.head"]`` lets the media
    proxy skip streaming the upstream file. Headers (Content-Type, Content-Length, ...) are kept.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "HEAD":
            await self.app(scope, receive, send)
            return
        get_scope = dict(scope, method="GET")
        get_scope["proxitok.head"] = True

        async def send_headers_only(message: Message) -> None:
            if message["type"] == "http.response.body":
                if message.get("more_body", False):
                    return
                message = {"type": "http.response.body", "body": b"", "more_body": False}
            await send(message)

        await self.app(get_scope, receive, send_headers_only)


def create_app(settings: Optional[Settings] = None, scraper: Optional[Scraper] = None) -> FastAPI:
    """Build the app. ``scraper`` may be injected (tests); otherwise a real Scraper is created.

    The scraper is attached to ``app.state.scraper`` immediately and started/closed by the lifespan.
    """
    settings = settings or get_settings()
    scraper = scraper if scraper is not None else Scraper(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await scraper.start()
        try:
            yield
        finally:
            await scraper.close()

    app = FastAPI(title="ProxiTok", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None, debug=settings.APP_DEBUG)
    app.add_middleware(HeadAsGetMiddleware)
    app.state.settings = settings
    app.state.scraper = scraper
    app.state.templates = create_templates(settings)

    # -- static -- #
    app.mount("/styles", StaticFiles(directory=STATIC_DIR / "styles"), name="styles")
    app.mount("/scripts", StaticFiles(directory=STATIC_DIR / "scripts"), name="scripts")

    def _root_file(name: str):
        path = STATIC_DIR / name

        async def handler():
            return FileResponse(path)

        handler.__name__ = "static_" + name.replace(".", "_").replace("-", "_")
        return handler

    for name in ROOT_FILES:
        app.add_api_route("/" + name, _root_file(name), methods=["GET"], include_in_schema=False)

    # -- routes -- #
    for module in (pages, proxy, redirect, user, video, tag, music, foryou, settings_routes, embed):
        app.include_router(module.router)

    # -- errors -- #
    @app.exception_handler(ScraperError)
    async def scraper_error(request: Request, exc: ScraperError):
        log.info("ScraperError on %s: %s", request.url.path, exc)
        return render_error(request, exc.http_code, exc.msg, exc.tiktok_code, exc.debug)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return render_error(request, 404, "That endpoint doesn't exist")
        if exc.status_code == 405:
            return render_error(request, 405, "Method not allowed")
        return render_error(request, exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return render_error(request, 400, "Invalid request")

    return app


app = create_app()
