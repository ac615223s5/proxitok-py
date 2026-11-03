"""Normalization tests against live-captured fixtures (tests/fixtures, captured 2026-11-03)."""
import json
from pathlib import Path

import pytest

from proxitok.scraper.errors import ScraperError
from proxitok.scraper.models import (
    Comment, Feed, ImagePost, Item, MusicInfo, OgMeta, TagInfo, TextExtra, UserFeed, UserInfo,
)

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


# ---------------------------------------------------------------- user


def test_user_info_from_hydration():
    scope = load("user_tiktok")["webapp.user-detail"]
    info = UserInfo.from_tiktok(scope["userInfo"], scope["shareMeta"])
    d = info.detail
    assert d.unique_id == "tiktok"
    assert d.nickname == "TikTok"
    assert d.verified is True
    assert d.private_account is False
    assert d.sec_uid.startswith("MS4wLjABAAAA")
    assert d.avatar_larger.startswith("https://")
    assert d.avatar_thumb.startswith("https://")
    assert d.signature
    assert d.bio_link == "tiktok.com/discovermore"
    assert d.commerce_user is True and d.category == "Others"
    # statsV2 exact numbers preferred over rounded stats
    assert info.stats.follower_count == 96555924
    assert info.stats.heart_count == 466847641
    assert info.stats.video_count == 1514
    assert info.stats.following_count == 1
    assert info.og.title == "TikTok on TikTok"
    assert "Followers" in info.og.description


def test_user_info_accepts_whole_scope():
    scope = load("user_tiktok")["webapp.user-detail"]
    info = UserInfo.from_tiktok(scope)
    assert info.detail.unique_id == "tiktok"
    assert info.og.title == "TikTok on TikTok"


def test_user_info_tolerates_empty():
    info = UserInfo.from_tiktok({})
    assert info.detail.unique_id == "" and info.detail.bio_link is None
    assert info.stats.follower_count == 0
    assert UserInfo.from_tiktok(None).og == OgMeta()


# ---------------------------------------------------------------- video item


def _video_struct(name="video_tiktok"):
    return load(name)["webapp.video-detail"]["itemInfo"]["itemStruct"]


def test_video_item_from_hydration():
    item = Item.from_tiktok(_video_struct())
    assert item.id == "7702947070604332319"
    assert item.create_time == 1793482137  # string in hydration -> int
    assert item.video.play_addr.startswith("https://")
    assert item.video.download_addr.startswith("https://")
    assert item.video.origin_cover and item.video.dynamic_cover and item.video.cover
    assert item.video.duration == 43
    assert item.has_video and not item.is_image_post and item.image_post is None
    assert item.author.unique_id == "tiktok" and item.author.verified
    assert item.music.title == "original sound"
    assert item.music.play_url.startswith("https://")
    assert item.stats.play_count == 357300
    assert item.stats.digg_count == 5078
    assert item.stats.comment_count == 1522
    assert item.stats.share_count == 396
    assert item.duet_enabled and item.stitch_enabled and item.share_enabled
    assert item.comments == []


def test_text_extra_utf16_offsets():
    s = _video_struct()
    item = Item.from_tiktok(s)
    assert len(item.text_extra) == 1
    te = item.text_extra[0]
    assert te.type == TextExtra.USER
    assert te.user_unique_id == "dadsocial"
    # desc has two astral emoji before the mention -> offsets are UTF-16 code units
    assert te.span(item.desc) == "@Mason Smith"
    assert item.desc[te.start:te.end] != "@Mason Smith"


def test_text_extra_hashtag_and_out_of_range():
    te = TextExtra.from_tiktok({"type": 1, "start": 4, "end": 8, "hashtagName": "fyp"})
    assert te.type == TextExtra.HASHTAG and te.hashtag_name == "fyp"
    assert te.span("hi! #fyp yes") == "#fyp"
    assert TextExtra(start=5, end=99).span("short") == ""


def test_author_info_for_video_page():
    s = _video_struct()
    scope = load("video_tiktok")["webapp.video-detail"]
    info = UserInfo.from_author(s["author"], s.get("authorStats"), scope["shareMeta"], s.get("authorStatsV2"))
    assert info.detail.unique_id == "tiktok"
    assert info.detail.avatar_thumb
    assert info.og.title == "TikTok on TikTok"
    assert info.stats.follower_count > 0


def test_photo_post():
    item = Item.from_tiktok(_video_struct("photo_post"))
    assert item.is_image_post and not item.has_video
    assert isinstance(item.image_post, ImagePost)
    assert len(item.image_post.images) == 2
    assert all(u.startswith("https://") for u in item.image_post.images)
    assert item.image_post.cover.startswith("https://")
    assert item.video.play_addr == ""
    assert item.video.origin_cover  # still has a cover for og:image


def test_photo_in_creator_list_detected():
    # imagePost absent / empty -> None
    assert ImagePost.from_tiktok(None) is None
    assert ImagePost.from_tiktok({"images": []}) is None


def test_item_display_flag_fallback():
    item = Item.from_tiktok({"duetDisplay": 1, "stitchDisplay": 0})
    assert item.duet_enabled is True and item.stitch_enabled is False and item.share_enabled is False


def test_item_tolerates_garbage():
    item = Item.from_tiktok({"createTime": None, "stats": None, "video": "x", "challenges": None})
    assert item.create_time == 0 and item.stats.play_count == 0 and item.video.play_addr == ""
    assert item.challenges == [] and item.text_extra == []


# ---------------------------------------------------------------- feeds


def test_creator_list_feed():
    raw = load("creator_item_list")
    feed = Feed.from_creator_list(raw)
    assert len(feed.items) == 15
    assert feed.has_more is True
    oldest = min(i["createTime"] for i in raw["itemList"])
    assert feed.cursor == str(int(oldest) * 1000)
    assert all(i.author.unique_id == "tiktok" for i in feed.items)


def test_recommend_feed():
    feed = Feed.from_tiktok(load("recommend_item_list"))
    assert len(feed.items) == 6 and feed.has_more is True and feed.cursor == "0"
    for i in feed.items:
        assert i.id and i.author.unique_id
        assert i.has_video or i.is_image_post


def test_music_feed():
    feed = Feed.from_tiktok(load("music_item_list"))
    assert len(feed.items) == 1 and feed.has_more is False and feed.cursor == "1"


def test_empty_feed():
    feed = Feed.from_tiktok({})
    assert feed.items == [] and feed.has_more is False and feed.cursor == "0"
    assert Feed.from_creator_list({}).cursor == "0"


def test_user_feed_from_user_list():
    raw = load("user_list")
    uf = UserFeed.from_tiktok(raw)
    assert len(uf.users) == len(raw["userList"]) > 0
    u = uf.users[0]
    assert u.detail.unique_id and u.detail.nickname and u.detail.avatar_larger
    assert u.stats.follower_count > 0


# ---------------------------------------------------------------- tag / music info


def test_tag_info():
    info = TagInfo.from_tiktok(load("challenge_detail"))
    assert info.detail.id == "229207" and info.detail.title == "fyp"
    assert info.detail.is_commerce is False
    assert info.detail.profile_larger == ""
    assert info.stats.view_count == 131198991980184
    assert info.stats.video_count == 9487016238
    assert info.og.title == "#fyp on TikTok"


def test_music_info_detail_shape():
    raw = {
        "musicInfo": {
            "music": {"id": "1", "title": "Song", "authorName": "Artist", "playUrl": "https://x/y.mp3", "album": ""},
            "stats": {"videoCount": 10}, "statsV2": {"videoCount": "12345"},
        },
        "shareMeta": {"title": "Song on TikTok", "desc": "d"},
    }
    info = MusicInfo.from_tiktok(raw)
    assert info.detail.title == "Song" and info.detail.play_url == "https://x/y.mp3"
    assert info.desc == "Artist" and info.stats.video_count == 12345
    assert info.og.title == "Song on TikTok"


def test_music_info_fallback_from_item():
    item = Item.from_tiktok(load("music_item_list")["itemList"][0])
    info = MusicInfo.from_music(item.music)
    assert info.detail.id == "7703578453568310029"
    assert info.detail.title


# ---------------------------------------------------------------- comments / errors


def test_comment_shape():
    c = Comment.from_tiktok({
        "cid": "1", "text": "nice", "create_time": 1700000000, "digg_count": 5, "reply_comment_total": 2,
        "user": {"unique_id": "bob", "nickname": "Bob", "avatar_thumb": {"url_list": ["https://a/b.jpg"]}},
    })
    assert (c.cid, c.text, c.create_time, c.digg_count, c.reply_count) == ("1", "nice", 1700000000, 5, 2)
    assert c.user.unique_id == "bob" and c.user.nickname == "Bob" and c.user.avatar_thumb == "https://a/b.jpg"
    assert Comment.from_tiktok({}).user.avatar_thumb == ""


@pytest.mark.parametrize("code,http", [(10204, 404), (10221, 404), (10216, 403), (-1, 502), (123456, 502)])
def test_scraper_error_codes(code, http):
    e = ScraperError.from_tiktok_code(code)
    assert e.http_code == http and e.tiktok_code == code and e.msg
