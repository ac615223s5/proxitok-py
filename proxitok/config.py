"""Application settings (environment / .env). Variable names follow ProxiTok's .env.example."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional
from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False)

    # Absolute URL of this instance, e.g. "https://proxitok.example.com" (no trailing slash).
    # Empty -> links are root-relative ("/@user").
    APP_URL: str = ""
    APP_DEBUG: bool = False

    # Cache: "memory" (default), "json", "redis", or "none" to disable.
    API_CACHE: Literal["memory", "json", "redis", "none"] = "memory"
    API_CACHE_JSON: str = "./cache/api"
    REDIS_URL: Optional[str] = None
    REDIS_HOST: Optional[str] = None
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: Optional[str] = None
    # Seconds a scraped response stays cached. Keep well under TikTok's CDN URL expiry (~48h) and note
    # that video URLs are bound to the session cookies (see scraper.api.Scraper.stream_client).
    CACHE_TTL: int = 3600

    # Outbound proxy for TikTok traffic: http:// https:// socks4:// socks5://
    PROXY: Optional[str] = None
    USER_AGENT: str = DEFAULT_USER_AGENT

    # Playwright Chromium used for signed feed API calls
    BROWSER_HEADLESS: bool = True
    BROWSER_TIMEOUT: float = 30.0  # seconds per in-page API call / navigation

    @field_validator("APP_URL")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("API_CACHE", mode="before")
    @classmethod
    def _cache_lower(cls, v):
        if v is None or v == "":
            return "memory"
        return str(v).lower()

    @field_validator("PROXY", "REDIS_URL", "REDIS_HOST", "REDIS_PASSWORD", mode="before")
    @classmethod
    def _empty_none(cls, v):
        return v or None

    def redis_params(self) -> tuple[str, int, Optional[str]]:
        """(host, port, password) from REDIS_URL or REDIS_HOST/PORT/PASSWORD. Raises ValueError if unset."""
        if self.REDIS_URL:
            u = urlparse(self.REDIS_URL)
            return u.hostname or "localhost", u.port or 6379, u.password
        if self.REDIS_HOST:
            return self.REDIS_HOST, self.REDIS_PORT, self.REDIS_PASSWORD
        raise ValueError("You need to set REDIS_URL or REDIS_HOST and REDIS_PORT to use Redis cache!")


@lru_cache
def get_settings() -> Settings:
    return Settings()
