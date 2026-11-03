"""BrowserSession lifecycle / locking, with a fake Playwright (no Chromium needed)."""
import asyncio
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from proxitok.scraper.browser import BrowserSession
from proxitok.scraper.errors import ScraperError


class FakeReq:
    def __init__(self, url):
        self.url = url


class FakePage:
    def __init__(self, pw):
        self.pw = pw
        self.closed = False
        self.listeners = []
        self.urls = []

    def on(self, event, fn):
        self.listeners.append(fn)

    def remove_listener(self, event, fn):
        self.listeners.remove(fn)

    async def goto(self, url, **kw):
        await asyncio.sleep(self.pw.goto_delay)
        for fn in list(self.listeners):
            fn(FakeReq("https://www.tiktok.com/api/explore/item_list/?aid=1988&device_id=42&odinId=7"))
        self.pw.cookies.append({"name": "msToken", "value": "m", "domain": ".tiktok.com", "path": "/"})

    def is_closed(self):
        return self.closed

    async def evaluate(self, js, arg):
        self.urls.append(arg["url"])
        return await self.pw.evaluate(arg)


class FakeCtx:
    def __init__(self, pw):
        self.pw = pw
        self.closed = False
        self.pages = []

    def set_default_timeout(self, ms):
        pass

    async def add_cookies(self, cookies):
        pass

    async def cookies(self, *a):
        return list(self.pw.cookies)

    async def new_page(self):
        p = FakePage(self.pw)
        self.pages.append(p)
        self.pw.pages.append(p)
        return p

    async def close(self):
        self.closed = True
        for p in self.pages:
            p.closed = True


class FakeBrowserObj:
    def __init__(self, pw):
        self.pw = pw
        self.closed = False

    async def new_context(self, **kw):
        c = FakeCtx(self.pw)
        self.pw.contexts.append(c)
        return c

    async def close(self):
        self.closed = True


class FakePW:
    def __init__(self, goto_delay=0.0, evaluate=None):
        self.goto_delay = goto_delay
        self.cookies = []
        self.browsers, self.contexts, self.pages = [], [], []
        self.chromium = self
        self._evaluate = evaluate

    async def launch(self, **kw):
        b = FakeBrowserObj(self)
        self.browsers.append(b)
        return b

    async def evaluate(self, arg):
        if self._evaluate is not None:
            return await self._evaluate(arg)
        return {"status": 200, "body": json.dumps({"statusCode": 0, "ok": True})}

    async def stop(self):
        pass


def session(pw, **kw):
    s = BrowserSession(**kw)
    s._pw = pw
    return s


async def test_launch_survives_caller_cancellation():
    """Regression: a caller timeout during the first launch left started=True with empty params."""
    pw = FakePW(goto_delay=0.5)
    s = session(pw, timeout=5)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(s.fetch_json("/api/music/item_list/", {"musicID": "1"}), timeout=0.1)
    assert not s.started  # not usable yet - but the launch keeps going in the background
    await asyncio.sleep(0.8)
    assert s.started and s._base.get("device_id") == "42" and s.last_start > 0
    data = await s.fetch_json("/api/music/item_list/", {"musicID": "1"})
    assert data["ok"] is True
    q = parse_qs(urlparse(pw.pages[-1].urls[-1]).query)
    assert q["device_id"] == ["42"] and q["aid"] == ["1988"] and q["musicID"] == ["1"]
    assert s.launches == 1
    assert pw.pages[-1].listeners == []  # request listener removed
    await s.close()


async def test_cancelled_launch_cleans_up_and_retries():
    pw = FakePW(goto_delay=5)
    s = session(pw, timeout=5)
    task = asyncio.ensure_future(s._start())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not s.started and s._base == {}
    assert pw.browsers[0].closed and pw.contexts[0].closed
    pw.goto_delay = 0
    assert (await s.fetch_json("/x/", {}))["ok"] is True
    assert s.started and s._base.get("device_id") == "42"
    await s.close()


async def test_close_during_launch():
    pw = FakePW(goto_delay=5)
    s = session(pw, timeout=5)
    call = asyncio.ensure_future(s.fetch_json("/x/", {}))
    await asyncio.sleep(0.1)
    await s.close()
    with pytest.raises((ScraperError, asyncio.CancelledError)):
        await call
    assert all(b.closed for b in pw.browsers)


async def test_hung_call_does_not_relaunch_and_queue_is_bounded():
    """Regression: one hung call held the lock for ~2x timeout + relaunch, and queued calls waited forever."""
    async def hang(arg):
        await asyncio.sleep(3600)

    pw = FakePW(evaluate=hang)
    s = session(pw, timeout=0.5, lock_timeout=0.2)
    t0 = time.monotonic()
    first = asyncio.ensure_future(s.fetch_json("/x/", {}, attempts=3))
    await asyncio.sleep(0.05)
    with pytest.raises(ScraperError) as busy:
        await s.fetch_json("/y/", {})
    assert busy.value.http_code == 503 and "busy" in busy.value.msg
    with pytest.raises(ScraperError) as ei:
        await first
    elapsed = time.monotonic() - t0
    assert ei.value.http_code == 504
    assert elapsed < 1.5  # one timeout, no retries / relaunch
    assert s.launches == 1 and s.started
    await s.close()


async def test_repeated_timeouts_relaunch():
    async def hang(arg):
        await asyncio.sleep(3600)

    pw = FakePW(evaluate=hang)
    s = session(pw, timeout=0.2)
    for _ in range(3):
        with pytest.raises(ScraperError):
            await s.fetch_json("/x/", {}, attempts=1)
    assert not s.started  # torn down after 3 timeouts in a row; next call relaunches
    pw._evaluate = None
    assert (await s.fetch_json("/x/", {}))["ok"] is True
    assert s.launches == 2
    await s.close()


async def test_in_page_abort_is_a_timeout():
    async def aborted(arg):
        return {"status": 0, "body": "", "error": "TimeoutError", "aborted": True}

    pw = FakePW(evaluate=aborted)
    s = session(pw, timeout=5)
    with pytest.raises(ScraperError) as ei:
        await s.fetch_json("/x/", {})
    assert ei.value.http_code == 504 and s.launches == 1
    await s.close()


async def test_empty_body_retries_then_502():
    async def empty(arg):
        return {"status": 403, "body": ""}

    pw = FakePW(evaluate=empty)
    s = session(pw, timeout=5)
    with pytest.raises(ScraperError) as ei:
        await s.fetch_json("/x/", {}, attempts=2)
    assert ei.value.http_code == 502 and ei.value.tiktok_code == -1
    assert len(pw.pages[-1].urls) == 2
    await s.close()
