"""P4 unit tests: YouTube Data API v3 collector (mocked only, zero network calls)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from collectors.runner import run_collect
from collectors.youtube import (
    CHANNELS_URL,
    COMMENT_THREADS_URL,
    PLAYLIST_ITEMS_URL,
    YouTubeCollector,
    is_youtube_spam_or_bot,
    match_youtube_comment,
)
from collectors.matcher import KeywordMatcher
from core.schemas import RawPost
from core.utils import require_author_salt, verify_evidence_span

FIXED_NOW = datetime(2026, 10, 26, 10, 0, 0, tzinfo=timezone.utc)
IN_WIN_ISO = "2026-09-15T10:00:00Z"
OLD_ISO = "2023-02-10T10:00:00Z"


class _FakeYTResp:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self) -> Any:
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload


def _configure_minimal_youtube(settings) -> None:
    yt = settings.sources["youtube"]
    yt["channels"] = ["Groww"]
    yt["search_queries"] = []
    yt["max_upload_pages_per_channel"] = 1
    yt["max_comment_pages_per_video"] = 1
    yt["max_comments_per_video"] = 40
    yt["max_matched_total"] = 1500
    yt["daily_quota_cap_units"] = 8000
    yt["description_lead_chars"] = 250


def _channel_payload(channel_id: str = "UC_GROWW", title: str = "Groww", uploads_id: str = "UU_GROWW") -> Dict[str, Any]:
    return {
        "items": [
            {
                "id": channel_id,
                "snippet": {"title": title, "customUrl": "@groww"},
                "statistics": {"subscriberCount": "2480000"},
                "contentDetails": {"relatedPlaylists": {"uploads": uploads_id}},
            }
        ]
    }


def _playlist_item(video_id: str, title: str, description: str = "", published_at: str = IN_WIN_ISO) -> Dict[str, Any]:
    return {
        "snippet": {
            "title": title,
            "description": description,
            "publishedAt": published_at,
            "resourceId": {"videoId": video_id},
        },
        "contentDetails": {
            "videoId": video_id,
            "videoPublishedAt": published_at,
        },
    }


def _comment_thread(
    comment_id: str,
    text: str,
    *,
    author: str = "@InvestorRahul",
    author_channel_id: str = "UC_USER_1",
    like_count: int = 10,
    published_at: str = IN_WIN_ISO,
    replies: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "id": comment_id,
        "snippet": {
            "totalReplyCount": len(replies or []),
            "topLevelComment": {
                "id": comment_id,
                "snippet": {
                    "authorDisplayName": author,
                    "authorChannelId": {"value": author_channel_id},
                    "textOriginal": text,
                    "likeCount": like_count,
                    "publishedAt": published_at,
                },
            },
        },
    }
    if replies:
        item["replies"] = {"comments": replies}
    return item


def _reply_item(
    reply_id: str,
    parent_id: str,
    text: str,
    *,
    author: str = "@ReplyPriya",
    author_channel_id: str = "UC_USER_2",
    like_count: int = 3,
    published_at: str = IN_WIN_ISO,
) -> Dict[str, Any]:
    return {
        "id": reply_id,
        "snippet": {
            "parentId": parent_id,
            "authorDisplayName": author,
            "authorChannelId": {"value": author_channel_id},
            "textOriginal": text,
            "likeCount": like_count,
            "publishedAt": published_at,
        },
    }


# ----------------------------------------------------------------------------- 1. Playlist scan & strict SIP/MF video anchor filter


def test_youtube_channel_playlist_and_strict_video_anchor_filtering(settings):
    _configure_minimal_youtube(settings)
    harvested_videos: List[str] = []

    playlist_payload = {
        "items": [
            # 1. Valid in-window video with "SIP" anchor in title -> MUST queue
            _playlist_item("vid_sip_ok", "Why 90% Beginners Stop Their Groww SIP in Year 1", "Beginner guide"),
            # 2. Valid in-window video with "mutual fund" in lead description (<250 chars) -> MUST queue
            _playlist_item("vid_mf_lead", "Investing Mistakes in Your 20s", "How first-time mutual fund investors react."),
            # 3. Video with only "portfolio", "small cap", "mid cap", "index fund" -> MUST NOT queue
            _playlist_item("vid_generic", "My Small Cap and Mid Cap Portfolio Review", "Index fund allocation for 2026."),
            # 4. Video where "Mutual Fund" only appears in boilerplate footer after 250 chars -> MUST NOT queue
            _playlist_item(
                "vid_footer_only",
                "How to File Income Tax Return",
                ("Step by step ITR filing guide for salaried employees. " * 6) + " Invest in Mutual Funds on Groww!",
            ),
            # 5. Out-of-window SIP video -> MUST NOT queue
            _playlist_item("vid_old_sip", "Old SIP Video from 2023", "SIP stopped", published_at=OLD_ISO),
        ],
        "nextPageToken": None,
    }

    def fake_get(url: str, params: Dict[str, Any], timeout: float = 20):
        if url == CHANNELS_URL:
            return _FakeYTResp(200, _channel_payload())
        if url == PLAYLIST_ITEMS_URL:
            return _FakeYTResp(200, playlist_payload)
        if url == COMMENT_THREADS_URL:
            vid = params["videoId"]
            harvested_videos.append(vid)
            return _FakeYTResp(
                200,
                {
                    "items": [
                        _comment_thread(
                            f"c_{vid}",
                            "I paused my SIP after 4 months because my bank autopay mandate failed.",
                        )
                    ]
                },
            )
        raise AssertionError(f"Unexpected URL: {url}")

    collector = YouTubeCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        http_get=fake_get,
    )
    stats = collector.run()

    assert stats["status"] == "ok"
    assert harvested_videos == ["vid_sip_ok", "vid_mf_lead"]
    assert stats["videos_queued"] == 2
    assert stats["videos_harvested"] == 2
    # Quota: 1 (channels.list) + 1 (playlistItems.list) + 2 (commentThreads.list) = 4 units
    assert stats["quota_units_this_run"] == 4
    assert stats["quota_units_today_pt"] == 4


# ----------------------------------------------------------------------------- 2. Comment + reply schema & evidence_span safety


def test_youtube_comments_replies_and_schema(settings):
    _configure_minimal_youtube(settings)

    threads_payload = {
        "items": [
            _comment_thread(
                "top_101",
                "Started my first SIP of 5000 on Groww, paused SIP in month 3 after market dip.",
                author="@PlainAuthorHandle",
                like_count=25,
                replies=[
                    _reply_item(
                        "rep_202",
                        "top_101",
                        "Don't worry, my SIP chalu hai for 1 year SIP and returns recovered.",
                        author="@PlainReplierHandle",
                        like_count=7,
                    ),
                    _reply_item(
                        "rep_old",
                        "top_101",
                        "Old SIP reply outside 24m window.",
                        published_at=OLD_ISO,
                    ),
                ],
            )
        ]
    }

    def fake_get(url: str, params: Dict[str, Any], timeout: float = 20):
        if url == CHANNELS_URL:
            return _FakeYTResp(200, _channel_payload())
        if url == PLAYLIST_ITEMS_URL:
            return _FakeYTResp(200, {"items": [_playlist_item("v_schema", "Groww SIP Beginner Guide")]})
        if url == COMMENT_THREADS_URL:
            return _FakeYTResp(200, threads_payload)
        raise AssertionError(url)

    collector = YouTubeCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        http_get=fake_get,
    )
    stats = collector.run()

    assert stats["comments_matched"] == 2
    raw_file = (settings.paths.resolve("raw_dir") / "youtube.jsonl").read_text(encoding="utf-8")
    assert "PlainAuthorHandle" not in raw_file
    assert "PlainReplierHandle" not in raw_file

    rows = {r.id: r for r in (RawPost.model_validate(json.loads(l)) for l in raw_file.splitlines())}
    assert set(rows) == {"youtube:top_101", "youtube:rep_202"}

    top = rows["youtube:top_101"]
    assert top.url == "https://www.youtube.com/watch?v=v_schema&lc=top_101"
    assert top.raw_meta["record_type"] == "comment"
    assert top.raw_meta["parent_id"] is None
    assert top.raw_meta["video_id"] == "v_schema"
    assert top.raw_meta["video_title"] == "Groww SIP Beginner Guide"
    assert top.raw_meta["channel_title"] == "Groww"
    assert top.raw_meta["channel_handle"] == "Groww"
    assert top.raw_meta["like_count"] == 25
    assert top.raw_meta["reply_count"] == 2
    # Video title is never prepended to comment text
    assert verify_evidence_span("Groww SIP Beginner Guide", top.text) is False

    rep = rows["youtube:rep_202"]
    assert rep.raw_meta["record_type"] == "reply"
    assert rep.raw_meta["parent_id"] == "top_101"
    assert rep.keyword_group == "continuer"


# ----------------------------------------------------------------------------- 3. Creator, spam/crypto nuance & disabled comments


def test_youtube_creator_spam_crypto_and_disabled_comments_filter(settings):
    _configure_minimal_youtube(settings)

    # Direct unit checks on spam helper
    assert is_youtube_spam_or_bot("@user", "Stopped my SIP and moved to crypto last year") is False
    assert is_youtube_spam_or_bot("@user", "My SIP went up +91 rupees this month") is False
    assert is_youtube_spam_or_bot("@user", "Contact me on WhatsApp +91 9876543210 for guaranteed return on SIP") is True
    assert is_youtube_spam_or_bot("@user", "DM me for crypto signals and 100% profit on mutual fund") is True

    playlist_payload = {
        "items": [
            _playlist_item("v_disabled", "SIP Mandate Guide"),
            _playlist_item("v_active", "Why Investors Stop SIP on Groww"),
        ]
    }

    threads_payload = {
        "items": [
            # 1. Creator pinned comment (authorChannelId == UC_GROWW) -> dropped as creator_or_channel_owner
            _comment_thread(
                "c_creator",
                "Start your first SIP on Groww today!",
                author="@Groww",
                author_channel_id="UC_GROWW",
                like_count=500,
            ),
            # 2. WhatsApp / Telegram / link spam -> dropped as bot_or_spam
            _comment_thread(
                "c_spam_wa",
                "I recovered my SIP loss! Message me on WhatsApp for guaranteed return.",
                author="@ScamBot",
                like_count=50,
            ),
            # 3. Timestamp-only short comment (<3 substantive words) -> dropped as too_short_or_timestamp
            _comment_thread(
                "c_ts_short",
                "04:15 12:30 SIP",
                author="@ShortUser",
                like_count=10,
            ),
            # 4. Genuine comment mentioning "crypto" without promo cues -> MUST BE KEPT
            _comment_thread(
                "c_real_crypto",
                "I stopped my SIP after 6 months and moved to crypto because NIFTY returns felt slow.",
                author="@RealInvestor",
                like_count=15,
            ),
        ]
    }

    def fake_get(url: str, params: Dict[str, Any], timeout: float = 20):
        if url == CHANNELS_URL:
            return _FakeYTResp(200, _channel_payload(channel_id="UC_GROWW"))
        if url == PLAYLIST_ITEMS_URL:
            return _FakeYTResp(200, playlist_payload)
        if url == COMMENT_THREADS_URL:
            if params["videoId"] == "v_disabled":
                return _FakeYTResp(
                    403,
                    {
                        "error": {
                            "code": 403,
                            "message": "The video identified by the videoId parameter has disabled comments.",
                            "errors": [{"reason": "commentsDisabled"}],
                        }
                    },
                )
            return _FakeYTResp(200, threads_payload)
        raise AssertionError(url)

    collector = YouTubeCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        http_get=fake_get,
    )
    stats = collector.run()

    assert stats["status"] == "ok"
    assert stats["dropped"]["comments_disabled"] == 1
    assert stats["dropped"]["creator_or_channel_owner"] == 1
    assert stats["dropped"]["bot_or_spam"] == 1
    assert stats["dropped"]["too_short_or_timestamp"] == 1
    assert stats["comments_matched"] == 1

    lines = (settings.paths.resolve("raw_dir") / "youtube.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    kept = RawPost.model_validate(json.loads(lines[0]))
    assert kept.id == "youtube:c_real_crypto"
    assert "moved to crypto" in kept.text


# ----------------------------------------------------------------------------- 4. 40-comment per-video cap & matched_via


def test_youtube_per_video_cap_and_matched_via(settings):
    _configure_minimal_youtube(settings)
    settings.sources["youtube"]["max_comments_per_video"] = 40

    items = [
        # 2 thread_context matches (no "SIP" word, but video has SIP anchor and comments match continuer/hinglish phrases)
        _comment_thread(
            "c_ctx_1",
            "Market kitna bhi gire maine abhi tak band nahi kiya, holding for long term.",
            like_count=200,
        ),
        _comment_thread(
            "c_ctx_2",
            "Started 10 months ago with 2000 per month and still going strong without panic.",
            like_count=190,
        ),
        # 1 generic praise comment -> rejected
        _comment_thread(
            "c_praise",
            "Very nice explanation sir, thank you for making this video for beginners!",
            like_count=999,
        ),
    ]
    # Add 43 direct "self" matches with like_count 1..43 -> total matched = 2 + 43 = 45 -> 5 capped!
    for i in range(1, 44):
        items.append(
            _comment_thread(
                f"c_self_{i}",
                f"My Groww SIP installment #{i} was deducted on time this month.",
                like_count=i,
            )
        )

    def fake_get(url: str, params: Dict[str, Any], timeout: float = 20):
        if url == CHANNELS_URL:
            return _FakeYTResp(200, _channel_payload())
        if url == PLAYLIST_ITEMS_URL:
            return _FakeYTResp(200, {"items": [_playlist_item("v_cap", "Groww SIP Review After 1 Year")]})
        if url == COMMENT_THREADS_URL:
            return _FakeYTResp(200, {"items": items})
        raise AssertionError(url)

    collector = YouTubeCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        http_get=fake_get,
    )
    stats = collector.run()

    assert stats["comments_matched"] == 40
    assert stats["comments_capped"] == 5
    assert stats["dropped"]["video_comment_cap"] == 5
    assert stats["matched_via"] == {"self": 38, "thread_context": 2}

    rows = {
        r.id: r
        for r in (
            RawPost.model_validate(json.loads(l))
            for l in (settings.paths.resolve("raw_dir") / "youtube.jsonl").read_text(encoding="utf-8").splitlines()
        )
    }
    assert "youtube:c_praise" not in rows
    assert rows["youtube:c_ctx_1"].raw_meta["matched_via"] == "thread_context"
    assert rows["youtube:c_ctx_2"].raw_meta["matched_via"] == "thread_context"
    assert rows["youtube:c_self_43"].raw_meta["matched_via"] == "self"
    # Lowest 5 like_counts (1..5) were capped
    for low_i in range(1, 6):
        assert f"youtube:c_self_{low_i}" not in rows
    for kept_i in range(6, 44):
        assert f"youtube:c_self_{kept_i}" in rows


# ----------------------------------------------------------------------------- 5. Quota exhaustion & idempotent next-day resume


def test_youtube_quota_exhaustion_and_idempotent_resume(settings):
    _configure_minimal_youtube(settings)

    playlist_payload = {
        "items": [
            _playlist_item("v_day1", "First SIP on Groww"),
            _playlist_item("v_day2", "Why People Pause SIP"),
        ]
    }

    def day1_get(url: str, params: Dict[str, Any], timeout: float = 20):
        if url == CHANNELS_URL:
            return _FakeYTResp(200, _channel_payload())
        if url == PLAYLIST_ITEMS_URL:
            return _FakeYTResp(200, playlist_payload)
        if url == COMMENT_THREADS_URL:
            if params["videoId"] == "v_day1":
                return _FakeYTResp(
                    200,
                    {"items": [_comment_thread("c_d1", "I paused my SIP in month 4 due to rent hike.")]},
                )
            if params["videoId"] == "v_day2":
                return _FakeYTResp(
                    403,
                    {
                        "error": {
                            "code": 403,
                            "message": "The request cannot be completed because you have exceeded your quota.",
                            "errors": [{"reason": "quotaExceeded"}],
                        }
                    },
                )
        raise AssertionError(url)

    c1 = YouTubeCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        http_get=day1_get,
    )
    s1 = c1.run()
    assert s1["status"] == "ok"
    assert s1["quota_exhausted"] is True
    assert s1["videos_done"] == 1
    assert s1["videos_pending"] == 1
    assert s1["comments_matched"] == 1

    # Day 2: resumes directly on pending video v_day2 without re-calling channels.list, playlistItems.list, or v_day1
    day2_calls: List[str] = []

    def day2_get(url: str, params: Dict[str, Any], timeout: float = 20):
        day2_calls.append(f"{url}:{params.get('videoId', '')}")
        if url == COMMENT_THREADS_URL and params.get("videoId") == "v_day2":
            return _FakeYTResp(
                200,
                {"items": [_comment_thread("c_d2", "My SIP is still going after 1 year SIP on Groww.")]},
            )
        raise AssertionError(f"Unexpected call on resume: {url} {params}")

    c2 = YouTubeCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=datetime(2026, 10, 27, 10, 0, 0, tzinfo=timezone.utc),
        http_get=day2_get,
    )
    s2 = c2.run()
    assert s2["status"] == "ok"
    assert s2["quota_exhausted"] is False
    assert day2_calls == [f"{COMMENT_THREADS_URL}:v_day2"]
    assert s2["videos_done"] == 2
    assert s2["videos_pending"] == 0
    assert s2["total_in_file"] == 2


# ----------------------------------------------------------------------------- 6. Missing or placeholder AUTHOR_HASH_SALT refuses to run


@pytest.mark.parametrize(
    "bad_salt",
    [
        "",
        "   ",
        "short",
        "your-random-salt-here",
        "your_salt_12345",
        "<insert-salt-here>",
        "changeme-please",
        "groww-sip-default-salt",
        "placeholder-salt-value",
    ],
)
def test_missing_or_placeholder_salt_refuses_to_run(settings, monkeypatch, bad_salt):
    _configure_minimal_youtube(settings)
    monkeypatch.setenv("AUTHOR_HASH_SALT", bad_salt)

    with pytest.raises(ValueError, match="AUTHOR_HASH_SALT"):
        require_author_salt()

    called = {"count": 0}

    def should_not_be_called(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("Network must never be called when AUTHOR_HASH_SALT is invalid")

    with pytest.raises(ValueError, match="AUTHOR_HASH_SALT"):
        YouTubeCollector(
            settings,
            raw_dir=settings.paths.resolve("raw_dir"),
            checkpoint_dir=settings.paths.resolve("checkpoints"),
            now=FIXED_NOW,
            http_get=should_not_be_called,
        )

    with pytest.raises(ValueError, match="AUTHOR_HASH_SALT"):
        run_collect(settings, sources=["youtube"], now=FIXED_NOW)

    assert called["count"] == 0


# ----------------------------------------------------------------------------- 7. Round-robin channel rotation & per-channel cap


def test_youtube_round_robin_channel_rotation_and_per_channel_cap(settings):
    _configure_minimal_youtube(settings)
    settings.sources["youtube"]["channels"] = ["Groww", "ETMONEY", "warikoo"]
    settings.sources["youtube"]["max_comments_per_channel"] = 3

    harvested_order: List[str] = []

    def fake_get(url: str, params: Dict[str, Any], timeout: float = 20):
        if url == CHANNELS_URL:
            h = params["forHandle"].lstrip("@")
            return _FakeYTResp(200, _channel_payload(channel_id=f"UC_{h}", title=h, uploads_id=f"UU_{h}"))
        if url == PLAYLIST_ITEMS_URL:
            pl = params["playlistId"]
            prefix = {"UU_Groww": "g", "UU_ETMONEY": "e", "UU_warikoo": "w"}[pl]
            return _FakeYTResp(
                200,
                {
                    "items": [
                        _playlist_item(f"{prefix}_v1", f"{prefix} SIP video 1"),
                        _playlist_item(f"{prefix}_v2", f"{prefix} SIP video 2"),
                        _playlist_item(f"{prefix}_v3", f"{prefix} SIP video 3"),
                    ]
                },
            )
        if url == COMMENT_THREADS_URL:
            vid = params["videoId"]
            harvested_order.append(vid)
            # Each video returns 2 matching comments; since max_comments_per_channel=3,
            # video 1 of each channel keeps 2 comments, video 2 keeps 1 (1 capped), and video 3 is pruned!
            return _FakeYTResp(
                200,
                {
                    "items": [
                        _comment_thread(f"{vid}_c1", "I paused my SIP on Groww last month.", like_count=20),
                        _comment_thread(f"{vid}_c2", "My mutual fund SIP installment failed.", like_count=10),
                    ]
                },
            )
        raise AssertionError(url)

    collector = YouTubeCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        scan_limit=6,
        http_get=fake_get,
    )
    stats = collector.run()

    assert stats["status"] == "ok"
    # Videos are harvested in strict round-robin order across Groww, ETMONEY, warikoo
    assert harvested_order == ["g_v1", "e_v1", "w_v1", "g_v2", "e_v2", "w_v2"]
    # Each channel was capped at 3 matched comments (2 from v1 + 1 from v2; 1 capped per channel = 3 capped total)
    assert stats["matched_by_channel"] == {"Groww": 3, "ETMONEY": 3, "warikoo": 3}
    assert stats["comments_matched"] == 9
    assert stats["comments_capped_channel"] == 3
    assert stats["dropped"]["channel_comment_cap"] == 3


# ----------------------------------------------------------------------------- 8. YouTube first-person + behaviour thread_context rule


def test_youtube_first_person_behaviour_thread_context_matching(settings):
    _configure_minimal_youtube(settings)
    matcher = KeywordMatcher.from_settings(settings.keywords)

    # 1. First-person + drop-off behaviour (no "SIP" word) -> matches on YouTube via thread_context
    m1 = match_youtube_comment("I stopped after 4 months because of a medical emergency.", matcher)
    assert m1.matched is True
    assert m1.matched_via == "thread_context"
    assert m1.keyword_group == "core"
    assert "i" in m1.matched_keywords and "stopped" in m1.matched_keywords

    # 2. Hinglish first-person ("mera", "mujhe") + behaviour ("salary", "redeem", "loss") -> matches via thread_context
    m2 = match_youtube_comment("Mera salary late aaya toh mujhe loss mein redeem karna pada.", matcher)
    assert m2.matched is True
    assert m2.matched_via == "thread_context"
    assert "mera" in m2.matched_keywords and "redeem" in m2.matched_keywords

    # 3. First-person + continuer behaviour ("continuing", "years") -> matches as continuer via thread_context
    m3 = match_youtube_comment("Maine 3 years pehle start kiya tha and I am still continuing.", matcher)
    assert m3.matched is True
    assert m3.matched_via == "thread_context"
    assert m3.keyword_group == "continuer"

    # 4. First-person ONLY (no behaviour term) -> must NOT match
    assert match_youtube_comment("I really loved your explanation in this video sir!", matcher).matched is False

    # 5. Behaviour term ONLY (no first-person marker) -> must NOT match
    assert match_youtube_comment("People usually stop after a few months when returns fall.", matcher).matched is False

    # 6. Reddit / Store KeywordMatcher remains untouched (does NOT match first-person + behaviour without SIP/phrase)
    assert matcher.match("I stopped after 4 months because of a medical emergency.").matched is False
    assert matcher.match_with_context(
        "I stopped after 4 months because of a medical emergency.",
        thread_has_anchor=True,
    ).matched is False

