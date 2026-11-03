"""Cache backends for raw TikTok JSON (hydration sections / API responses).

Values must be JSON-serialisable (dicts/lists/str/num); the scraper caches the raw TikTok data and
re-parses it into models, so all backends behave the same.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

log = logging.getLogger(__name__)


class Cache(Protocol):
    async def get(self, key: str) -> Optional[Any]: ...
    async def set(self, key: str, value: Any, ttl: int) -> None: ...
    async def close(self) -> None: ...


class NullCache:
    async def get(self, key: str) -> Optional[Any]:
        return None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        return None

    async def close(self) -> None:
        return None


class MemoryCache:
    """In-process TTL + LRU cache."""

    def __init__(self, max_entries: int = 256) -> None:
        self.max_entries = max_entries
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()

    async def get(self, key: str) -> Optional[Any]:
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, value = hit
        if expires < time.time():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    async def set(self, key: str, value: Any, ttl: int) -> None:
        if ttl <= 0:
            return
        self._data[key] = (time.time() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    async def close(self) -> None:
        self._data.clear()


class JSONCache:
    """One JSON file per key under ``directory`` ({"expires": ts, "data": ...})."""

    def __init__(self, directory: str) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.dir / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def _read(self, path: Path) -> Optional[Any]:
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(blob, dict) or blob.get("expires", 0) < time.time():
            try:
                path.unlink()
            except OSError:
                pass
            return None
        return blob.get("data")

    def _write(self, path: Path, value: Any, ttl: int) -> None:
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"expires": time.time() + ttl, "data": value}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    async def get(self, key: str) -> Optional[Any]:
        return await asyncio.to_thread(self._read, self._path(key))

    async def set(self, key: str, value: Any, ttl: int) -> None:
        if ttl <= 0:
            return
        try:
            await asyncio.to_thread(self._write, self._path(key), value, ttl)
        except OSError as e:
            log.warning("JSON cache write failed: %s", e)

    async def close(self) -> None:
        return None


class RedisCache:
    def __init__(self, host: str, port: int, password: Optional[str], prefix: str = "proxitok:") -> None:
        import redis.asyncio as aioredis  # optional dependency (extra "redis")

        self.prefix = prefix
        self.r = aioredis.Redis(host=host, port=port, password=password)

    async def get(self, key: str) -> Optional[Any]:
        try:
            raw = await self.r.get(self.prefix + key)
        except Exception as e:
            log.warning("Redis get failed: %s", e)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        if ttl <= 0:
            return
        try:
            await self.r.set(self.prefix + key, json.dumps(value, ensure_ascii=False), ex=ttl)
        except Exception as e:
            log.warning("Redis set failed: %s", e)

    async def close(self) -> None:
        try:
            await self.r.aclose()
        except Exception:
            pass


def make_cache(settings: "Settings") -> Cache:
    """Build the backend selected by ``API_CACHE``. Falls back to memory (with a warning) when
    the json directory isn't writable or redis isn't installed/configured."""
    kind = settings.API_CACHE
    try:
        if kind == "none":
            return NullCache()
        if kind == "json":
            return JSONCache(settings.API_CACHE_JSON)
        if kind == "redis":
            host, port, password = settings.redis_params()
            return RedisCache(host, port, password)
    except (ImportError, ValueError, OSError) as e:
        log.warning("Cache backend %r unavailable (%s); using in-memory cache", kind, e)
    return MemoryCache()


__all__ = ["Cache", "NullCache", "MemoryCache", "JSONCache", "RedisCache", "make_cache"]
