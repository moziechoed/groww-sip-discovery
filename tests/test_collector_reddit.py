"""P2 unit tests: Reddit collector (.json + .rss fallback, comments, matched_via, per-thread cap, resume)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from collectors.reddit import RedditCollector, clean_rss_html
from core.schemas import RawPost
from core.utils import verify_evidence_span

FIXED_NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
IN_WIN_TS = int(datetime(2026, 9, 15, 10, 0, 0, tzinfo=timezone.utc).timestamp())
OLD_TS = int(datetime(2023, 3, 1, 10, 0, 0, tzinfo=timezone.utc).timestamp())


class _FakeResp:
    def __init__(self, status_code: int, body: Any, headers: Optional[Dict[str, str]] = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self) -> Any:
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


def _rss_search_xml(entries_xml: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"{entries_xml}"
        "</feed>"
    )


def _rss_post_entry(
    short_id: str,
    title: str,
    body_html: str,
    published: str = "2026-09-15T10:00:00+00:00",
    author: str = "/u/investor_rahul",
    sub: str = "MutualfundsIndia",
) -> str:
    return f"""
    <entry>
      <author><name>{author}</name></author>
      <category term="{sub}" label="r/{sub}"/>
      <content type="html">&lt;!-- SC_OFF --&gt;&lt;div class="md"&gt;{body_html}&lt;/div&gt;&lt;!-- SC_ON --&gt; &amp;#32; submitted by &amp;#32; {author} [link] [comments]</content>
      <id>t3_{short_id}</id>
      <link href="https://www.reddit.com/r/{sub}/comments/{short_id}/test_slug/" />
      <updated>{published}</updated>
      <published>{published}</published>
      <title>{title}</title>
    </entry>
    """


def _rss_comment_entry(
    comment_id: str,
    post_short_id: str,
    body_html: str,
    published: str = "2026-09-15T11:00:00+00:00",
    author: str = "/u/commenter_priya",
    sub: str = "MutualfundsIndia",
) -> str:
    return f"""
    <entry>
      <author><name>{author}</name></author>
      <category term="{sub}" label="r/{sub}"/>
      <content type="html">&lt;!-- SC_OFF --&gt;&lt;div class="md"&gt;{body_html}&lt;/div&gt;&lt;!-- SC_ON --&gt;</content>
      <id>t1_{comment_id}</id>
      <link href="https://www.reddit.com/r/{sub}/comments/{post_short_id}/test_slug/{comment_id}/" />
      <updated>{published}</updated>
      <published>{published}</published>
      <title>{author} on Test Thread</title>
    </entry>
    """


def _configure_minimal_reddit(settings) -> None:
    r = settings.sources["reddit"]
    r["subs"] = ["MutualfundsIndia"]
    r["queries"] = ["SIP"]
    r["sorts"] = [{"sort": "new", "t": "all"}]
    r["max_pages_per_stream"] = 1
    r["request_delay_sec"] = 0.0
    r["rate_limit_cooldown_sec"] = 0.0


# ----------------------------------------------------------------------------- 1. .json -> .rss fallback & HTML cleanup


def test_reddit_json_and_rss_fallback_parsing(settings):
    _configure_minimal_reddit(settings)
    calls: List[str] = []

    search_xml = _rss_search_xml(
        _rss_post_entry(
            "abc123",
            "Paused my Groww SIP after 4 months",
            "&lt;p&gt;I started my first SIP of 5000 on Groww, paused it due to rent hike.&lt;/p&gt;",
            author="/u/PlainUsername99",
        )
    )
    thread_xml = _rss_search_xml(
        _rss_post_entry(
            "abc123",
            "Paused my Groww SIP after 4 months",
            "&lt;p&gt;I started my first SIP of 5000 on Groww, paused it due to rent hike.&lt;/p&gt;",
            author="/u/PlainUsername99",
        )
        + _rss_comment_entry(
            "c900",
            "abc123",
            "&lt;p&gt;My SIP chalu hai even during the dip, kept my SIP running.&lt;/p&gt;",
            author="/u/ContinuerUser",
        )
    )

    def fake_get(url: str, timeout: float = 20):
        calls.append(url)
        if ".json" in url:
            return _FakeResp(403, "<html><body>You've been blocked by network security.</body></html>")
        if "search.rss" in url:
            return _FakeResp(200, search_xml)
        if "comments/abc123/.rss" in url:
            return _FakeResp(200, thread_xml)
        raise AssertionError(f"Unexpected URL: {url}")

    collector = RedditCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        sleep=lambda _: None,
        http_get=fake_get,
    )
    stats = collector.run()

    assert stats["status"] == "ok"
    assert stats["active_format"] == "rss"
    assert stats["format_fallback"] == "json_to_rss"
    assert any(".json" in u for u in calls)
    assert any("search.rss" in u for u in calls)

    lines = (settings.paths.resolve("raw_dir") / "reddit.jsonl").read_text(encoding="utf-8").splitlines()
    rows = {r.id: r for r in (RawPost.model_validate(json.loads(l)) for l in lines)}
    assert set(rows) == {"reddit:t3_abc123", "reddit:t1_c900"}

    # Plain usernames never appear; HTML tags and "submitted by" footer are stripped
    raw_file_text = "\n".join(lines)
    assert "PlainUsername99" not in raw_file_text
    assert "ContinuerUser" not in raw_file_text
    assert "submitted by" not in rows["reddit:t3_abc123"].text
    assert "<p>" not in rows["reddit:t3_abc123"].text
    assert rows["reddit:t1_c900"].keyword_group == "continuer"


# ----------------------------------------------------------------------------- 2. Post vs comment schema & 24m cutoff


def test_reddit_post_and_comment_schema_and_24m_cutoff(settings):
    _configure_minimal_reddit(settings)

    search_json = {
        "data": {
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "id": "p_in",
                        "name": "t3_p_in",
                        "subreddit": "MutualfundsIndia",
                        "title": "Stopped my SIP on Groww",
                        "selftext": "My autopay mandate failed twice.",
                        "author": "user_one",
                        "created_utc": IN_WIN_TS,
                        "permalink": "/r/MutualfundsIndia/comments/p_in/stopped_my_sip/",
                        "score": 15,
                        "num_comments": 2,
                        "link_flair_text": "Query",
                    },
                },
                {
                    "kind": "t3",
                    "data": {
                        "id": "p_old",
                        "name": "t3_p_old",
                        "subreddit": "MutualfundsIndia",
                        "title": "Old SIP thread from 2023",
                        "selftext": "Paused my SIP back in 2023.",
                        "author": "old_user",
                        "created_utc": OLD_TS,
                        "permalink": "/r/MutualfundsIndia/comments/p_old/old/",
                        "score": 5,
                        "num_comments": 4,
                    },
                },
            ],
            "after": None,
        }
    }

    thread_json = [
        {"data": {"children": [search_json["data"]["children"][0]]}},
        {
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c_in",
                            "name": "t1_c_in",
                            "subreddit": "MutualfundsIndia",
                            "parent_id": "t3_p_in",
                            "body": "I also cancelled my mutual fund SIP when bank mandate failed.",
                            "author": "reply_user",
                            "created_utc": IN_WIN_TS + 3600,
                            "permalink": "/r/MutualfundsIndia/comments/p_in/stopped_my_sip/c_in/",
                            "score": 8,
                            "replies": "",
                        },
                    },
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c_old",
                            "name": "t1_c_old",
                            "subreddit": "MutualfundsIndia",
                            "parent_id": "t3_p_in",
                            "body": "Old SIP comment outside 24m window.",
                            "author": "old_reply",
                            "created_utc": OLD_TS,
                            "score": 1,
                            "replies": "",
                        },
                    },
                ]
            }
        },
    ]

    def fake_get(url: str, timeout: float = 20):
        if "search.json" in url:
            return _FakeResp(200, search_json)
        if "comments/p_in/.json" in url:
            return _FakeResp(200, thread_json)
        raise AssertionError(f"Unexpected URL (old thread should not be fetched): {url}")

    collector = RedditCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        sleep=lambda _: None,
        http_get=fake_get,
    )
    stats = collector.run()

    assert stats["posts_matched"] == 1
    assert stats["comments_matched"] == 1
    assert stats["thread_coverage"]["threads_fetched"] == 1
    assert stats["thread_coverage"]["total_comments_returned"] == 2
    assert stats["thread_coverage"]["total_num_comments_reported"] == 2

    rows = {
        r.id: r
        for r in (
            RawPost.model_validate(json.loads(l))
            for l in (settings.paths.resolve("raw_dir") / "reddit.jsonl").read_text(encoding="utf-8").splitlines()
        )
    }
    assert set(rows) == {"reddit:t3_p_in", "reddit:t1_c_in"}

    post = rows["reddit:t3_p_in"]
    assert post.raw_meta["record_type"] == "post"
    assert post.raw_meta["parent_id"] is None
    assert post.raw_meta["title"] == "Stopped my SIP on Groww"
    assert post.text == "Stopped my SIP on Groww. My autopay mandate failed twice."

    comment = rows["reddit:t1_c_in"]
    assert comment.raw_meta["record_type"] == "comment"
    assert comment.raw_meta["post_id"] == "t3_p_in"
    assert comment.raw_meta["parent_id"] == "t3_p_in"
    assert comment.raw_meta["post_title"] == "Stopped my SIP on Groww"
    # Comment text is strictly the comment's own body (no post title prepended)
    assert comment.text == "I also cancelled my mutual fund SIP when bank mandate failed."
    assert verify_evidence_span("Stopped my SIP on Groww", comment.text) is False


# ----------------------------------------------------------------------------- 3. Deleted/removed & bot/mod filtering


def test_reddit_deleted_and_bot_filtering(settings):
    _configure_minimal_reddit(settings)

    # Post has [removed] body, but its title matches SIP -> post is dropped, yet its thread IS fetched!
    search_json = {
        "data": {
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "id": "p_rem",
                        "name": "t3_p_rem",
                        "subreddit": "MutualfundsIndia",
                        "title": "Why did you pause your Groww SIP?",
                        "selftext": "[removed]",
                        "author": "some_op",
                        "created_utc": IN_WIN_TS,
                        "permalink": "/r/MutualfundsIndia/comments/p_rem/why/",
                        "score": 10,
                        "num_comments": 4,
                    },
                }
            ],
            "after": None,
        }
    }

    thread_json = [
        {"data": {"children": [search_json["data"]["children"][0]]}},
        {
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c_bot1",
                            "name": "t1_c_bot1",
                            "body": "Please read the SIP wiki before posting.",
                            "author": "AutoModerator",
                            "created_utc": IN_WIN_TS,
                            "score": 1,
                        },
                    },
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c_bot2",
                            "name": "t1_c_bot2",
                            "body": "SIP reminder! I am a bot, and this action was performed automatically.",
                            "author": "custom_helper",
                            "created_utc": IN_WIN_TS,
                            "score": 1,
                        },
                    },
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c_del_body",
                            "name": "t1_c_del_body",
                            "body": "[deleted]",
                            "author": "normal_user",
                            "created_utc": IN_WIN_TS,
                            "score": 3,
                        },
                    },
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c_del_author",
                            "name": "t1_c_del_author",
                            "body": "I paused my SIP in month 4 when my rent went up.",
                            "author": "[deleted]",
                            "created_utc": IN_WIN_TS,
                            "score": 9,
                        },
                    },
                ]
            }
        },
    ]

    def fake_get(url: str, timeout: float = 20):
        if "search.json" in url:
            return _FakeResp(200, search_json)
        if "comments/p_rem/.json" in url:
            return _FakeResp(200, thread_json)
        raise AssertionError(url)

    collector = RedditCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        sleep=lambda _: None,
        http_get=fake_get,
    )
    stats = collector.run()

    assert stats["posts_matched"] == 0
    assert stats["comments_matched"] == 1
    assert stats["dropped"]["deleted_or_removed"] >= 2
    assert stats["dropped"]["bot_or_mod"] == 2

    lines = (settings.paths.resolve("raw_dir") / "reddit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    kept = RawPost.model_validate(json.loads(lines[0]))
    assert kept.id == "reddit:t1_c_del_author"
    assert kept.text == "I paused my SIP in month 4 when my rent went up."


# ----------------------------------------------------------------------------- 4. Multi-query/sort thread deduplication


def test_reddit_thread_deduplication_across_queries(settings):
    _configure_minimal_reddit(settings)
    settings.sources["reddit"]["queries"] = ["SIP", "Groww SIP"]
    settings.sources["reddit"]["sorts"] = [{"sort": "new", "t": "all"}, {"sort": "top", "t": "year"}]

    same_post = {
        "kind": "t3",
        "data": {
            "id": "dup_t3",
            "name": "t3_dup_t3",
            "subreddit": "MutualfundsIndia",
            "title": "Groww SIP autopay issue",
            "selftext": "My SIP instalment was skipped.",
            "author": "u1",
            "created_utc": IN_WIN_TS,
            "permalink": "/r/MutualfundsIndia/comments/dup_t3/groww/",
            "score": 10,
            "num_comments": 1,
        },
    }
    thread_fetches = {"count": 0}

    def fake_get(url: str, timeout: float = 20):
        if "search.json" in url:
            return _FakeResp(200, {"data": {"children": [same_post], "after": None}})
        if "comments/dup_t3/.json" in url:
            thread_fetches["count"] += 1
            return _FakeResp(200, [{"data": {"children": [same_post]}}, {"data": {"children": []}}])
        raise AssertionError(url)

    collector = RedditCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        sleep=lambda _: None,
        http_get=fake_get,
    )
    stats = collector.run()

    # 2 queries x 2 sorts = 4 search calls, all returning t3_dup_t3, but thread is fetched only ONCE
    assert thread_fetches["count"] == 1
    assert stats["posts_matched"] == 1
    assert stats["total_in_file"] == 1


# ----------------------------------------------------------------------------- 5. 429 cooldown & idempotent resume


def test_reddit_429_cooldown_and_idempotent_resume(settings):
    _configure_minimal_reddit(settings)
    sleeps: List[float] = []

    post_a = {
        "kind": "t3",
        "data": {
            "id": "ta",
            "name": "t3_ta",
            "subreddit": "MutualfundsIndia",
            "title": "First SIP on Groww",
            "selftext": "Started my first SIP of 2000.",
            "author": "ua",
            "created_utc": IN_WIN_TS,
            "permalink": "/r/MutualfundsIndia/comments/ta/a/",
            "score": 4,
            "num_comments": 1,
        },
    }
    post_b = {
        "kind": "t3",
        "data": {
            "id": "tb",
            "name": "t3_tb",
            "subreddit": "MutualfundsIndia",
            "title": "Paused SIP on Groww",
            "selftext": "Paused SIP last month.",
            "author": "ub",
            "created_utc": IN_WIN_TS,
            "permalink": "/r/MutualfundsIndia/comments/tb/b/",
            "score": 6,
            "num_comments": 1,
        },
    }

    # Run 1: search gets a 429 with Retry-After=12 on first try, succeeds on retry;
    # thread 'ta' succeeds, thread 'tb' crashes with network drop -> checkpoint saved with 'tb' still pending
    state = {"search_tries": 0}

    def run1_get(url: str, timeout: float = 20):
        if "search.json" in url:
            state["search_tries"] += 1
            if state["search_tries"] == 1:
                return _FakeResp(429, "Too Many Requests", headers={"Retry-After": "12"})
            return _FakeResp(200, {"data": {"children": [post_a, post_b], "after": None}})
        if "comments/ta/.json" in url:
            return _FakeResp(
                200,
                [
                    {"data": {"children": [post_a]}},
                    {
                        "data": {
                            "children": [
                                {
                                    "kind": "t1",
                                    "data": {
                                        "id": "ca",
                                        "name": "t1_ca",
                                        "body": "My SIP is still going after 1 year SIP!",
                                        "author": "ca_u",
                                        "created_utc": IN_WIN_TS,
                                        "score": 5,
                                    },
                                }
                            ]
                        }
                    },
                ],
            )
        if "comments/tb/.json" in url:
            raise RuntimeError("simulated connection drop on tb")
        raise AssertionError(url)

    c1 = RedditCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        sleep=lambda s: sleeps.append(s),
        http_get=run1_get,
    )
    s1 = c1.run()
    assert 60.0 in sleeps
    assert all(s >= 60.0 for s in sleeps)
    assert s1["status"] == "failed"
    assert s1["threads_done"] == 1
    assert s1["threads_pending"] == 1

    # Run 2: resumes directly from pending_threads ('tb') without re-running completed search or 'ta'
    run2_urls: List[str] = []

    def run2_get(url: str, timeout: float = 20):
        run2_urls.append(url)
        if "comments/tb/.json" in url:
            return _FakeResp(
                200,
                [
                    {"data": {"children": [post_b]}},
                    {
                        "data": {
                            "children": [
                                {
                                    "kind": "t1",
                                    "data": {
                                        "id": "cb",
                                        "name": "t1_cb",
                                        "body": "I also paused SIP due to medical expenses.",
                                        "author": "cb_u",
                                        "created_utc": IN_WIN_TS,
                                        "score": 7,
                                    },
                                }
                            ]
                        }
                    },
                ],
            )
        raise AssertionError(f"Should only fetch remaining pending thread tb, got: {url}")

    c2 = RedditCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        sleep=lambda _: None,
        http_get=run2_get,
    )
    s2 = c2.run()
    assert s2["status"] == "ok"
    assert len(run2_urls) == 1 and "comments/tb/.json" in run2_urls[0]
    assert s2["threads_pending"] == 0
    assert s2["threads_done"] == 2

    lines = (settings.paths.resolve("raw_dir") / "reddit.jsonl").read_text(encoding="utf-8").splitlines()
    ids = [json.loads(l)["id"] for l in lines]
    assert len(ids) == len(set(ids)) == 4
    assert set(ids) == {"reddit:t3_ta", "reddit:t3_tb", "reddit:t1_ca", "reddit:t1_cb"}


# ----------------------------------------------------------------------------- 6. matched_via & 30-comment per-thread cap


def test_reddit_comment_matched_via_and_per_thread_cap(settings):
    _configure_minimal_reddit(settings)
    settings.sources["reddit"]["max_comments_per_thread"] = 30

    # Thread has a SIP anchor in the title ("Groww SIP")
    parent_post = {
        "kind": "t3",
        "data": {
            "id": "tcap",
            "name": "t3_tcap",
            "subreddit": "MutualfundsIndia",
            "title": "Anyone still continuing their Groww SIP in this market?",
            "selftext": "Curious how beginners are handling their monthly SIP.",
            "author": "op_user",
            "created_utc": IN_WIN_TS,
            "permalink": "/r/MutualfundsIndia/comments/tcap/thread/",
            "score": 25,
            "num_comments": 40,
        },
    }

    comments_children = [
        # 1. Comment that matches via thread_context (generic Hinglish/continuer phrase "band nahi kiya", no "SIP" word)
        {
            "kind": "t1",
            "data": {
                "id": "c_ctx_1",
                "name": "t1_c_ctx_1",
                "body": "Market gira phir bhi maine abhi tak band nahi kiya, holding steady.",
                "author": "u_ctx1",
                "created_utc": IN_WIN_TS + 10,
                "score": 100,
            },
        },
        # 2. Comment that matches via thread_context ("still going", no "SIP" word)
        {
            "kind": "t1",
            "data": {
                "id": "c_ctx_2",
                "name": "t1_c_ctx_2",
                "body": "Started 8 months ago with 3k a month and still going without checking daily.",
                "author": "u_ctx2",
                "created_utc": IN_WIN_TS + 20,
                "score": 95,
            },
        },
        # 3. Long generic comment (>20 words) WITHOUT any core/continuer/hinglish phrase or anchor -> MUST BE REJECTED
        {
            "kind": "t1",
            "data": {
                "id": "c_long_no_phrase",
                "name": "t1_c_long_no_phrase",
                "body": (
                    "Honestly I just deleted the app from my phone after the budget announcement because "
                    "checking the red numbers every morning was giving me way too much anxiety at work."
                ),
                "author": "u_long",
                "created_utc": IN_WIN_TS + 30,
                "score": 200,
            },
        },
    ]

    # Add 33 direct "self" matching comments with scores 1..33 so total matched = 2 (ctx) + 33 (self) = 35 -> 5 capped!
    for i in range(1, 34):
        comments_children.append({
            "kind": "t1",
            "data": {
                "id": f"c_self_{i}",
                "name": f"t1_c_self_{i}",
                "body": f"My SIP installment #{i} went through fine on Groww.",
                "author": f"u_s{i}",
                "created_utc": IN_WIN_TS + 100 + i,
                "score": i,
            },
        })

    def fake_get(url: str, timeout: float = 20):
        if "search.json" in url:
            return _FakeResp(200, {"data": {"children": [parent_post], "after": None}})
        if "comments/tcap/.json" in url:
            return _FakeResp(
                200,
                [
                    {"data": {"children": [parent_post]}},
                    {"data": {"children": comments_children}},
                ],
            )
        raise AssertionError(url)

    collector = RedditCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        sleep=lambda _: None,
        http_get=fake_get,
    )
    stats = collector.run()

    assert stats["comments_matched"] == 30
    assert stats["comments_capped"] == 5
    assert stats["dropped"]["thread_comment_cap"] == 5
    assert stats["matched_via"] == {"self": 28, "thread_context": 2}

    rows = {
        r.id: r
        for r in (
            RawPost.model_validate(json.loads(l))
            for l in (settings.paths.resolve("raw_dir") / "reddit.jsonl").read_text(encoding="utf-8").splitlines()
        )
    }
    # Long comment without any phrase match was rejected despite being >20 words and score=200
    assert "reddit:t1_c_long_no_phrase" not in rows

    # Both thread_context comments (scores 100 and 95) were kept and tagged matched_via="thread_context"
    assert rows["reddit:t1_c_ctx_1"].raw_meta["matched_via"] == "thread_context"
    assert rows["reddit:t1_c_ctx_1"].keyword_group == "continuer"
    assert rows["reddit:t1_c_ctx_2"].raw_meta["matched_via"] == "thread_context"

    # Direct comment has matched_via="self"
    assert rows["reddit:t1_c_self_33"].raw_meta["matched_via"] == "self"

    # Lowest-scored 5 comments (scores 1..5) were dropped by the 30-comment cap
    for low_i in range(1, 6):
        assert f"reddit:t1_c_self_{low_i}" not in rows
    for kept_i in range(6, 34):
        assert f"reddit:t1_c_self_{kept_i}" in rows


# ----------------------------------------------------------------------------- 7. Minimum 60s 429 cooldown & >80% out-of-window slice stop


def test_reddit_429_minimum_cooldown_and_out_of_window_page_stop(settings):
    _configure_minimal_reddit(settings)
    settings.sources["reddit"]["sorts"] = [{"sort": "top", "t": "year"}]
    settings.sources["reddit"]["max_pages_per_stream"] = 3
    settings.sources["reddit"]["request_delay_sec"] = 10.0
    settings.sources["reddit"]["rate_limit_cooldown_sec"] = 60.0

    sleeps: List[float] = []
    search_calls: List[str] = []

    # Page 1 has 6 posts: 1 in-window, 5 older than 24m (5/6 = 83.3% > 80%) and a non-null `after` token.
    # Slice must stop after page 1 and NEVER fetch page 2.
    children = [
        {
            "kind": "t3",
            "data": {
                "id": "p_in_1",
                "name": "t3_p_in_1",
                "subreddit": "MutualfundsIndia",
                "title": "Paused my Groww SIP this month",
                "selftext": "Stopped SIP temporarily.",
                "author": "u_in",
                "created_utc": IN_WIN_TS,
                "permalink": "/r/MutualfundsIndia/comments/p_in_1/in/",
                "score": 12,
                "num_comments": 0,
            },
        }
    ]
    for idx in range(1, 6):
        children.append({
            "kind": "t3",
            "data": {
                "id": f"p_old_{idx}",
                "name": f"t3_p_old_{idx}",
                "subreddit": "MutualfundsIndia",
                "title": f"Old SIP post {idx}",
                "selftext": "Paused my SIP years ago.",
                "author": f"u_old_{idx}",
                "created_utc": OLD_TS - idx * 86400,
                "permalink": f"/r/MutualfundsIndia/comments/p_old_{idx}/old/",
                "score": 50 - idx,
                "num_comments": 0,
            },
        })

    state = {"search_tries": 0}

    def fake_get(url: str, timeout: float = 20):
        if "search.json" in url:
            if "after=" in url:
                raise AssertionError(f"Should not paginate after >80% out-of-window page: {url}")
            search_calls.append(url)
            state["search_tries"] += 1
            # Retry 1: x-ratelimit-reset is "0" -> must still sleep 60s (never 0s!)
            if state["search_tries"] == 1:
                return _FakeResp(429, "Too Many Requests", headers={"x-ratelimit-reset": "0"})
            # Retry 2: Retry-After is "5" -> must sleep 120s (60 * 2^1, never 5s or 0s!)
            if state["search_tries"] == 2:
                return _FakeResp(429, "Too Many Requests", headers={"Retry-After": "5", "x-ratelimit-reset": "0"})
            # Retry 3: x-ratelimit-reset is "300" -> larger than 60 * 2^2 (240s), so sleeps 300s
            if state["search_tries"] == 3:
                return _FakeResp(429, "Too Many Requests", headers={"x-ratelimit-reset": "300"})
            # Try 4 succeeds, with x-ratelimit-reset: "75"
            return _FakeResp(
                200,
                {"data": {"children": children, "after": "t3_p_old_5"}},
                headers={"x-ratelimit-reset": "75", "x-ratelimit-remaining": "8"},
            )
        if "comments/p_in_1/.json" in url:
            return _FakeResp(200, [{"data": {"children": [children[0]]}}, {"data": {"children": []}}])
        raise AssertionError(f"Unexpected URL: {url}")

    collector = RedditCollector(
        settings,
        raw_dir=settings.paths.resolve("raw_dir"),
        checkpoint_dir=settings.paths.resolve("checkpoints"),
        now=FIXED_NOW,
        sleep=lambda s: sleeps.append(s),
        http_get=fake_get,
    )
    stats = collector.run()

    assert stats["status"] == "ok"
    # Verify 429 retry sleeps: 60s (retry 1), 120s (retry 2), 300s (retry 3),
    # then 300s post-429 reset window wait before the next request, then 10s normal delay
    assert 0.0 not in sleeps
    assert sleeps[:4] == [60.0, 120.0, 300.0, 300.0]
    assert all(s >= 10.0 for s in sleeps)

    # Verify >80% out-of-window page stopped pagination on page 1
    assert stats["slices_stopped_out_of_window"] == 1
    assert collector.state["streams"]["MutualfundsIndia|SIP|top|year"]["pages"] == 1
    assert collector.state["streams"]["MutualfundsIndia|SIP|top|year"]["done"] is True
    assert stats["posts_matched"] == 1

