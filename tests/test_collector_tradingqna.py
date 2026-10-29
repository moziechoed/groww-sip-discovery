"""Mocked unit tests for P5 Part 1: TradingQ&A Discourse collector (`collectors/tradingqna.py`).

Zero live network calls and zero LLM calls.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

import pytest

from collectors.tradingqna import (
    TradingQnACollector,
    clean_discourse_cooked,
    extract_discourse_like_count,
)
from core.config import load_config

FIXED_NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status_code: int, payload: Any, headers: Optional[Dict[str, str]] = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeDiscourseSession:
    """Mock `requests.Session` returning canned responses for `/search.json` and `/t/{id}.json`."""

    def __init__(self, routes: Dict[str, List[FakeResponse]]):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.headers: Dict[str, str] = {}
        self.calls: List[str] = []

    def get(self, url: str, timeout: float = 20.0, **kwargs) -> FakeResponse:
        self.calls.append(url)
        decoded = unquote(url)
        for pattern, queue in self.routes.items():
            if pattern in url or pattern in decoded:
                if len(queue) > 1:
                    return queue.pop(0)
                return queue[0]
        return FakeResponse(404, {"errors": ["Not found"]})


def _make_settings(overrides: Optional[Dict[str, Any]] = None):
    settings = load_config()
    cfg = dict(settings.sources.get("tradingqna", {}))
    cfg.update(
        {
            "enabled": True,
            "queries": ["SIP mandate"],
            "max_search_pages_per_query": 1,
            "max_replies_per_topic": 20,
            "max_matched_total": 400,
            "request_delay_sec": 6.0,
            "rate_limit_cooldown_sec": 60.0,
            "platform_tag": "zerodha",
        }
    )
    if overrides:
        cfg.update(overrides)
    settings.sources["tradingqna"] = cfg
    return settings


def test_clean_discourse_cooked_strips_quote_asides_and_prevents_false_match():
    """1. Quote-aside stripping: `<aside class="quote">...</aside>` containing 'SIP stopped' is stripped
    so an unrelated reply body does not false-match on quoted OP text."""
    cooked = (
        '<aside class="quote no-group" data-username="op_user" data-post="1" data-topic="100">'
        "<div class=\"title\">op_user:</div>"
        "<blockquote><p>My Coin SIP stopped after mandate failed last month.</p></blockquote>"
        "</aside>"
        "<p>Check the ticket number with customer support team.</p>"
    )
    cleaned = clean_discourse_cooked(cooked)
    assert "SIP stopped" not in cleaned
    assert "mandate failed" not in cleaned
    assert cleaned == "Check the ticket number with customer support team."


def test_search_and_topic_harvest_schema_author_hash_and_platform_tag(tmp_path: Path):
    """2. Search & topic harvest: OP (`tradingqna:t_101`) and replies (`tradingqna:p_5002`, `tradingqna:p_5003`)
    carry `platform_tag = 'zerodha'`, hashed authors (never plain username), and support both `self` and
    first-person + behaviour `thread_context` matching."""
    settings = _make_settings()
    search_payload = {
        "posts": [
            {
                "id": 5001,
                "topic_id": 101,
                "post_number": 1,
                "created_at": "2026-06-15T10:00:00Z",
                "blurb": "Coin SIP mandate failed...",
            }
        ],
        "topics": [
            {
                "id": 101,
                "title": "Coin SIP mandate failed and installment missed",
                "slug": "coin-sip-mandate-failed-and-installment-missed",
                "category_id": 12,
                "posts_count": 4,
                "last_posted_at": "2026-06-16T10:00:00Z",
            }
        ],
        "grouped_search_result": {"more_full_page_results": False},
    }
    topic_payload = {
        "id": 101,
        "title": "Coin SIP mandate failed and installment missed",
        "slug": "coin-sip-mandate-failed-and-installment-missed",
        "category_id": 12,
        "posts_count": 4,
        "post_stream": {
            "posts": [
                {
                    "id": 5001,
                    "post_number": 1,
                    "post_type": 1,
                    "username": "rahul_investor",
                    "created_at": "2026-06-15T10:00:00Z",
                    "cooked": "<p>My first SIP on Coin got missed because bank autopay mandate failed.</p>",
                    "reply_count": 2,
                    "reads": 45,
                    "actions_summary": [{"id": 2, "count": 4}],
                },
                {
                    # Direct keyword match ("self")
                    "id": 5002,
                    "post_number": 2,
                    "post_type": 1,
                    "username": "priya_mf",
                    "created_at": "2026-06-15T11:00:00Z",
                    "cooked": "<p>I also paused SIP when my bank e-mandate was rejected twice.</p>",
                    "reply_to_post_number": 1,
                    "reply_count": 0,
                    "reads": 30,
                    "like_count": 7,
                },
                {
                    # First-person + behaviour match ("thread_context") without repeating "SIP"
                    "id": 5003,
                    "post_number": 3,
                    "post_type": 1,
                    "username": "amit_saver",
                    "created_at": "2026-06-15T12:00:00Z",
                    "cooked": (
                        '<aside class="quote"><blockquote><p>Coin SIP mandate failed</p></blockquote></aside>'
                        "<p>I stopped after 4 months because of emergency medical expenses and loss.</p>"
                    ),
                    "reply_to_post_number": 1,
                    "reply_count": 0,
                    "reads": 20,
                    "like_count": 3,
                },
                {
                    # Quote-only mention of SIP; body has no SIP keyword and no first-person + behaviour
                    "id": 5004,
                    "post_number": 4,
                    "post_type": 1,
                    "username": "random_helper",
                    "created_at": "2026-06-15T13:00:00Z",
                    "cooked": (
                        '<aside class="quote"><blockquote><p>My first SIP on Coin got missed</p></blockquote></aside>'
                        "<p>Please check the bank server status page tomorrow morning.</p>"
                    ),
                    "reply_to_post_number": 1,
                    "reply_count": 0,
                    "reads": 15,
                    "like_count": 1,
                },
            ]
        },
    }

    session = FakeDiscourseSession(
        {
            "/search.json": [FakeResponse(200, search_payload)],
            "/t/101.json": [FakeResponse(200, topic_payload)],
        }
    )
    sleeps: List[float] = []
    collector = TradingQnACollector(
        settings,
        raw_dir=tmp_path / "raw",
        checkpoint_dir=tmp_path / "ckpt",
        now=FIXED_NOW,
        sleep=sleeps.append,
        session=session,
    )
    stats = collector.run()
    assert stats["status"] == "ok"
    assert stats["matched"] == 3
    assert stats["topics_matched"] == 1
    assert stats["replies_matched"] == 2
    assert stats["matched_via"] == {"self": 2, "thread_context": 1}

    out_file = tmp_path / "raw" / "tradingqna.jsonl"
    rows = [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in rows] == ["tradingqna:t_101", "tradingqna:p_5002", "tradingqna:p_5003"]
    for r in rows:
        assert r["source"] == "tradingqna"
        assert r["raw_meta"]["platform_tag"] == "zerodha"
        assert len(r["author_hash"]) == 16
        assert "rahul_investor" not in json.dumps(r)
        assert "priya_mf" not in json.dumps(r)
        assert "amit_saver" not in json.dumps(r)

    assert rows[0]["raw_meta"]["kind"] == "topic"
    assert rows[0]["raw_meta"]["like_count"] == 4
    assert rows[1]["raw_meta"]["kind"] == "reply"
    assert rows[1]["raw_meta"]["matched_via"] == "self"
    assert rows[2]["raw_meta"]["kind"] == "reply"
    assert rows[2]["raw_meta"]["matched_via"] == "thread_context"


def test_per_topic_reply_cap_and_24m_window_filtering(tmp_path: Path):
    """3. Per-topic reply cap (`max_replies_per_topic`) and 24-month window filtering:
    - Replies older than 24 months are dropped as `before_window`.
    - When more than `max_replies_per_topic` replies match, keeps the top N by `(-like_count, created_at ASC)`
      and logs the rest under `per_topic_cap`."""
    settings = _make_settings({"max_replies_per_topic": 2})
    search_payload = {
        "posts": [{"id": 1, "topic_id": 202, "created_at": "2026-08-01T10:00:00Z"}],
        "topics": [{"id": 202, "title": "Paused SIP on Coin", "slug": "paused-sip-on-coin", "posts_count": 6}],
        "grouped_search_result": {"more_full_page_results": False},
    }
    topic_payload = {
        "id": 202,
        "title": "Paused SIP on Coin",
        "slug": "paused-sip-on-coin",
        "category_id": 9,
        "post_stream": {
            "posts": [
                {
                    "id": 2001,
                    "post_number": 1,
                    "post_type": 1,
                    "username": "u1",
                    "created_at": "2023-01-10T10:00:00Z",  # OP is older than 24m cutoff (2024-10-26)
                    "cooked": "<p>I paused my SIP on Coin two years ago.</p>",
                },
                {
                    "id": 2002,
                    "post_number": 2,
                    "post_type": 1,
                    "username": "u2",
                    "created_at": "2024-01-10T10:00:00Z",  # Reply also older than 24m cutoff
                    "cooked": "<p>I also stopped my SIP back then.</p>",
                    "like_count": 50,
                },
                {
                    "id": 2003,
                    "post_number": 3,
                    "post_type": 1,
                    "username": "u3",
                    "created_at": "2026-05-01T10:00:00Z",
                    "cooked": "<p>I stopped my SIP for 2 months due to salary delay.</p>",
                    "like_count": 2,
                },
                {
                    "id": 2004,
                    "post_number": 4,
                    "post_type": 1,
                    "username": "u4",
                    "created_at": "2026-05-02T10:00:00Z",
                    "cooked": "<p>I paused my SIP when markets fell 10 percent.</p>",
                    "like_count": 10,
                },
                {
                    "id": 2005,
                    "post_number": 5,
                    "post_type": 1,
                    "username": "u5",
                    "created_at": "2026-05-03T10:00:00Z",
                    "cooked": "<p>I continued my SIP and never stopped for 1 year SIP.</p>",
                    "like_count": 5,
                },
            ]
        },
    }
    session = FakeDiscourseSession(
        {
            "/search.json": [FakeResponse(200, search_payload)],
            "/t/202.json": [FakeResponse(200, topic_payload)],
        }
    )
    collector = TradingQnACollector(
        settings,
        raw_dir=tmp_path / "raw",
        checkpoint_dir=tmp_path / "ckpt",
        now=FIXED_NOW,
        sleep=lambda _: None,
        session=session,
    )
    stats = collector.run()
    assert stats["dropped"]["before_window"] == 2
    assert stats["dropped"]["per_topic_cap"] == 1
    rows = [json.loads(line) for line in (tmp_path / "raw" / "tradingqna.jsonl").read_text().splitlines()]
    # Only top 2 in-window replies kept: 2004 (like_count=10) and 2005 (like_count=5)
    assert [r["id"] for r in rows] == ["tradingqna:p_2004", "tradingqna:p_2005"]
    assert rows[1]["keyword_group"] == "continuer"


def test_http_429_enforces_minimum_60s_cooldown_and_resume_idempotency(tmp_path: Path):
    """4. HTTP 429 cooldown & idempotency:
    - When Discourse returns HTTP 429 with `Retry-After: 5`, collector still sleeps at least 60s.
    - Re-running against the same checkpoint and JSONL makes 0 duplicate records."""
    settings = _make_settings({"rate_limit_cooldown_sec": 60.0, "request_delay_sec": 6.0})
    search_payload = {
        "posts": [{"id": 3001, "topic_id": 303, "created_at": "2026-09-01T10:00:00Z"}],
        "topics": [{"id": 303, "title": "First SIP on Coin", "slug": "first-sip-on-coin", "posts_count": 1}],
        "grouped_search_result": {"more_full_page_results": False},
    }
    topic_payload = {
        "id": 303,
        "title": "First SIP on Coin",
        "slug": "first-sip-on-coin",
        "category_id": 5,
        "post_stream": {
            "posts": [
                {
                    "id": 3001,
                    "post_number": 1,
                    "post_type": 1,
                    "username": "new_sip_user",
                    "created_at": "2026-09-01T10:00:00Z",
                    "cooked": "<p>Started my first SIP of 5000 in an index mutual fund today.</p>",
                }
            ]
        },
    }
    session = FakeDiscourseSession(
        {
            "/search.json": [
                FakeResponse(429, {"errors": ["Slow down"]}, headers={"Retry-After": "5"}),
                FakeResponse(200, search_payload),
            ],
            "/t/303.json": [FakeResponse(200, topic_payload)],
        }
    )
    sleeps: List[float] = []
    collector = TradingQnACollector(
        settings,
        raw_dir=tmp_path / "raw",
        checkpoint_dir=tmp_path / "ckpt",
        now=FIXED_NOW,
        sleep=sleeps.append,
        session=session,
    )
    stats1 = collector.run()
    assert stats1["status"] == "ok"
    assert stats1["written_this_run"] == 1
    assert any(s >= 60.0 for s in sleeps), f"Expected at least one >=60s cooldown sleep, got {sleeps}"

    # Second run with same state + JSONL should do 0 new requests and write 0 duplicates
    session2 = FakeDiscourseSession({})
    collector2 = TradingQnACollector(
        settings,
        raw_dir=tmp_path / "raw",
        checkpoint_dir=tmp_path / "ckpt",
        now=FIXED_NOW,
        sleep=lambda _: None,
        session=session2,
    )
    stats2 = collector2.run()
    assert stats2["status"] == "ok"
    assert stats2["written_this_run"] == 0
    assert stats2["total_in_file"] == 1
    assert len(session2.calls) == 0
