"""P1 unit tests: Play Store + App Store collectors and keyword matcher (mocked only, zero network calls)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from collectors.app_store import AppStoreCollector, compose_text
from collectors.matcher import KeywordMatcher
from collectors.play_store import PlayStoreCollector
from collectors.runner import collate, run_collect
from core.schemas import RawPost
from core.utils import verify_evidence_span

FIXED_NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)


def _gp_item(
    review_id: str,
    content: str,
    at: datetime,
    score: int = 1,
    user_name: str = "Rahul Sharma",
    reply_content: str | None = None,
) -> Dict[str, Any]:
    return {
        "reviewId": review_id,
        "userName": user_name,
        "content": content,
        "score": score,
        "thumbsUpCount": 4,
        "reviewCreatedVersion": "18.20.0",
        "at": at,
        "replyContent": reply_content,
        "repliedAt": at if reply_content else None,
        "appVersion": "18.20.0",
    }


def _rss_entry(
    entry_id: str,
    title: str,
    content: str,
    updated: str,
    rating: int = 4,
    author: str = "Priya Verma",
) -> Dict[str, Any]:
    return {
        "id": {"label": entry_id},
        "title": {"label": title},
        "content": {"label": content},
        "updated": {"label": updated},
        "im:rating": {"label": str(rating)},
        "im:version": {"label": "18.22.0"},
        "im:voteSum": {"label": "2"},
        "im:voteCount": {"label": "3"},
        "author": {"name": {"label": author}},
    }


class _FakeHTTPResponse:
    def __init__(self, status_code: int, payload: Dict[str, Any]):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


# ----------------------------------------------------------------------------- 1. Whole-word & co-occurrence matcher


def test_whole_word_keyword_matching(settings):
    matcher = KeywordMatcher.from_settings(settings.keywords)

    # Whole-word boundaries: "nav" must not match "navigation", "sip" must not match "gossip"
    assert matcher.match("App navigation is smooth and gossip section is fun").matched is False

    # Standalone anchors match on their own
    m_nav = matcher.match("Where do I check the NAV of my fund on Groww?")
    assert m_nav.matched is True
    assert "nav" in m_nav.matched_keywords
    assert m_nav.keyword_group == "core"

    m_autopay = matcher.match("My auto-pay failed this month and instalment was skipped")
    assert m_autopay.matched is True
    assert "auto pay" in m_autopay.matched_keywords
    assert "instalment" in m_autopay.matched_keywords

    # Hinglish phrases that already include "sip" match on their own
    m_sip_band = matcher.match("Maine apna sip bandh kar diya last month")
    assert m_sip_band.matched is True
    assert "sip" in m_sip_band.matched_keywords
    assert "sip bandh" in m_sip_band.matched_keywords
    assert m_sip_band.keyword_group == "core"

    # Generic Hinglish phrases ("band kar diya", "band nahi kiya", "paisa kat") DO NOT match alone
    assert matcher.match("Mera trading account band kar diya without warning").matched is False
    assert matcher.match("F&O intraday me mera paisa kat gaya").matched is False
    assert matcher.match("Option selling abhi tak band nahi kiya").matched is False

    # Generic Hinglish phrases DO match when a SIP/MF anchor is in the same review
    m_hinglish_anchor = matcher.match("Mutual fund me mera paisa kat gaya par units allot nahi hue")
    assert m_hinglish_anchor.matched is True
    assert "mutual fund" in m_hinglish_anchor.matched_keywords
    assert "paisa kat" in m_hinglish_anchor.matched_keywords
    assert m_hinglish_anchor.keyword_group == "core"

    # Generic continuer phrases ("still going", "band nahi kiya") require a SIP/MF anchor
    assert matcher.match("Good app, still going strong for stock trading").matched is False
    m_cont = matcher.match("Market girne ke baad bhi mera SIP chalu hai, band nahi kiya")
    assert m_cont.matched is True
    assert m_cont.keyword_group == "continuer"
    assert "band nahi kiya" in m_cont.matched_keywords

    # Continuer phrase that contains "sip" ("kept my SIP", "1 year SIP") matches standalone as continuer
    m_cont_direct = matcher.match("Completed my 1 year SIP on Groww smoothly!")
    assert m_cont_direct.matched is True
    assert m_cont_direct.keyword_group == "continuer"


# ----------------------------------------------------------------------------- 2. Play Store pagination, cutoff & per-star stats


def test_play_store_pagination_and_24m_cutoff(settings):
    settings.sources["play_store"]["stars"] = [1, 5]
    settings.collect.old_streak_stop = 2

    in_win_1 = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
    in_win_2 = datetime(2025, 6, 15, 9, 0, tzinfo=timezone.utc)
    old_dt = datetime(2023, 5, 1, 9, 0, tzinfo=timezone.utc)  # >24 months before Oct 2026

    # Star 1 has 2 pages: page 1 has 1 match + 1 non-match; page 2 has 2 old reviews -> triggers window_cutoff
    # Star 5 has 1 page with a continuer match and no continuation token
    pages = {
        (1, None): (
            [
                _gp_item("r1", "Paused my SIP because autopay mandate failed", in_win_1, score=1),
                _gp_item("r2", "Stocks chart is lagging", in_win_1, score=1),
            ],
            SimpleNamespace(token="tok-star1-p2"),
        ),
        (1, "tok-star1-p2"): (
            [
                _gp_item("r3", "Old SIP review 1", old_dt, score=1),
                _gp_item("r4", "Old SIP review 2", old_dt, score=1),
                _gp_item("r5", "Should not even be scanned after old_streak_stop", in_win_2, score=1),
            ],
            SimpleNamespace(token="tok-star1-p3-never-called"),
        ),
        (5, None): (
            [
                _gp_item("r6", "Kept my SIP going for 1 year SIP anniversary", in_win_2, score=5),
            ],
            SimpleNamespace(token=None),
        ),
    }

    def fake_fetch(app_id, **kwargs):
        ct = kwargs.get("continuation_token")
        key = (ct.filter_score_with, ct.token) if ct is not None else (kwargs["filter_score_with"], None)
        return pages[key]

    collector = PlayStoreCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        sleep=lambda _: None,
        fetch=fake_fetch,
    )
    stats = collector.run()

    assert stats["status"] == "ok"
    assert stats["matched"] == 2
    assert stats["per_star"]["1"] == {"scanned": 4, "matched": 1, "end_reason": "window_cutoff"}
    assert stats["per_star"]["5"] == {"scanned": 1, "matched": 1, "end_reason": "end_of_reviews"}


# ----------------------------------------------------------------------------- 3. Play Store schema, author hash & reply exclusion


def test_play_store_schema_and_author_hash(settings):
    settings.sources["play_store"]["stars"] = [2]
    dt = datetime(2026, 9, 1, 10, 30, tzinfo=timezone.utc)

    def fake_fetch(app_id, **kwargs):
        return (
            [
                _gp_item(
                    "rev-42",
                    "I stopped my mutual fund installment due to confusion in XIRR.",
                    dt,
                    score=2,
                    user_name="SecretUserName123",
                    reply_content="Hi user, sorry for the SIP inconvenience! Contact support@groww.in",
                )
            ],
            SimpleNamespace(token=None),
        )

    collector = PlayStoreCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        sleep=lambda _: None,
        fetch=fake_fetch,
    )
    collector.run()

    lines = (settings.paths.resolve("raw_dir") / "play_store.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    post = RawPost.model_validate(json.loads(lines[0]))
    assert post.id == "play_store:rev-42"
    assert post.source == "play_store"
    assert len(post.author_hash) == 16
    assert "SecretUserName123" not in lines[0]
    assert "support@groww.in" not in post.text  # developer replyContent is never in text
    assert post.raw_meta["has_developer_reply"] is True
    assert post.raw_meta["score"] == 2


# ----------------------------------------------------------------------------- 4. App Store RSS parsing, title+body & shortfall note


def test_app_store_rss_parsing_and_shortfall_log(settings):
    page1_payload = {
        "feed": {
            "entry": [
                {"im:name": {"label": "Groww"}},  # metadata entry without im:rating -> skipped
                _rss_entry(
                    "a1",
                    "Stopped my SIP",
                    "Auto pay mandate failed twice so I cancelled it.",
                    "2026-09-10T08:00:00-07:00",
                    rating=2,
                    author="AnanyaK",
                ),
                _rss_entry(
                    "a2",
                    "SIP returns are good",
                    "SIP returns are good and I kept my SIP running for 10 months.",
                    "2026-08-01T08:00:00Z",
                    rating=5,
                    author="VikramS",
                ),
            ]
        }
    }

    def fake_get(url, timeout=20):
        if "/page=1/" in url:
            return _FakeHTTPResponse(200, page1_payload)
        return _FakeHTTPResponse(200, {"feed": {}})

    collector = AppStoreCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        sleep=lambda _: None,
        http_get=fake_get,
    )
    stats = collector.run()

    assert stats["status"] == "ok"
    assert stats["scanned"] == 2
    assert stats["matched"] == 2
    assert stats["shortfall_vs_cap"] == 498
    assert "500" in stats["shortfall_note"]

    rows = [
        RawPost.model_validate(json.loads(line))
        for line in (settings.paths.resolve("raw_dir") / "app_store.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    by_id = {r.id: r for r in rows}

    # a1: title not in body -> "{title}. {content}" and evidence_span from title verifies against final text
    assert by_id["app_store:a1"].text == "Stopped my SIP. Auto pay mandate failed twice so I cancelled it."
    assert by_id["app_store:a1"].raw_meta["title"] == "Stopped my SIP"
    assert verify_evidence_span("Stopped my SIP", by_id["app_store:a1"].text) is True

    # a2: title already in body -> not duplicated; tagged as continuer
    assert by_id["app_store:a2"].text == "SIP returns are good and I kept my SIP running for 10 months."
    assert by_id["app_store:a2"].keyword_group == "continuer"


# ----------------------------------------------------------------------------- 5. Idempotent resume & deduplication


def test_collector_idempotent_resume(settings):
    settings.sources["play_store"]["stars"] = [1]
    dt = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

    # First run crashes on page 2 after writing page 1
    calls = {"n": 0}

    def flaky_fetch(app_id, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                [_gp_item("id-1", "My SIP instalment failed on Groww", dt, score=1)],
                SimpleNamespace(token="p2"),
            )
        raise RuntimeError("simulated network drop")

    c1 = PlayStoreCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        sleep=lambda _: None,
        fetch=flaky_fetch,
    )
    s1 = c1.run()
    assert s1["status"] == "failed"
    assert s1["total_in_file"] == 1

    # Second run resumes from saved token "p2", receives overlapping "id-1" + new "id-2"
    def recovery_fetch(app_id, **kwargs):
        ct = kwargs.get("continuation_token")
        assert ct is not None and ct.token == "p2"
        return (
            [
                _gp_item("id-1", "My SIP instalment failed on Groww", dt, score=1),
                _gp_item("id-2", "Cancelled my mutual fund SIP after 3 months", dt, score=1),
            ],
            SimpleNamespace(token=None),
        )

    c2 = PlayStoreCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        sleep=lambda _: None,
        fetch=recovery_fetch,
    )
    s2 = c2.run()
    assert s2["status"] == "ok"
    assert s2["written_this_run"] == 1
    assert s2["total_in_file"] == 2

    lines = (settings.paths.resolve("raw_dir") / "play_store.jsonl").read_text(encoding="utf-8").splitlines()
    ids = [json.loads(l)["id"] for l in lines]
    assert ids == ["play_store:id-1", "play_store:id-2"]


# ----------------------------------------------------------------------------- 6. Source isolation (FR1.5) + collation


def test_collector_isolation_on_error(settings):
    settings.collect.max_retries = 1

    def broken_play(app_id, **kwargs):
        raise RuntimeError("Play Store blocked")

    def working_app_store(url, timeout=20):
        if "/page=1/" in url:
            return _FakeHTTPResponse(
                200,
                {
                    "feed": {
                        "entry": [
                            _rss_entry(
                                "ok-1",
                                "First SIP experience",
                                "Started my first SIP on Groww 6 months ago.",
                                "2026-09-15T10:00:00Z",
                                rating=4,
                            )
                        ]
                    }
                },
            )
        return _FakeHTTPResponse(200, {"feed": {}})

    rc = run_collect(
        settings,
        sources=["play_store", "app_store"],
        now=FIXED_NOW,
        sleep=lambda _: None,
        collector_kwargs={
            "play_store": {"fetch": broken_play},
            "app_store": {"http_get": working_app_store},
        },
    )

    # Returns non-zero because one source failed, but app_store still ran and collated into raw_posts.jsonl
    assert rc == 1
    collated = (settings.paths.resolve("raw_dir") / "raw_posts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(collated) == 1
    assert json.loads(collated[0])["id"] == "app_store:ok-1"

    stats = json.loads(settings.paths.resolve("corpus_stats").read_text(encoding="utf-8"))
    assert stats["collect"]["sources"]["play_store"]["status"] == "failed"
    assert stats["collect"]["sources"]["app_store"]["status"] == "ok"
    assert stats["collect"]["raw_posts_total"] == 1
