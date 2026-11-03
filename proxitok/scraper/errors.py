"""Scraper error type. Rendered by the error view (parity with PHP ErrorTemplate)."""
from __future__ import annotations

from typing import Any, Optional


class ScraperError(Exception):
    """Raised by every Scraper method on failure.

    http_code:   HTTP status the *ProxiTok* response should use (404 user/video not found, 502 TikTok
                 failure/empty body, 504 browser timeout, 403 private, ...).
    tiktok_code: TikTok's statusCode if one was returned (e.g. 10202 user not found, 10204 video not
                 found, 10216 private video, 10221 banned user, 10000 captcha). None when not applicable.
                 -1 means "TikTok returned an empty / unparseable body" (likely blocked / needs verify).
    msg:         Human readable message (TikTok statusMsg or our explanation).
    debug:       Optional JSON-serialisable dict (url, status, body excerpt) shown in "Show debug info"
                 only when APP_DEBUG is enabled.
    """

    # Common TikTok codes -> (http_code, message)
    KNOWN_CODES: dict[int, tuple[int, str]] = {
        -1: (502, "Empty response from TikTok"),
        10000: (502, "Captcha required"),
        10101: (502, "Server error"),
        10102: (403, "User not logged in"),
        10201: (404, "Not found"),
        10202: (404, "User not found"),
        10203: (404, "Music not found"),
        10204: (404, "Video not found"),
        10205: (404, "Challenge not found"),
        10216: (403, "This video is private"),
        10221: (404, "User not found or banned"),  # live: nonexistent user -> 10221
        10222: (403, "This account is private"),
        10223: (403, "User not found or private"),
    }

    def __init__(
        self,
        http_code: int = 502,
        tiktok_code: Optional[int] = None,
        msg: str = "",
        debug: Optional[dict[str, Any]] = None,
    ) -> None:
        self.http_code = http_code
        self.tiktok_code = tiktok_code
        self.msg = msg
        self.debug = debug
        super().__init__(f"[{http_code}/{tiktok_code}] {msg}")

    @classmethod
    def from_tiktok_code(cls, code: int, msg: str = "", debug: Optional[dict[str, Any]] = None) -> "ScraperError":
        http_code, default_msg = cls.KNOWN_CODES.get(code, (502, "TikTok error"))
        return cls(http_code=http_code, tiktok_code=code, msg=msg or default_msg, debug=debug)
