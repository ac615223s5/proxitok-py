"""In-memory Scraper double built from the live-captured fixtures (implements proxitok.scraper.api.Scraper)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

import httpx

from proxitok.scraper.errors import ScraperError
from proxitok.scraper.models import (
    Comment, CommentUser, Feed, Item, MusicInfo, TagInfo, UserFeed, UserInfo,
)

FIX = Path(__file__).parent / "fixtures"

PHOTO_ID = "7702926850120600845"
VIDEO_ID = "7702947070604332319"


def load(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def _video_scope(name: str) -> dict:
    return load(name)["webapp.video-detail"]


def default_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"\x00" * 16, headers={"content-type": "video/mp4"})


class FakeScraper:
    def __init__(self, handler: Optional[Callable[[httpx.Request], httpx.Response]] = None) -> None:
        self.calls: list[tuple] = []
        self.started = False
        self.closed = False
        self.handler = handler or default_handler
        self.stream_requests: list[httpx.Request] = []
        self.error: Optional[ScraperError] = None  # raise this from every data method when set
        self.short_map: dict[str, str] = {}
        self._client: Optional[httpx.AsyncClient] = None

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _check(self, *call) -> None:
        self.calls.append(call)
        if self.error is not None:
            raise self.error

    # ---------------------------------------------------------------- data
    async def user(self, username: str, cursor: str = "0"):
        self._check("user", username, cursor)
        if username == "missing":
            raise ScraperError.from_tiktok_code(10221)
        info = UserInfo.from_tiktok(load("user_tiktok")["webapp.user-detail"])
        return info, Feed.from_creator_list(load("creator_item_list"))

    async def video(self, video_id: str, username: Optional[str] = None):
        self._check("video", video_id, username)
        if video_id == PHOTO_ID:
            scope = _video_scope("photo_post")
        elif video_id == VIDEO_ID or video_id.isdigit():
            scope = _video_scope("video_tiktok")
        else:
            raise ScraperError.from_tiktok_code(10204)
        struct = scope["itemInfo"]["itemStruct"]
        item = Item.from_tiktok(struct)
        info = UserInfo.from_author(struct.get("author"), struct.get("authorStats"), scope.get("shareMeta"),
                                    struct.get("authorStatsV2"))
        comments = [
            Comment(cid="1", text="<script>alert('c')</script> nice", create_time=1793482137, digg_count=1500,
                    user=CommentUser(unique_id="commenter", nickname="Commenter", avatar_thumb="https://p16-sign.tiktokcdn-us.com/a.jpg")),
        ]
        item.comments = comments
        return item, info, comments

    async def tag(self, name: str, cursor: str = "0"):
        self._check("tag", name, cursor)
        if name == "missing":
            raise ScraperError.from_tiktok_code(10205)
        return TagInfo.from_tiktok(load("challenge_detail")), Feed()

    async def music(self, music_id: str, cursor: str = "0"):
        self._check("music", music_id, cursor)
        feed = Feed.from_tiktok(load("music_item_list"))
        return MusicInfo.from_music(feed.items[0].music, 12345), feed

    async def foryou(self):
        self._check("foryou")
        return Feed.from_tiktok(load("recommend_item_list"))

    async def following(self, cursor: str = "0"):
        self._check("following", cursor)
        return UserFeed.from_tiktok(load("user_list"))

    async def resolve_short(self, url: str) -> str:
        self._check("resolve_short", url)
        for key, value in self.short_map.items():
            if key in url:
                return value
        raise ScraperError(404, None, "Short link did not resolve")

    # ---------------------------------------------------------------- streaming
    def stream_client(self) -> httpx.AsyncClient:
        if self._client is None:
            def record(request: httpx.Request) -> httpx.Response:
                self.stream_requests.append(request)
                resp = self.handler(request)
                if hasattr(resp, "_content"):
                    # Response(content=...) is pre-read; re-wrap so the proxy can stream it like a real one
                    resp = httpx.Response(resp.status_code, headers=resp.headers,
                                          stream=httpx.ByteStream(resp.content))
                return resp

            self._client = httpx.AsyncClient(transport=httpx.MockTransport(record), follow_redirects=False)
        return self._client

    def stream_headers(self, range_header: Optional[str] = None) -> dict[str, str]:
        h = {"User-Agent": "test-agent", "Referer": "https://www.tiktok.com/", "Accept": "*/*"}
        if range_header:
            h["Range"] = range_header
        return h

    def cookie_header(self) -> str:
        return "ttwid=x; tt_chain_token=y"


# ---------------------------------------------------------------- pytest fixtures (import into test modules)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def fake() -> FakeScraper:
    return FakeScraper()


@pytest.fixture
def make_client(fake):
    from proxitok.config import Settings
    from proxitok.main import create_app

    def _make(**settings_kw) -> TestClient:
        return TestClient(create_app(Settings(_env_file=None, **settings_kw), scraper=fake))

    return _make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()
