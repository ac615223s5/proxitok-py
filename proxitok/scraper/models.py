"""Normalized TikTok data models.

Every model has ``from_tiktok(dict)`` which accepts the raw camelCase TikTok JSON (hydration
``__DEFAULT_SCOPE__`` objects or web API responses), tolerates missing keys / ``None`` / string
numbers, and never raises on shape drift (missing data -> empty string / 0 / empty list).

Attribute names are snake_case. Templates should use only these attributes.

Source shapes (captured live 2026-11-03, see tests/fixtures/):
  * user page hydration     -> scope["webapp.user-detail"]  = {userInfo:{user,stats,statsV2}, shareMeta:{title,desc}, statusCode}
  * video page hydration    -> scope["webapp.video-detail"] = {itemInfo:{itemStruct}, shareMeta, statusCode}
  * item lists              -> {itemList:[itemStruct...], hasMore, cursor}  (creator/item_list: hasMorePrevious, no cursor)
  * challenge/detail        -> {challengeInfo:{challenge, stats, statsV2}, shareMeta}
  * music/detail            -> {musicInfo:{music, author?, stats}, shareMeta}
  * user/list (suggested)   -> {userList:[{user, stats, statsV2}], hasMore, minCursor}
  * comment/list            -> {comments:[{cid,text,create_time,digg_count,user:{unique_id,nickname,avatar_thumb:{url_list}}}], has_more, cursor}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# ----------------------------------------------------------------------------- helpers


def _d(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _l(v: Any) -> list:
    return v if isinstance(v, list) else []


def _s(v: Any) -> str:
    if v is None:
        return ""
    return v if isinstance(v, str) else str(v)


def _i(v: Any) -> int:
    if v is None or v == "":
        return 0
    if isinstance(v, bool):
        return int(v)
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0


def _b(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return bool(v)


def _stat(raw: dict, raw_v2: dict, key: str) -> int:
    """Prefer exact statsV2 string counters, fall back to rounded stats ints."""
    if key in raw_v2 and raw_v2[key] not in (None, ""):
        return _i(raw_v2[key])
    return _i(raw.get(key))


def _first_url(v: Any) -> str:
    """First URL of {urlList:[...]} / {url_list:[...]} / [..] / str."""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return _s(v[0]) if v else ""
    d = _d(v)
    lst = d.get("urlList") or d.get("url_list") or []
    return _s(lst[0]) if lst else ""


# ----------------------------------------------------------------------------- small models


@dataclass
class OgMeta:
    """OpenGraph meta from TikTok ``shareMeta`` ({title, desc})."""

    title: str = ""
    description: str = ""

    @classmethod
    def from_tiktok(cls, d: Optional[dict]) -> "OgMeta":
        d = _d(d)
        return cls(title=_s(d.get("title")), description=_s(d.get("desc") or d.get("description")))


@dataclass
class Stats:
    """Per-item counters."""

    play_count: int = 0
    digg_count: int = 0
    comment_count: int = 0
    share_count: int = 0
    collect_count: int = 0

    @classmethod
    def from_tiktok(cls, d: Optional[dict], v2: Optional[dict] = None) -> "Stats":
        d, v2 = _d(d), _d(v2)
        return cls(
            play_count=_stat(d, v2, "playCount"),
            digg_count=_stat(d, v2, "diggCount"),
            comment_count=_stat(d, v2, "commentCount"),
            share_count=_stat(d, v2, "shareCount"),
            collect_count=_stat(d, v2, "collectCount"),
        )


@dataclass
class VideoInfo:
    """``itemStruct.video``. For image posts play_addr/download_addr are "" (check Item.is_image_post)."""

    id: str = ""
    play_addr: str = ""
    download_addr: str = ""  # watermarked; may be "" -> templates fall back to play_addr
    origin_cover: str = ""
    dynamic_cover: str = ""
    cover: str = ""
    duration: int = 0
    width: int = 0
    height: int = 0

    @classmethod
    def from_tiktok(cls, d: Optional[dict]) -> "VideoInfo":
        d = _d(d)
        play = d.get("playAddr")
        if isinstance(play, dict):  # some API variants nest {UrlList:[...]}
            play = _first_url({"urlList": play.get("UrlList") or play.get("urlList")})
        return cls(
            id=_s(d.get("id")),
            play_addr=_s(play),
            download_addr=_s(d.get("downloadAddr")),
            origin_cover=_s(d.get("originCover") or d.get("cover")),
            dynamic_cover=_s(d.get("dynamicCover")),
            cover=_s(d.get("cover") or d.get("originCover")),
            duration=_i(d.get("duration")),
            width=_i(d.get("width")),
            height=_i(d.get("height")),
        )


@dataclass
class ImagePost:
    """Photo-mode post. ``images`` = first CDN URL of every image, in order."""

    images: list[str] = field(default_factory=list)
    cover: str = ""
    title: str = ""

    @classmethod
    def from_tiktok(cls, d: Optional[dict]) -> Optional["ImagePost"]:
        """Returns None when d is empty / has no images (i.e. the item is a normal video)."""
        d = _d(d)
        images = [u for u in (_first_url(_d(img).get("imageURL")) for img in _l(d.get("images"))) if u]
        if not images:
            return None
        return cls(images=images, cover=_first_url(_d(d.get("cover")).get("imageURL")), title=_s(d.get("title")))


@dataclass
class Music:
    id: str = ""
    title: str = ""
    play_url: str = ""  # may be "" (copyright-restricted sounds)
    author_name: str = ""
    cover_large: str = ""
    cover_medium: str = ""
    cover_thumb: str = ""
    original: bool = False
    duration: int = 0

    @classmethod
    def from_tiktok(cls, d: Optional[dict]) -> "Music":
        d = _d(d)
        return cls(
            id=_s(d.get("id") or d.get("idStr")),
            title=_s(d.get("title")),
            play_url=_first_url(d.get("playUrl")),
            author_name=_s(d.get("authorName")),
            cover_large=_s(d.get("coverLarge")),
            cover_medium=_s(d.get("coverMedium")),
            cover_thumb=_s(d.get("coverThumb")),
            original=_b(d.get("original")),
            duration=_i(d.get("duration")),
        )


@dataclass
class Author:
    """Item author (``itemStruct.author``) and also the base of UserDetail."""

    id: str = ""
    unique_id: str = ""
    nickname: str = ""
    avatar_thumb: str = ""
    avatar_medium: str = ""
    avatar_larger: str = ""
    signature: str = ""
    verified: bool = False
    sec_uid: str = ""
    private_account: bool = False

    @classmethod
    def from_tiktok(cls, d: Optional[dict]) -> "Author":
        d = _d(d)
        return cls(
            id=_s(d.get("id")),
            unique_id=_s(d.get("uniqueId")),
            nickname=_s(d.get("nickname")),
            avatar_thumb=_s(d.get("avatarThumb")),
            avatar_medium=_s(d.get("avatarMedium")),
            avatar_larger=_s(d.get("avatarLarger") or d.get("avatarMedium") or d.get("avatarThumb")),
            signature=_s(d.get("signature")),
            verified=_b(d.get("verified")),
            sec_uid=_s(d.get("secUid")),
            private_account=_b(d.get("privateAccount")),
        )


@dataclass
class TextExtra:
    """Mention / hashtag span inside Item.desc.

    IMPORTANT: ``start``/``end`` are **UTF-16 code unit** offsets into desc, start inclusive, end
    exclusive (verified live: desc with two emoji, '@Mason Smith' at start=55,end=67). For mentions the
    span text is the display name ("@Mason Smith"), NOT "@"+user_unique_id. Use :meth:`span` to slice.
    """

    USER = 0
    HASHTAG = 1

    type: int = 0
    start: int = 0
    end: int = 0
    user_unique_id: str = ""
    hashtag_name: str = ""

    @classmethod
    def from_tiktok(cls, d: Optional[dict]) -> "TextExtra":
        d = _d(d)
        return cls(
            type=_i(d.get("type")),
            start=_i(d.get("start")),
            end=_i(d.get("end")),
            user_unique_id=_s(d.get("userUniqueId")),
            hashtag_name=_s(d.get("hashtagName")),
        )

    def span(self, desc: str) -> str:
        """Substring of ``desc`` this extra covers (UTF-16 offsets). "" if out of range."""
        u = desc.encode("utf-16-le")
        if not (0 <= self.start < self.end <= len(u) // 2):
            return ""
        return u[self.start * 2 : self.end * 2].decode("utf-16-le", errors="ignore")


@dataclass
class Challenge:
    """Hashtag. Used for Item.challenges and TagInfo.detail."""

    id: str = ""
    title: str = ""
    desc: str = ""
    profile_larger: str = ""
    cover_larger: str = ""
    is_commerce: bool = False

    @classmethod
    def from_tiktok(cls, d: Optional[dict]) -> "Challenge":
        d = _d(d)
        return cls(
            id=_s(d.get("id")),
            title=_s(d.get("title")),
            desc=_s(d.get("desc")),
            profile_larger=_s(d.get("profileLarger") or d.get("profileMedium") or d.get("profileThumb")),
            cover_larger=_s(d.get("coverLarger") or d.get("coverMedium") or d.get("coverThumb")),
            is_commerce=_b(d.get("isCommerce")),
        )


@dataclass
class CommentUser:
    unique_id: str = ""
    nickname: str = ""
    avatar_thumb: str = ""


@dataclass
class Comment:
    """One entry of /api/comment/list/ ``comments`` (snake_case already in TikTok's JSON)."""

    cid: str = ""
    text: str = ""
    create_time: int = 0
    digg_count: int = 0
    reply_count: int = 0
    user: CommentUser = field(default_factory=CommentUser)

    @classmethod
    def from_tiktok(cls, d: Optional[dict]) -> "Comment":
        d = _d(d)
        u = _d(d.get("user"))
        return cls(
            cid=_s(d.get("cid")),
            text=_s(d.get("text")),
            create_time=_i(d.get("create_time")),
            digg_count=_i(d.get("digg_count")),
            reply_count=_i(d.get("reply_comment_total")),
            user=CommentUser(
                unique_id=_s(u.get("unique_id")),
                nickname=_s(u.get("nickname")),
                avatar_thumb=_first_url(u.get("avatar_thumb")),
            ),
        )


# ----------------------------------------------------------------------------- item


@dataclass
class Item:
    """A post (video or photo) - ``itemStruct``."""

    id: str = ""
    desc: str = ""
    create_time: int = 0  # unix seconds (TikTok sends a string in hydration, int in APIs)
    video: VideoInfo = field(default_factory=VideoInfo)
    author: Author = field(default_factory=Author)
    music: Music = field(default_factory=Music)
    stats: Stats = field(default_factory=Stats)
    challenges: list[Challenge] = field(default_factory=list)
    text_extra: list[TextExtra] = field(default_factory=list)
    image_post: Optional[ImagePost] = None
    duet_enabled: bool = False
    stitch_enabled: bool = False
    share_enabled: bool = False
    # Filled by Scraper.video() only (feeds leave it empty)
    comments: list[Comment] = field(default_factory=list)

    @property
    def is_image_post(self) -> bool:
        return self.image_post is not None

    @property
    def has_video(self) -> bool:
        """True when there is a playable/downloadable mp4 (False for photo posts)."""
        return bool(self.video.play_addr) and not self.is_image_post

    @classmethod
    def from_tiktok(cls, d: Optional[dict]) -> "Item":
        d = _d(d)

        def flag(enabled_key: str, display_key: str) -> bool:
            # PHP: isset($item->duetEnabled) ? duetEnabled : boolval(duetDisplay)
            if enabled_key in d and d[enabled_key] is not None:
                return _b(d[enabled_key])
            return _b(d.get(display_key))

        return cls(
            id=_s(d.get("id")),
            desc=_s(d.get("desc")),
            create_time=_i(d.get("createTime")),
            video=VideoInfo.from_tiktok(d.get("video")),
            author=Author.from_tiktok(d.get("author")),
            music=Music.from_tiktok(d.get("music")),
            stats=Stats.from_tiktok(d.get("stats"), d.get("statsV2")),
            challenges=[Challenge.from_tiktok(c) for c in _l(d.get("challenges"))],
            text_extra=[TextExtra.from_tiktok(t) for t in _l(d.get("textExtra"))],
            image_post=ImagePost.from_tiktok(d.get("imagePost")),
            duet_enabled=flag("duetEnabled", "duetDisplay"),
            stitch_enabled=flag("stitchEnabled", "stitchDisplay"),
            share_enabled=flag("shareEnabled", "shareDisplay"),
        )


# ----------------------------------------------------------------------------- user


@dataclass
class UserDetail(Author):
    """Author + profile-only fields."""

    region: str = ""
    bio_link: Optional[str] = None  # e.g. "tiktok.com/discovermore" (no scheme!) or None
    commerce_user: bool = False
    category: str = ""  # commerceUserInfo.category, only meaningful if commerce_user

    @classmethod
    def from_tiktok(cls, d: Optional[dict]) -> "UserDetail":
        d = _d(d)
        base = Author.from_tiktok(d)
        commerce = _d(d.get("commerceUserInfo"))
        link = _s(_d(d.get("bioLink")).get("link")) or None
        return cls(
            **base.__dict__,
            region=_s(d.get("region")),
            bio_link=link,
            commerce_user=_b(commerce.get("commerceUser")),
            category=_s(commerce.get("category")),
        )


@dataclass
class UserStats:
    follower_count: int = 0
    following_count: int = 0
    heart_count: int = 0
    video_count: int = 0
    digg_count: int = 0
    friend_count: int = 0

    @classmethod
    def from_tiktok(cls, d: Optional[dict], v2: Optional[dict] = None) -> "UserStats":
        d, v2 = _d(d), _d(v2)
        heart = _stat(d, v2, "heartCount") or _stat(d, v2, "heart")
        return cls(
            follower_count=_stat(d, v2, "followerCount"),
            following_count=_stat(d, v2, "followingCount"),
            heart_count=heart,
            video_count=_stat(d, v2, "videoCount"),
            digg_count=_stat(d, v2, "diggCount"),
            friend_count=_stat(d, v2, "friendCount"),
        )


@dataclass
class UserInfo:
    """Profile header data: ``info.detail.*``, ``info.stats.*``, ``info.og.*``."""

    detail: UserDetail = field(default_factory=UserDetail)
    stats: UserStats = field(default_factory=UserStats)
    og: OgMeta = field(default_factory=OgMeta)

    @classmethod
    def from_tiktok(cls, d: Optional[dict], share_meta: Optional[dict] = None) -> "UserInfo":
        """d = ``userInfo`` ({user, stats, statsV2}) or a user/list entry (same shape).

        Also accepts the whole ``webapp.user-detail`` scope ({userInfo, shareMeta}).
        """
        d = _d(d)
        if "userInfo" in d:
            share_meta = share_meta if share_meta is not None else d.get("shareMeta")
            d = _d(d["userInfo"])
        return cls(
            detail=UserDetail.from_tiktok(d.get("user")),
            stats=UserStats.from_tiktok(d.get("stats"), d.get("statsV2")),
            og=OgMeta.from_tiktok(share_meta),
        )

    @classmethod
    def from_author(cls, author: Optional[dict], author_stats: Optional[dict] = None,
                    share_meta: Optional[dict] = None, author_stats_v2: Optional[dict] = None) -> "UserInfo":
        """Build from ``itemStruct.author`` (+ ``authorStats``/``authorStatsV2``) for the video page."""
        return cls(
            detail=UserDetail.from_tiktok(author),
            stats=UserStats.from_tiktok(author_stats, author_stats_v2),
            og=OgMeta.from_tiktok(share_meta),
        )


# ----------------------------------------------------------------------------- tag / music


@dataclass
class TagStats:
    video_count: int = 0
    view_count: int = 0


@dataclass
class TagInfo:
    """challenge/detail -> ``info.detail`` (Challenge), ``info.stats``, ``info.og``."""

    detail: Challenge = field(default_factory=Challenge)
    stats: TagStats = field(default_factory=TagStats)
    og: OgMeta = field(default_factory=OgMeta)

    @classmethod
    def from_tiktok(cls, d: Optional[dict]) -> "TagInfo":
        """d = full challenge/detail response ({challengeInfo, shareMeta}) or just challengeInfo."""
        d = _d(d)
        ci = _d(d.get("challengeInfo")) if "challengeInfo" in d else d
        st, v2 = _d(ci.get("stats")), _d(ci.get("statsV2"))
        return cls(
            detail=Challenge.from_tiktok(ci.get("challenge")),
            stats=TagStats(video_count=_stat(st, v2, "videoCount"), view_count=_stat(st, v2, "viewCount")),
            og=OgMeta.from_tiktok(d.get("shareMeta")),
        )


@dataclass
class MusicStats:
    # None = unknown (info built from a feed item because music/detail was unavailable)
    video_count: Optional[int] = None


@dataclass
class MusicInfo:
    """music/detail -> ``info.detail`` (Music + desc), ``info.stats.video_count``, ``info.og``."""

    detail: Music = field(default_factory=Music)
    desc: str = ""  # subtitle shown on the music page (author name / album)
    stats: MusicStats = field(default_factory=MusicStats)
    og: OgMeta = field(default_factory=OgMeta)

    @classmethod
    def from_tiktok(cls, d: Optional[dict]) -> "MusicInfo":
        """d = full music/detail response ({musicInfo, shareMeta}) or just musicInfo."""
        d = _d(d)
        mi = _d(d.get("musicInfo")) if "musicInfo" in d else d
        music = Music.from_tiktok(mi.get("music"))
        st, v2 = _d(mi.get("stats")), _d(mi.get("statsV2"))
        author = _d(mi.get("author"))
        desc = _s(_d(mi.get("music")).get("album")) or music.author_name or _s(author.get("nickname"))
        return cls(
            detail=music,
            desc=desc,
            stats=MusicStats(video_count=_stat(st, v2, "videoCount")),
            og=OgMeta.from_tiktok(d.get("shareMeta")),
        )

    @classmethod
    def from_music(cls, music: Music, video_count: Optional[int] = None) -> "MusicInfo":
        """Fallback when music/detail is unavailable: build from an item's ``music``.

        ``video_count`` stays None (unknown) unless given; the music page then hides the "Videos" line
        instead of showing a made-up 0."""
        return cls(detail=music, desc=music.author_name, stats=MusicStats(video_count=video_count),
                   og=OgMeta(title=music.title, description=music.author_name))


# ----------------------------------------------------------------------------- feeds


@dataclass
class Feed:
    """A page of items. ``cursor`` is an opaque string to put in ``?cursor=`` for the next page."""

    items: list[Item] = field(default_factory=list)
    has_more: bool = False
    cursor: str = "0"

    @classmethod
    def from_tiktok(cls, d: Optional[dict]) -> "Feed":
        """Generic {itemList, hasMore, cursor} (challenge/music/recommend/post item_list)."""
        d = _d(d)
        items = [Item.from_tiktok(i) for i in _l(d.get("itemList") or d.get("items"))]
        has_more = _b(d.get("hasMore")) or _b(d.get("hasMorePrevious"))
        return cls(items=items, has_more=has_more, cursor=_s(d.get("cursor")) or "0")

    @classmethod
    def from_creator_list(cls, d: Optional[dict]) -> "Feed":
        """/api/creator/item_list/ (user feed): pagination is time based.

        No cursor in response; next cursor = oldest item's createTime in **milliseconds**;
        "more" flag is ``hasMorePrevious``.
        """
        d = _d(d)
        items = [Item.from_tiktok(i) for i in _l(d.get("itemList"))]
        times = [i.create_time for i in items if i.create_time]
        cursor = str(min(times) * 1000) if times else "0"
        return cls(items=items, has_more=_b(d.get("hasMorePrevious")) and bool(items), cursor=cursor)


@dataclass
class UserFeed:
    """A page of users (Following / Discover). ``users`` are UserInfo with empty og."""

    users: list[UserInfo] = field(default_factory=list)
    has_more: bool = False
    cursor: str = "0"

    @classmethod
    def from_tiktok(cls, d: Optional[dict]) -> "UserFeed":
        """/api/user/list/ ({userList:[{user,stats,statsV2}], hasMore, minCursor})."""
        d = _d(d)
        users = [UserInfo.from_tiktok(u) for u in _l(d.get("userList"))]
        cursor = _s(d.get("minCursor") or d.get("cursor")) or "0"
        return cls(users=users, has_more=_b(d.get("hasMore")), cursor=cursor)


__all__ = [
    "OgMeta", "Stats", "VideoInfo", "ImagePost", "Music", "Author", "TextExtra", "Challenge",
    "CommentUser", "Comment", "Item", "UserDetail", "UserStats", "UserInfo", "TagStats", "TagInfo",
    "MusicStats", "MusicInfo", "Feed", "UserFeed",
]
