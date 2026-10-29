"""TradingQ&A (Discourse) collector (Stage 1, Priority 3 — PRD §9.1, Architecture §4.3).

Collects public forum topics and replies from `https://tradingqna.com` via Discourse's public JSON API:
  1. Search phase (`/search.json?q={query}&page={page}`):
     - Queries single-term/phrase SIP queries (Discourse treats `OR` as an AND literal when quoted, so
       queries are kept single-term/phrase).
     - Queues candidate topics (`id`, `slug`, `title`, `category_id`, `posts_count`).
  2. Topic harvest phase (`/t/{topic_id}.json`):
     - Fetches the topic's `post_stream.posts`.
     - Post #1 (`post_number == 1`) is evaluated as `kind = "topic"` with text `f"{topic_title}\\n\\n{body}"`.
     - Posts #2+ (`post_number > 1`, `post_type == 1`) are evaluated as `kind = "reply"` after stripping
       `<aside class="quote">...</aside>` blocks so quoted OP text never triggers false keyword matches.
     - Inside a SIP/MF-anchored topic (`KeywordMatcher.has_anchor` on title or OP), replies match via
       `match_anchored_reply` (standard `match_with_context` + the P4/P5 first-person + behaviour rule,
       tagged `matched_via = "thread_context"`).
     - Caps kept replies per topic at `max_replies_per_topic` (20, sorted by `like_count DESC, created_at ASC`)
       and stops the collector cleanly once `max_matched_total` (400) is reached.
     - Sets `raw_meta.platform_tag = "zerodha"` on every record per PRD §9.1 / §9.3 (competitor posts retained
       and tagged).
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote

import requests
from pydantic import BaseModel, ConfigDict, Field

from collectors.base import BaseCollector, TransientError, to_utc
from collectors.matcher import KeywordMatcher, MatchResult, match_anchored_reply
from core.config import Settings
from core.logger import get_logger
from core.schemas import RawPost

log = get_logger("collect.tradingqna")

DEFAULT_BASE_URL = "https://tradingqna.com"
DEFAULT_QUERIES: Tuple[str, ...] = (
    "SIP stopped",
    "pause SIP",
    "cancel SIP",
    "SIP mandate",
    "autopay SIP",
    "SIP installment failed",
    "first SIP",
    "Coin SIP",
    "step up SIP",
    "ELSS SIP",
    "XIRR mutual fund",
    "mutual fund SIP",
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MIN_RATE_LIMIT_COOLDOWN_SEC = 60.0

_QUOTE_ASIDE_RE = re.compile(r"<aside\b[^>]*class=[\"'][^\"']*quote[^\"']*[\"'][^>]*>.*?</aside>", re.IGNORECASE | re.DOTALL)
_BLOCK_TAG_RE = re.compile(r"</?(?:p|div|br|li|ul|ol|blockquote|h[1-6]|tr)[^>]*>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_SPACE_RE = re.compile(r"[ \t]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def clean_discourse_cooked(cooked_html: str) -> str:
    """Convert Discourse `cooked` HTML to plain text after stripping `<aside class="quote">...</aside>`."""
    if not cooked_html:
        return ""
    # Strip nested or sequential <aside class="quote"> blocks so quoted text doesn't trigger false matches
    s = cooked_html
    prev = None
    while prev != s:
        prev = s
        s = _QUOTE_ASIDE_RE.sub(" ", s)
    s = _BLOCK_TAG_RE.sub("\n", s)
    s = _ANY_TAG_RE.sub(" ", s)
    s = html.unescape(s)
    lines = [_MULTI_SPACE_RE.sub(" ", ln).strip() for ln in s.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return _MULTI_NL_RE.sub("\n\n", text).strip()


def extract_discourse_like_count(post: Dict[str, Any]) -> int:
    """Extract like count from Discourse post JSON (`like_count` or `actions_summary` id==2)."""
    direct = post.get("like_count")
    if isinstance(direct, int):
        return direct
    for act in post.get("actions_summary") or []:
        if isinstance(act, dict) and act.get("id") == 2 and isinstance(act.get("count"), int):
            return act["count"]
    return 0


class TradingQnACfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    priority: int = 3
    base_url: str = DEFAULT_BASE_URL
    queries: List[str] = Field(default_factory=lambda: list(DEFAULT_QUERIES))
    max_search_pages_per_query: int = Field(2, ge=1, le=10)
    max_replies_per_topic: int = Field(20, ge=1)
    max_matched_total: int = Field(400, ge=1)
    request_delay_sec: float = Field(6.0, ge=0.0)
    rate_limit_cooldown_sec: float = Field(60.0, ge=0.0)
    platform_tag: str = "zerodha"
    user_agent: str = DEFAULT_USER_AGENT


class TradingQnACollector(BaseCollector):
    source = "tradingqna"

    def __init__(
        self,
        settings: Settings,
        *,
        raw_dir: Path,
        checkpoint_dir: Path,
        now: Optional[datetime] = None,
        sleep: Callable[[float], None] = time.sleep,
        scan_limit: Optional[int] = None,
        matcher: Optional[KeywordMatcher] = None,
        session: Optional[requests.Session] = None,
        queries_override: Optional[List[str]] = None,
    ):
        super().__init__(
            settings,
            raw_dir=raw_dir,
            checkpoint_dir=checkpoint_dir,
            now=now,
            sleep=sleep,
            scan_limit=scan_limit,
            matcher=matcher,
        )
        raw_cfg = dict(settings.sources.get(self.source, {}))
        self.cfg = TradingQnACfg.model_validate(raw_cfg)
        if queries_override:
            self.cfg.queries = list(queries_override)

        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.cfg.user_agent,
                "Accept": "application/json",
            }
        )
        self._has_requested = False

        # Initialize checkpoint structures
        self.state.setdefault("search_done_queries", [])
        self.state.setdefault("topic_queue", {})
        self.state.setdefault("harvested_topics", [])
        self.state.setdefault("matched_via", {"self": 0, "thread_context": 0})
        self.state.setdefault("per_query", {})

    # -- HTTP + rate limit handling -----------------------------------------------------------

    def _cooldown_seconds(self, resp: Optional[requests.Response] = None) -> float:
        floor = max(float(self.cfg.rate_limit_cooldown_sec), MIN_RATE_LIMIT_COOLDOWN_SEC)
        if resp is None or not getattr(resp, "headers", None):
            return floor
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                return max(float(ra), floor)
            except ValueError:
                try:
                    dt = parsedate_to_datetime(ra)
                    return max((dt - self.now).total_seconds(), floor)
                except Exception:
                    pass
        return floor

    def _get_json(self, path_or_url: str, what: str) -> Dict[str, Any]:
        url = path_or_url if path_or_url.startswith("http") else f"{self.cfg.base_url.rstrip('/')}/{path_or_url.lstrip('/')}"
        if self._has_requested and self.cfg.request_delay_sec > 0:
            self.sleep(self.cfg.request_delay_sec)

        def _do() -> Dict[str, Any]:
            self._has_requested = True
            self.count("requests_made")
            try:
                resp = self.session.get(url, timeout=self.collect_cfg.request_timeout_sec)
            except requests.RequestException as e:
                raise TransientError(f"Network error fetching {url}: {e}") from e

            if resp.status_code == 429:
                wait_s = self._cooldown_seconds(resp)
                log.warning("%s: HTTP 429 rate limit; sleeping %.0fs cooldown", what, wait_s)
                self.sleep(wait_s)
                raise TransientError(f"HTTP 429 on {url} (slept {wait_s:.0f}s)")
            if resp.status_code >= 500:
                raise TransientError(f"HTTP {resp.status_code} on {url}")
            if resp.status_code == 404:
                return {"_not_found": True}
            if resp.status_code != 200:
                raise TransientError(f"HTTP {resp.status_code} on {url}")
            try:
                return resp.json()
            except ValueError as e:
                raise TransientError(f"Invalid JSON from {url}: {e}") from e

        return self.call_with_retries(_do, what)

    # -- Phase 1: Search discovery ------------------------------------------------------------

    def _discover_topics_for_query(self, query: str) -> int:
        """Run `/search.json` for `query` (up to `max_search_pages_per_query` pages). Returns topics queued."""
        q_stats = self.state["per_query"].setdefault(query, {"hits": 0, "topics_queued": 0})
        queued_now = 0
        for page in range(1, self.cfg.max_search_pages_per_query + 1):
            path = f"/search.json?q={quote(query)}&page={page}"
            data = self._get_json(path, f"search({query!r}, page={page})")
            if data.get("_not_found"):
                break
            posts = data.get("posts") or []
            topics = data.get("topics") or []
            q_stats["hits"] += len(posts)
            topic_by_id: Dict[int, Dict[str, Any]] = {
                int(t["id"]): t for t in topics if isinstance(t, dict) and t.get("id") is not None
            }

            # Check dates on search post snippets so we don't paginate deeply into ancient threads
            in_win_posts = 0
            for p in posts:
                dt = to_utc(p.get("created_at"))
                if self.in_window(dt):
                    in_win_posts += 1
                tid = p.get("topic_id")
                if tid is None:
                    continue
                tid_str = str(tid)
                t_obj = topic_by_id.get(int(tid), {})
                # Also check if topic's last_posted_at or created_at is in window, or post itself is in window
                t_last = to_utc(t_obj.get("last_posted_at") or t_obj.get("bumped_at") or t_obj.get("created_at"))
                if not (self.in_window(dt) or self.in_window(t_last)):
                    continue
                if tid_str not in self.state["topic_queue"] and tid_str not in self.state["harvested_topics"]:
                    self.state["topic_queue"][tid_str] = {
                        "topic_id": int(tid),
                        "slug": t_obj.get("slug") or f"topic-{tid}",
                        "title": t_obj.get("title") or p.get("topic_title_headline") or "",
                        "category_id": t_obj.get("category_id"),
                        "posts_count": t_obj.get("posts_count", 0),
                        "discovered_via_query": query,
                    }
                    queued_now += 1
                    q_stats["topics_queued"] += 1

            more = (data.get("grouped_search_result") or {}).get("more_full_page_results")
            if not posts or not more:
                break
            # Stop paginating a query if >80% of search hits on this page are older than the 24m window
            if len(posts) >= 5 and (len(posts) - in_win_posts) / len(posts) > 0.80:
                break

        return queued_now

    # -- Phase 2: Topic harvesting ------------------------------------------------------------

    def _harvest_topic(self, topic_meta: Dict[str, Any]) -> List[RawPost]:
        tid = int(topic_meta["topic_id"])
        data = self._get_json(f"/t/{tid}.json", f"topic({tid})")
        if data.get("_not_found"):
            self.drop("topic_not_found")
            return []

        title = (data.get("title") or topic_meta.get("title") or "").strip()
        slug = (data.get("slug") or topic_meta.get("slug") or f"topic-{tid}").strip()
        category_id = data.get("category_id", topic_meta.get("category_id"))
        posts_stream = (data.get("post_stream") or {}).get("posts") or []
        if not posts_stream:
            return []

        self.count("topics_harvested")
        out: List[RawPost] = []
        topic_has_anchor = self.matcher.has_anchor(title)

        # Evaluate Post #1 (OP)
        op_posts = [p for p in posts_stream if p.get("post_number") == 1]
        replies_raw = [p for p in posts_stream if (p.get("post_number") or 0) > 1]

        for op in op_posts:
            self.count("scanned")
            self.count("topics_scanned")
            body = clean_discourse_cooked(op.get("cooked") or "")
            combined_text = f"{title}\n\n{body}".strip() if title else body
            if self.matcher.has_anchor(combined_text):
                topic_has_anchor = True

            dt = to_utc(op.get("created_at"))
            if not self.in_window(dt):
                self.drop("before_window")
                continue
            if len(_WORD_RE.findall(body)) < 3:
                self.drop("empty_or_short")
                continue

            native_id = f"t_{tid}"
            full_id = f"{self.source}:{native_id}"
            if full_id in self.existing_ids:
                self.drop("duplicate_id")
                continue

            m = self.matcher.match(combined_text)
            if not m.matched:
                self.drop("no_keyword")
                continue

            assert dt is not None
            post_obj = self._build_raw_post(
                native_id=native_id,
                url=f"{self.cfg.base_url.rstrip('/')}/t/{slug}/{tid}",
                dt=dt,
                username=str(op.get("username") or op.get("user_id") or ""),
                text=combined_text,
                match=m,
                kind="topic",
                topic_id=tid,
                topic_title=title,
                topic_slug=slug,
                category_id=category_id,
                post_id=int(op.get("id") or tid),
                post_number=1,
                reply_to_post_number=None,
                like_count=extract_discourse_like_count(op),
                reply_count=int(op.get("reply_count") or 0),
                reads=int(op.get("reads") or 0),
                discovered_via_query=topic_meta.get("discovered_via_query"),
            )
            out.append(post_obj)

        # Evaluate Replies (post_number > 1)
        candidate_replies: List[Tuple[int, str, RawPost, MatchResult]] = []
        for rep in replies_raw:
            # Skip non-regular posts (e.g., moderator small_action / whisper post_type != 1)
            if rep.get("post_type", 1) != 1:
                continue
            self.count("scanned")
            self.count("replies_scanned")

            dt = to_utc(rep.get("created_at"))
            if not self.in_window(dt):
                self.drop("before_window")
                continue

            body = clean_discourse_cooked(rep.get("cooked") or "")
            if len(_WORD_RE.findall(body)) < 3:
                self.drop("empty_or_short")
                continue

            pid = rep.get("id")
            post_num = int(rep.get("post_number") or 2)
            native_id = f"p_{pid}" if pid is not None else f"t_{tid}_p_{post_num}"
            full_id = f"{self.source}:{native_id}"
            if full_id in self.existing_ids:
                self.drop("duplicate_id")
                continue

            if topic_has_anchor:
                m = match_anchored_reply(body, self.matcher)
            else:
                m = self.matcher.match(body)

            if not m.matched:
                self.drop("no_keyword")
                continue

            assert dt is not None
            like_cnt = extract_discourse_like_count(rep)
            # Do not mutate existing_ids / counters until after per-topic top-K selection
            raw_post = RawPost(
                id=full_id,
                source=self.source,
                url=f"{self.cfg.base_url.rstrip('/')}/t/{slug}/{tid}/{post_num}",
                date=dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                author_hash="",  # populated on selection via make_post
                text=body,
                collection_method="scraped",
                keyword_group="continuer" if m.keyword_group == "continuer" else "core",
                raw_meta={
                    "kind": "reply",
                    "topic_id": tid,
                    "topic_title": title,
                    "topic_slug": slug,
                    "category_id": category_id,
                    "post_id": int(pid) if pid is not None else post_num,
                    "post_number": post_num,
                    "reply_to_post_number": rep.get("reply_to_post_number"),
                    "like_count": like_cnt,
                    "reply_count": int(rep.get("reply_count") or 0),
                    "reads": int(rep.get("reads") or 0),
                    "platform_tag": self.cfg.platform_tag,
                    "matched_via": m.matched_via,
                    "discovered_via_query": topic_meta.get("discovered_via_query"),
                    "_username": str(rep.get("username") or rep.get("user_id") or ""),
                    "_dt": dt,
                    "_native_id": native_id,
                },
            )
            candidate_replies.append((like_cnt, raw_post.date, raw_post, m))

        # Sort candidate replies by (-like_count, created_at ASC) and cap at max_replies_per_topic
        candidate_replies.sort(key=lambda item: (-item[0], item[1]))
        kept_replies = candidate_replies[: self.cfg.max_replies_per_topic]
        dropped_overflow = len(candidate_replies) - len(kept_replies)
        for _ in range(dropped_overflow):
            self.drop("per_topic_cap")

        for _, _, rp, m in kept_replies:
            if self.state["counters"].get("matched", 0) + len(out) >= self.cfg.max_matched_total:
                self.drop("max_matched_cap")
                continue
            rm = dict(rp.raw_meta)
            uname = rm.pop("_username")
            dt_val = rm.pop("_dt")
            nat_id = rm.pop("_native_id")
            post_obj = self._build_raw_post(
                native_id=nat_id,
                url=rp.url,
                dt=dt_val,
                username=uname,
                text=rp.text,
                match=m,
                kind="reply",
                topic_id=rm["topic_id"],
                topic_title=rm["topic_title"],
                topic_slug=rm["topic_slug"],
                category_id=rm["category_id"],
                post_id=rm["post_id"],
                post_number=rm["post_number"],
                reply_to_post_number=rm["reply_to_post_number"],
                like_count=rm["like_count"],
                reply_count=rm["reply_count"],
                reads=rm["reads"],
                discovered_via_query=rm.get("discovered_via_query"),
            )
            out.append(post_obj)

        return out

    def _build_raw_post(
        self,
        *,
        native_id: str,
        url: str,
        dt: datetime,
        username: str,
        text: str,
        match: MatchResult,
        kind: str,
        topic_id: int,
        topic_title: str,
        topic_slug: str,
        category_id: Any,
        post_id: int,
        post_number: int,
        reply_to_post_number: Any,
        like_count: int,
        reply_count: int,
        reads: int,
        discovered_via_query: Optional[str],
    ) -> RawPost:
        raw_meta: Dict[str, Any] = {
            "kind": kind,
            "topic_id": topic_id,
            "topic_title": topic_title,
            "topic_slug": topic_slug,
            "category_id": category_id,
            "post_id": post_id,
            "post_number": post_number,
            "reply_to_post_number": reply_to_post_number,
            "like_count": like_count,
            "reply_count": reply_count,
            "reads": reads,
            "platform_tag": self.cfg.platform_tag,
            "matched_via": match.matched_via,
        }
        if discovered_via_query:
            raw_meta["discovered_via_query"] = discovered_via_query

        post = self.make_post(
            native_id=native_id,
            url=url,
            dt=dt,
            username=username,
            text=text,
            match=match,
            raw_meta=raw_meta,
        )
        self.count("matched")
        if kind == "topic":
            self.count("topics_matched")
        else:
            self.count("replies_matched")
        mv = self.state.setdefault("matched_via", {"self": 0, "thread_context": 0})
        mv[match.matched_via] = mv.get(match.matched_via, 0) + 1
        return post

    # -- Main generator -----------------------------------------------------------------------

    def iter_pages(self) -> Iterator[List[RawPost]]:
        # 1. Run search queries not yet completed
        done_queries = set(self.state.get("search_done_queries") or [])
        for q in self.cfg.queries:
            if q in done_queries:
                continue
            # If scan_limit is set (capped test run) and we already have enough queued topics, stop searching early
            if self.scan_limit and len(self.state["topic_queue"]) >= self.scan_limit:
                break
            self._discover_topics_for_query(q)
            self.state["search_done_queries"].append(q)
            done_queries.add(q)
            self.save_state()

        # 2. Harvest queued topics
        harvested_set = set(self.state.get("harvested_topics") or [])
        topics_processed_this_run = 0
        for tid_str, topic_meta in list(self.state["topic_queue"].items()):
            if tid_str in harvested_set:
                self.state["topic_queue"].pop(tid_str, None)
                continue
            if self.state["counters"].get("matched", 0) >= self.cfg.max_matched_total:
                log.info("Reached max_matched_total (%d); stopping TradingQ&A collection", self.cfg.max_matched_total)
                break
            if self.scan_limit and topics_processed_this_run >= self.scan_limit:
                log.info("Reached capped test topic limit (%d); stopping TradingQ&A collection", self.scan_limit)
                break

            posts = self._harvest_topic(topic_meta)
            topics_processed_this_run += 1
            self.state["harvested_topics"].append(tid_str)
            harvested_set.add(tid_str)
            self.state["topic_queue"].pop(tid_str, None)
            yield posts

    def extra_stats(self) -> Dict[str, Any]:
        c = self.state["counters"]
        return {
            "requests_made": c.get("requests_made", 0),
            "topics_queued_remaining": len(self.state.get("topic_queue", {})),
            "topics_harvested": c.get("topics_harvested", 0),
            "topics_scanned": c.get("topics_scanned", 0),
            "topics_matched": c.get("topics_matched", 0),
            "replies_scanned": c.get("replies_scanned", 0),
            "replies_matched": c.get("replies_matched", 0),
            "matched_via": dict(self.state.get("matched_via", {})),
            "per_query": dict(self.state.get("per_query", {})),
            "platform_tag": self.cfg.platform_tag,
        }
