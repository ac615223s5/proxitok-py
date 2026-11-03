"""Jinja2 environment + template globals (port of ProxiTok's Wrappers::latte and UrlBuilder.php)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Union
from urllib.parse import quote

import jinja2
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import __version__
from .config import Settings
from .scraper.models import TextExtra

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"

# Port of App\Constants\Themes
THEMES: tuple[str, ...] = ("default", "card")
DEFAULT_THEME = "default"

ASSET_DIRS = {"js": "/scripts", "css": "/styles"}


# ----------------------------------------------------------------------------- pure helpers


def theme_from_cookie(value: Optional[str]) -> str:
    """Cookies::theme(): cookie value if it is a known theme, else 'default'."""
    return value if value in THEMES else DEFAULT_THEME


def number(x: Union[int, float, str, None]) -> str:
    """Port of the ``number`` Latte function (https://stackoverflow.com/a/36365553).

    > 1000 -> first thousands group + optional first digit of the next group + K/M/B/T
    (1234 -> "1.2K", 1001 -> "1K", 96555924 -> "96.5M"); otherwise the number itself.
    """
    try:
        val = float(x or 0)
    except (TypeError, ValueError):
        val = 0.0
    if val > 1000:
        # PHP number_format() rounds half away from zero to an integer and groups by ","
        rounded = int(Decimal(str(val)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        groups = f"{rounded:,}".split(",")
        parts = ("K", "M", "B", "T")
        count = len(groups) - 1
        out = groups[0] + ("." + groups[1][0] if groups[1][0] != "0" else "")
        idx = min(count, len(parts)) - 1
        return out + parts[idx]
    if val.is_integer():
        return str(int(val))
    return str(val)


def bool_to_str(cond: Any) -> str:
    return "yes" if cond else "no"


_PHP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_PHP_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def php_date(fmt: str, ts: Union[int, float, str, None]) -> str:
    """Subset of PHP ``date()`` (UTC) for the formats the templates use: 'M d, Y', 'M d, Y H:i:s e', 'r'."""
    try:
        t = int(float(ts or 0))
    except (TypeError, ValueError):
        t = 0
    dt = datetime.fromtimestamp(t, tz=timezone.utc)
    out: list[str] = []
    escape_next = False
    for ch in fmt:
        if escape_next:
            out.append(ch)
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
        elif ch == "d":
            out.append(f"{dt.day:02d}")
        elif ch == "j":
            out.append(str(dt.day))
        elif ch == "D":
            out.append(_PHP_DAYS[dt.weekday()])
        elif ch == "M":
            out.append(_PHP_MONTHS[dt.month - 1])
        elif ch == "m":
            out.append(f"{dt.month:02d}")
        elif ch == "n":
            out.append(str(dt.month))
        elif ch == "Y":
            out.append(f"{dt.year:04d}")
        elif ch == "y":
            out.append(f"{dt.year % 100:02d}")
        elif ch == "H":
            out.append(f"{dt.hour:02d}")
        elif ch == "i":
            out.append(f"{dt.minute:02d}")
        elif ch == "s":
            out.append(f"{dt.second:02d}")
        elif ch == "e":
            out.append("UTC")
        elif ch == "r":
            out.append(format_datetime(dt))  # "Tue, 03 Nov 2026 12:00:00 +0000"
        else:
            out.append(ch)
    return "".join(out)


def _q(s: Any) -> str:
    """URL-encode a value for use in a path segment or query value."""
    return quote(str(s if s is not None else ""), safe="")


class Urls:
    """UrlBuilder.php port. ``prefix`` = APP_URL (may be "" -> root-relative links)."""

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix

    def path(self, endpoint: str = "") -> str:
        return self.prefix + endpoint

    def assets(self, type_: str, file: str, is_vendor: bool = False) -> str:
        if type_ not in ASSET_DIRS:
            raise ValueError("Invalid asset type")
        endpoint = ASSET_DIRS[type_]
        if is_vendor:
            endpoint += "/vendor"
        return self.path(f"{endpoint}/{file}")

    def stream(self, url: str) -> str:
        return self.path("/stream?url=" + _q(url))

    def download(self, url: str, username: str, id_: str, watermark: bool) -> str:
        out = self.path(f"/download?url={_q(url)}&id={_q(id_)}&user={_q(username)}")
        if watermark:
            out += "&watermark=1"
        return out

    def user(self, username: str) -> str:
        return self.path("/@" + _q(username))

    def tag(self, tag: str) -> str:
        return self.path("/tag/" + _q(tag))

    def video_internal(self, username: str, id_: str) -> str:
        return self.path(f"/@{_q(username)}/video/{_q(id_)}")

    @staticmethod
    def video_external(username: str, id_: str) -> str:
        return f"https://www.tiktok.com/@{_q(username)}/video/{_q(id_)}"


def _utf16_to_index(desc: str, offset: int) -> int:
    """Convert a UTF-16 code-unit offset into a Python str index."""
    units = 0
    for idx, ch in enumerate(desc):
        if units >= offset:
            return idx
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(desc)


def render_desc(desc: Optional[str], text_extras: Optional[Iterable[TextExtra]] = None,
                urls: Optional[Urls] = None) -> Markup:
    """Escape ``desc`` and turn mentions / hashtags into links.

    Fixes the PHP version: the description is HTML-escaped (markupsafe) before anything is inserted,
    TikTok's UTF-16 offsets are used to locate each span (``TextExtra.span``) and, when an extra has no
    usable offsets, the fallback token search uses ``re.escape`` so user text never becomes regex syntax.
    """
    desc = desc or ""
    urls = urls or Urls()
    spans: list[tuple[int, int, str]] = []  # (start_idx, end_idx, href) in Python str indices

    def target(extra: TextExtra) -> str:
        if extra.type == TextExtra.USER and extra.user_unique_id:
            return urls.user(extra.user_unique_id)
        if extra.type == TextExtra.HASHTAG and extra.hashtag_name:
            return urls.tag(extra.hashtag_name)
        return ""

    def overlaps(s: int, e: int) -> bool:
        return any(s < e2 and s2 < e for s2, e2, _ in spans)

    for extra in text_extras or []:
        href = target(extra)
        if not href:
            continue
        start = end = -1
        if extra.span(desc):
            start, end = _utf16_to_index(desc, extra.start), _utf16_to_index(desc, extra.end)
        else:
            # Fallback: find "#name" / "@user" as a whole token
            token = ("#" + extra.hashtag_name) if extra.type == TextExtra.HASHTAG else ("@" + extra.user_unique_id)
            for m in re.finditer(re.escape(token) + r"(?!\w)", desc):
                if not overlaps(m.start(), m.end()):
                    start, end = m.start(), m.end()
                    break
        if 0 <= start < end and not overlaps(start, end):
            spans.append((start, end, href))

    spans.sort()
    out: list[str] = []
    pos = 0
    for start, end, href in spans:
        out.append(str(escape(desc[pos:start])))
        out.append(f'<a href="{escape(href)}">{escape(desc[start:end])}</a>')
        pos = end
    out.append(str(escape(desc[pos:])))
    return Markup("".join(out))


def safe_link(link: Optional[str]) -> str:
    """Bio links come without a scheme ("tiktok.com/discovermore"); only ever emit http(s) URLs."""
    link = (link or "").strip()
    if not link:
        return ""
    lower = link.lower()
    if lower.startswith(("http://", "https://")):
        return link
    if ":" in link.split("/", 1)[0] or link.startswith("//"):
        # some other scheme (javascript:, data:, ...) or protocol-relative -> force https on the host part
        link = link.split(":", 1)[-1].lstrip("/")
    return "https://" + link


def json_pretty(value: Any) -> str:
    return json.dumps(value, indent=4, ensure_ascii=False, default=str)


# ----------------------------------------------------------------------------- environment


def create_templates(settings: Settings, directory: Path = TEMPLATES_DIR) -> Jinja2Templates:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(directory)),
        autoescape=jinja2.select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    def urls_for(ctx: Any) -> Urls:
        # RSS readers need absolute links: the RSS routes pass ``url_base`` when APP_URL is empty.
        return Urls(settings.APP_URL or (ctx.get("url_base") or ""))

    @jinja2.pass_context
    def path(ctx, endpoint: str = "") -> str:
        return urls_for(ctx).path(endpoint)

    @jinja2.pass_context
    def assets(ctx, type_: str, file: str, is_vendor: bool = False) -> str:
        return urls_for(ctx).assets(type_, file, is_vendor)

    @jinja2.pass_context
    def theme(ctx) -> str:
        req = ctx.get("request")
        return theme_from_cookie(req.cookies.get("theme") if req is not None else None)

    @jinja2.pass_context
    def url_stream(ctx, url: str) -> str:
        return urls_for(ctx).stream(url)

    @jinja2.pass_context
    def url_user(ctx, username: str) -> str:
        return urls_for(ctx).user(username)

    @jinja2.pass_context
    def url_tag(ctx, tag: str) -> str:
        return urls_for(ctx).tag(tag)

    @jinja2.pass_context
    def url_video_internal(ctx, username: str, id_: str) -> str:
        return urls_for(ctx).video_internal(username, id_)

    @jinja2.pass_context
    def url_download(ctx, url: str, username: str, id_: str, watermark: bool) -> str:
        return urls_for(ctx).download(url, username, id_, watermark)

    @jinja2.pass_context
    def rss_url(ctx) -> str:
        req = ctx.get("request")
        current = req.url.path.rstrip("/") if req is not None else ""
        return urls_for(ctx).path(current + "/rss")

    @jinja2.pass_context
    def render_desc_ctx(ctx, desc: str, text_extras=None) -> Markup:
        return render_desc(desc, text_extras, urls_for(ctx))

    env.globals.update(
        path=path,
        assets=assets,
        theme=theme,
        themes=THEMES,
        version_frontend=lambda: __version__,
        number=number,
        url_stream=url_stream,
        url_user=url_user,
        url_tag=url_tag,
        url_video_internal=url_video_internal,
        url_video_external=Urls.video_external,
        url_download=url_download,
        bool_to_str=bool_to_str,
        render_desc=render_desc_ctx,
        php_date=php_date,
        rss_url=rss_url,
        safe_link=safe_link,
        app_debug=settings.APP_DEBUG,
    )
    env.filters["date"] = lambda ts, fmt="M d, Y": php_date(fmt, ts)
    env.filters["json_pretty"] = json_pretty
    return Jinja2Templates(env=env)


__all__ = [
    "THEMES", "DEFAULT_THEME", "theme_from_cookie", "number", "bool_to_str", "php_date", "Urls",
    "render_desc", "safe_link", "create_templates",
]
