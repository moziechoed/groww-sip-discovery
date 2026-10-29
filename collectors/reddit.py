"""Reddit collector (P2): public no-login .json and .rss (Atom XML) endpoints.

Key design (locked 25 Oct):
- No Reddit API key or login required. Tries .json first; if blocked by Reddit's WAF (HTTP 403),
  automatically switches to .rss (Atom XML) for the rest of the run.
- Two-phase resumable state in data/checkpoints/collect_reddit.json:
    1. Search discovery across (subreddit, query, sort, t) streams -> writes matched posts (reddit:t3_*)
       and queues in-window threads into pending_threads (deduped across streams).
    2. Thread comment harvesting (/r/{sub}/comments/{id}/.{json|rss}?limit=500) -> writes up to
       max_comments_per_thread (30) matched comments per thread (reddit:t1_*).
- Comment matching:
    - Directly matches KeywordMatcher -> raw_meta.matched_via = "self"
    - Inside a SIP-anchored thread and matches any core/continuer/hinglish phrase ->
      raw_meta.matched_via = "thread_context"
    - RawPost.text for a comment is strictly the comment's own text.
- Deleted/removed bodies and moderator/bot comments are dropped; [deleted] authors with intact text
  are kept; removed posts whose title matched in-window still have their comments harvested.
"""
from __future__ import annotations

import html
import random
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional, Tuple
from urllib.parse import quote_plus

import requests
from pydantic import BaseModel, ConfigDict, Field

from collectors.app_store import compose_text
from collectors.base import BaseCollector, CollectorError, TransientError, iso_z, to_utc
from core.logger import get_logger
from core.schemas import RawPost

log = get_logger("collect.reddit")

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
SEARCH_URL = "https://www.reddit.com/r/{sub}/search.{ext}?q={q}&restrict_sr=on&sort={sort}&t={t}&limit=100{after}"
THREAD_URL = "https://www.reddit.com/r/{sub}/comments/{short_id}/.{ext}?limit=500"

_REMOVED_MARKERS = frozenset({
    "[deleted]",
    "[removed]",
    "[ removed by reddit ]",
    "[deleted by user]",
    "[removed by moderator]",
})
_BOT_AUTHORS = frozenset({
    "automoderator",
    "remindmebot",
    "wikitextbot",
    "botdefense",
    "sneakpeekbot",
    "fatfirebot",
    "totesmessenger",
    "haikubot",
    "metric_units",
    "akespear",
})
_BOT_PHRASES = (
    "i am a bot, and this action was performed automatically",
    "this action was performed automatically. please contact the moderators",
)

_MD_DIV_RE = re.compile(r'<div\s+class=["\']md["\']\s*>(.*?)</div>\s*(?:<!--\s*SC_ON\s*-->|$)', re.DOTALL | re.IGNORECASE)
_SUBMITTED_FOOTER_RE = re.compile(r"submitted\s+by\s+/u/\S+.*$", re.DOTALL | re.IGNORECASE)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK_TAG_RE = re.compile(r"</?(?:p|br|div|li|ul|ol|blockquote|tr|h[1-6])\b[^>]*>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


MIN_RATE_LIMIT_COOLDOWN_SEC = 60.0


class SortSlice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sort: str = "new"
    t: str = "all"


class RedditCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    priority: int = 1
    subs: List[str]
    queries: List[str] = Field(
        default_factory=lambda: [
            "SIP",
            "Groww SIP",
            "SIP stopped",
            "paused SIP",
            "cancel SIP",
            "SIP missed",
            "mutual fund beginner",
            "autopay OR mandate",
            "SIP band OR SIP chalu",
            "continued SIP OR 1 year SIP",
        ]
    )
    sorts: List[SortSlice] = Field(
        default_factory=lambda: [
            SortSlice(sort="new", t="all"),
            SortSlice(sort="top", t="year"),
            SortSlice(sort="comments", t="year"),
        ]
    )
    max_pages_per_stream: int = Field(3, ge=1, le=10)
    max_comments_per_thread: int = Field(30, ge=1)
    out_of_window_page_ratio_stop: float = Field(0.80, gt=0.0, le=1.0)
    request_delay_sec: float = Field(10.0, ge=0.0)
    rate_limit_cooldown_sec: float = Field(60.0, ge=0.0)
    format: Literal["auto", "json", "rss"] = "auto"
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )


# ----------------------------------------------------------------------------- text / filter helpers


def clean_rss_html(raw_html: str, *, is_post: bool = False) -> str:
    """Convert Reddit Atom <content type='html'> into clean plain text without the RSS footer."""
    if not raw_html:
        return ""
    s = html.unescape(raw_html)
    m = _MD_DIV_RE.search(s)
    if m:
        s = m.group(1)
    elif is_post:
        # Link-only or empty self-posts in RSS have no <div class="md"> block, only the "submitted by" footer.
        return ""
    s = _HTML_COMMENT_RE.sub(" ", s)
    s = _BLOCK_TAG_RE.sub("\n", s)
    s = _ANY_TAG_RE.sub("", s)
    s = html.unescape(s)
    if is_post:
        s = _SUBMITTED_FOOTER_RE.sub("", s)
    lines = [_WS_RE.sub(" ", line).strip() for line in s.splitlines()]
    s = "\n".join(line for line in lines if line)
    return _MULTI_NL_RE.sub("\n\n", s).strip()


def clean_username(raw_author: Optional[str]) -> str:
    if not raw_author:
        return "[deleted]"
    a = raw_author.strip()
    if a.startswith("/u/"):
        a = a[3:]
    elif a.startswith("u/"):
        a = a[2:]
    return a.strip() or "[deleted]"


def is_removed_or_deleted_body(text: Optional[str]) -> bool:
    if not text:
        return False
    norm = " ".join(text.strip().lower().split())
    return norm in _REMOVED_MARKERS


def is_bot_or_moderator(author: str, text: str, distinguished: Optional[str] = None) -> bool:
    if distinguished in ("moderator", "admin", "special"):
        return True
    a = clean_username(author).lower()
    if a not in ("", "[deleted]", "anonymous"):
        if a in _BOT_AUTHORS or a.endswith("bot"):
            return True
    low = (text or "").lower()
    return any(phrase in low for phrase in _BOT_PHRASES)


# ----------------------------------------------------------------------------- parsers


def parse_search_json(payload: Dict[str, Any], default_sub: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    data = (payload or {}).get("data") or {}
    children = data.get("children") or []
    posts: List[Dict[str, Any]] = []
    for child in children:
        if not isinstance(child, dict) or child.get("kind") != "t3":
            continue
        d = child.get("data") or {}
        short_id = str(d.get("id") or "")
        fullname = str(d.get("name") or (f"t3_{short_id}" if short_id else ""))
        if not fullname:
            continue
        created = d.get("created_utc")
        dt = datetime.fromtimestamp(float(created), tz=timezone.utc) if created is not None else None
        permalink = d.get("permalink") or f"/r/{d.get('subreddit') or default_sub}/comments/{short_id}/"
        url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else str(permalink)
        posts.append({
            "fullname": fullname,
            "short_id": short_id or fullname.replace("t3_", ""),
            "subreddit": str(d.get("subreddit") or default_sub),
            "title": (d.get("title") or "").strip(),
            "selftext": (d.get("selftext") or "").strip(),
            "author": clean_username(d.get("author")),
            "dt": dt,
            "url": url,
            "score": int(d["score"]) if d.get("score") is not None else None,
            "num_comments": int(d["num_comments"]) if d.get("num_comments") is not None else None,
            "flair": d.get("link_flair_text"),
            "distinguished": d.get("distinguished"),
            "format": "json",
        })
    after = data.get("after")
    if not after and posts:
        after = posts[-1]["fullname"]
    return posts, after


def parse_search_rss(xml_text: str, default_sub: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise TransientError(f"invalid Atom XML: {e}") from e
    posts: List[Dict[str, Any]] = []
    for entry in root.findall("a:entry", ATOM_NS):
        fullname = (entry.findtext("a:id", "", ATOM_NS) or "").strip()
        if not fullname.startswith("t3_"):
            continue
        short_id = fullname.replace("t3_", "")
        title = html.unescape(entry.findtext("a:title", "", ATOM_NS) or "").strip()
        raw_content = entry.findtext("a:content", "", ATOM_NS) or ""
        selftext = clean_rss_html(raw_content, is_post=True)
        author = clean_username(entry.findtext("a:author/a:name", "", ATOM_NS))
        dt_str = entry.findtext("a:published", "", ATOM_NS) or entry.findtext("a:updated", "", ATOM_NS)
        dt = to_utc(dt_str)
        link_el = entry.find("a:link", ATOM_NS)
        url = (link_el.attrib.get("href") if link_el is not None else None) or (
            f"https://www.reddit.com/r/{default_sub}/comments/{short_id}/"
        )
        cat_el = entry.find("a:category", ATOM_NS)
        sub = (cat_el.attrib.get("term") if cat_el is not None else None) or default_sub
        posts.append({
            "fullname": fullname,
            "short_id": short_id,
            "subreddit": sub,
            "title": title,
            "selftext": selftext,
            "author": author,
            "dt": dt,
            "url": url,
            "score": None,
            "num_comments": None,
            "flair": None,
            "distinguished": None,
            "format": "rss",
        })
    after = posts[-1]["fullname"] if posts else None
    return posts, after


def _flatten_json_comments(
    children: List[Any],
    *,
    sub: str,
    post_fullname: str,
    post_title: str,
    post_url: str,
    out: List[Dict[str, Any]],
) -> None:
    for child in children:
        if not isinstance(child, dict):
            continue
        kind = child.get("kind")
        if kind != "t1":
            continue
        d = child.get("data") or {}
        cid = str(d.get("id") or "")
        fullname = str(d.get("name") or (f"t1_{cid}" if cid else ""))
        if fullname:
            created = d.get("created_utc")
            dt = datetime.fromtimestamp(float(created), tz=timezone.utc) if created is not None else None
            permalink = d.get("permalink")
            url = (
                f"https://www.reddit.com{permalink}"
                if isinstance(permalink, str) and permalink.startswith("/")
                else f"{post_url.rstrip('/')}/{cid}/"
            )
            out.append({
                "fullname": fullname,
                "subreddit": str(d.get("subreddit") or sub),
                "post_id": post_fullname,
                "parent_id": d.get("parent_id") or post_fullname,
                "post_title": post_title,
                "body": (d.get("body") or "").strip(),
                "author": clean_username(d.get("author")),
                "dt": dt,
                "url": url,
                "score": int(d["score"]) if d.get("score") is not None else None,
                "distinguished": d.get("distinguished"),
                "format": "json",
            })
        replies = d.get("replies")
        if isinstance(replies, dict):
            rep_children = (replies.get("data") or {}).get("children") or []
            _flatten_json_comments(
                rep_children,
                sub=sub,
                post_fullname=post_fullname,
                post_title=post_title,
                post_url=post_url,
                out=out,
            )


def parse_thread_json(
    payload: Any, default_sub: str, short_id: str
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    if not isinstance(payload, list) or len(payload) < 1:
        raise TransientError("unexpected thread JSON structure")
    posts, _ = parse_search_json(payload[0], default_sub)
    post = posts[0] if posts else None
    post_fullname = post["fullname"] if post else f"t3_{short_id}"
    post_title = post["title"] if post else ""
    post_url = post["url"] if post else f"https://www.reddit.com/r/{default_sub}/comments/{short_id}/"
    comments: List[Dict[str, Any]] = []
    if len(payload) > 1 and isinstance(payload[1], dict):
        children = (payload[1].get("data") or {}).get("children") or []
        _flatten_json_comments(
            children,
            sub=post["subreddit"] if post else default_sub,
            post_fullname=post_fullname,
            post_title=post_title,
            post_url=post_url,
            out=comments,
        )
    return post, comments


def parse_thread_rss(
    xml_text: str, default_sub: str, short_id: str
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise TransientError(f"invalid thread Atom XML: {e}") from e
    post: Optional[Dict[str, Any]] = None
    comments: List[Dict[str, Any]] = []
    post_fullname = f"t3_{short_id}"
    post_title = ""
    post_url = f"https://www.reddit.com/r/{default_sub}/comments/{short_id}/"

    for entry in root.findall("a:entry", ATOM_NS):
        eid = (entry.findtext("a:id", "", ATOM_NS) or "").strip()
        link_el = entry.find("a:link", ATOM_NS)
        href = (link_el.attrib.get("href") if link_el is not None else None) or ""
        cat_el = entry.find("a:category", ATOM_NS)
        sub = (cat_el.attrib.get("term") if cat_el is not None else None) or default_sub
        author = clean_username(entry.findtext("a:author/a:name", "", ATOM_NS))
        dt_str = entry.findtext("a:published", "", ATOM_NS) or entry.findtext("a:updated", "", ATOM_NS)
        dt = to_utc(dt_str)
        raw_content = entry.findtext("a:content", "", ATOM_NS) or ""

        if eid.startswith("t3_"):
            post_fullname = eid
            post_title = html.unescape(entry.findtext("a:title", "", ATOM_NS) or "").strip()
            post_url = href or post_url
            post = {
                "fullname": post_fullname,
                "short_id": eid.replace("t3_", ""),
                "subreddit": sub,
                "title": post_title,
                "selftext": clean_rss_html(raw_content, is_post=True),
                "author": author,
                "dt": dt,
                "url": post_url,
                "score": None,
                "num_comments": None,
                "flair": None,
                "distinguished": None,
                "format": "rss",
            }
        elif eid.startswith("t1_"):
            cid = eid.replace("t1_", "")
            comments.append({
                "fullname": eid,
                "subreddit": sub,
                "post_id": post_fullname,
                "parent_id": post_fullname,
                "post_title": post_title,
                "body": clean_rss_html(raw_content, is_post=False),
                "author": author,
                "dt": dt,
                "url": href or f"{post_url.rstrip('/')}/{cid}/",
                "score": None,
                "distinguished": None,
                "format": "rss",
            })
    return post, comments


# ----------------------------------------------------------------------------- collector


class _RateLimitError(TransientError):
    def __init__(self, msg: str, retry_after: Optional[float] = None):
        super().__init__(msg)
        self.retry_after = retry_after


class RedditCollector(BaseCollector):
    source = "reddit"

    def __init__(self, settings, *, http_get: Optional[Callable[..., Any]] = None,
                 subs_override: Optional[List[str]] = None, **kwargs):
        super().__init__(settings, **kwargs)
        self.cfg = RedditCfg.model_validate(settings.sources["reddit"])
        if subs_override:
            self.cfg.subs = list(subs_override)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self.cfg.user_agent,
            "Accept": "application/json,application/atom+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._http_get = http_get
        self._next_pause_sec: float = 0.0
        self.active_format: str = (
            self.state.get("active_format")
            or ("rss" if self.cfg.format == "rss" else "json")
        )
        self.state["active_format"] = self.active_format
        self.state.setdefault("streams", {})
        self.state.setdefault("pending_threads", {})   # fullname -> {"sub": ..., "short_id": ..., "title": ..., "num_comments": ...}
        self.state.setdefault("done_threads", [])
        self.state.setdefault("per_sub", {})
        self.state.setdefault("thread_coverage", {
            "threads_fetched": 0,
            "total_comments_returned": 0,
            "threads_with_num_comments": 0,
            "total_num_comments_reported": 0,
            "samples": [],
        })

    # ------------------------------------------------------------------ HTTP & fallback

    def _requests_made(self) -> int:
        return self.state["counters"].get("requests_made", 0)

    def _limit_reached(self) -> bool:
        return self.scan_limit is not None and self._requests_made() >= self.scan_limit

    @staticmethod
    def _get_header_ci(headers: Optional[Dict[str, Any]], key: str) -> Optional[str]:
        if not headers:
            return None
        target = key.lower()
        for k, v in headers.items():
            if str(k).lower() == target and v is not None:
                return str(v)
        return None

    @classmethod
    def _parse_reset_seconds(cls, headers: Optional[Dict[str, Any]]) -> float:
        """Extract the largest positive Retry-After / x-ratelimit-reset value in seconds."""
        best = 0.0
        for hname in ("Retry-After", "x-ratelimit-reset"):
            raw = cls._get_header_ci(headers, hname)
            if raw is None:
                continue
            try:
                val = float(raw.strip())
                if val > best:
                    best = val
            except (ValueError, TypeError):
                continue
        return best

    def _polite_pause(self) -> None:
        pause = max(self.cfg.request_delay_sec, self._next_pause_sec)
        self._next_pause_sec = 0.0
        if pause > 0:
            self.sleep(pause)

    def _fetch_raw(self, url_template: str, **fmt_kwargs) -> Tuple[str, Any]:
        """Fetch URL using active_format; on HTTP 403 in 'auto' mode, switch to 'rss' and retry."""
        get = self._http_get or self._session.get
        max_attempts = max(self.collect_cfg.max_retries, 4)
        attempt = 0
        had_429 = False
        last_429_reset = 0.0

        while True:
            ext = self.active_format
            url = url_template.format(ext=ext, **fmt_kwargs)
            try:
                resp = get(url, timeout=self.collect_cfg.request_timeout_sec)
            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt >= max_attempts:
                    raise CollectorError(f"reddit network error after {attempt + 1} attempts: {e}") from e
                delay = self.collect_cfg.backoff_base_sec * (2 ** attempt)
                log.warning("reddit timeout/conn error (%s); retry %d/%d in %.0fs", e, attempt + 1, max_attempts, delay)
                self.sleep(delay)
                attempt += 1
                continue

            self.count("requests_made")
            code = resp.status_code

            # Check for 403 WAF block on .json -> auto-switch to .rss
            if code == 403 and ext == "json" and self.cfg.format == "auto":
                log.warning("reddit .json returned HTTP 403 (WAF block); switching transport to .rss for this run")
                self.active_format = "rss"
                self.state["active_format"] = "rss"
                self.state["format_fallback"] = "json_to_rss"
                continue

            if code == 429:
                if attempt >= max_attempts:
                    raise CollectorError(f"reddit HTTP 429 persisted after {attempt + 1} attempts on {url}")
                hdr_wait = self._parse_reset_seconds(resp.headers)
                min_cd = max(MIN_RATE_LIMIT_COOLDOWN_SEC, self.cfg.rate_limit_cooldown_sec)
                cooldown = max(min_cd * (2 ** attempt), hdr_wait)
                had_429 = True
                last_429_reset = max(min_cd, hdr_wait)
                log.warning(
                    "reddit HTTP 429 rate limit; sleeping %.0fs (retry %d/%d, header_reset=%.0fs)",
                    cooldown,
                    attempt + 1,
                    max_attempts,
                    hdr_wait,
                )
                self.sleep(cooldown)
                attempt += 1
                continue

            if code >= 500:
                if attempt >= max_attempts:
                    raise CollectorError(f"reddit HTTP {code} after {attempt + 1} attempts on {url}")
                delay = self.collect_cfg.backoff_base_sec * (2 ** attempt)
                log.warning("reddit HTTP %d; retry %d/%d in %.0fs", code, attempt + 1, max_attempts, delay)
                self.sleep(delay)
                attempt += 1
                continue

            if code >= 400:
                raise CollectorError(f"reddit HTTP {code} on {url}")

            # After a 429 (or when ratelimit-remaining is nearly exhausted), wait the full reset window
            # before the next request.
            resp_reset = self._parse_reset_seconds(resp.headers)
            if had_429:
                min_cd = max(MIN_RATE_LIMIT_COOLDOWN_SEC, self.cfg.rate_limit_cooldown_sec)
                self._next_pause_sec = max(self._next_pause_sec, min_cd, last_429_reset, resp_reset)
            elif resp_reset > 0:
                rem_raw = self._get_header_ci(resp.headers, "x-ratelimit-remaining")
                try:
                    if rem_raw is not None and float(rem_raw.strip()) <= 1.0:
                        self._next_pause_sec = max(self._next_pause_sec, resp_reset)
                except (ValueError, TypeError):
                    pass

            if ext == "json":
                try:
                    return "json", resp.json()
                except ValueError as e:
                    if self.cfg.format == "auto":
                        log.warning("reddit .json returned non-JSON body; switching to .rss")
                        self.active_format = "rss"
                        self.state["active_format"] = "rss"
                        self.state["format_fallback"] = "json_to_rss"
                        continue
                    raise CollectorError(f"invalid JSON from {url}: {e}") from e
            else:
                return "rss", resp.text

    # ------------------------------------------------------------------ record builders

    def _sub_stats(self, sub: str) -> Dict[str, int]:
        return self.state["per_sub"].setdefault(sub, {
            "posts_scanned": 0,
            "posts_matched": 0,
            "comments_scanned": 0,
            "comments_matched": 0,
            "threads_fetched": 0,
        })

    def _process_post(self, p: Dict[str, Any]) -> Tuple[Optional[RawPost], bool]:
        """Validate/match a submission. Returns (RawPost_or_None, should_queue_thread)."""
        sub = p["subreddit"]
        sub_st = self._sub_stats(sub)
        sub_st["posts_scanned"] += 1
        self.count("scanned")
        self.count("posts_scanned")

        dt = p.get("dt")
        if not self.in_window(dt):
            self.drop("out_of_window")
            return None, False

        title = (p.get("title") or "").strip()
        selftext = (p.get("selftext") or "").strip()
        body_removed = is_removed_or_deleted_body(selftext)

        if is_bot_or_moderator(p.get("author", ""), f"{title}\n{selftext}", p.get("distinguished")):
            self.drop("bot_or_mod")
            return None, False

        # Even if a post's body was [removed]/[deleted], if its title is in-window and matches SIP terms,
        # we still queue the thread so public comments inside it are harvested!
        title_match = self.match(title)
        title_has_anchor = self.matcher.has_anchor(title)

        if body_removed:
            self.drop("deleted_or_removed")
            return None, (title_match.matched or title_has_anchor)

        full_text = compose_text(title, selftext)
        if not full_text or is_removed_or_deleted_body(full_text):
            self.drop("empty_text")
            return None, False

        m = self.match(full_text)
        should_queue = m.matched or title_has_anchor or self.matcher.has_anchor(full_text)

        fullname = p["fullname"]
        if f"{self.source}:{fullname}" in self.existing_ids:
            self.drop("duplicate")
            return None, should_queue

        if not m.matched:
            self.drop("no_keyword_match")
            return None, should_queue

        raw_post = self.make_post(
            native_id=fullname,
            url=p["url"],
            dt=dt,
            username=p.get("author") or "[deleted]",
            text=full_text,
            match=m,
            raw_meta={
                "record_type": "post",
                "subreddit": sub,
                "post_id": fullname,
                "parent_id": None,
                "title": title,
                "score": p.get("score"),
                "num_comments": p.get("num_comments"),
                "flair": p.get("flair"),
                "format": p.get("format"),
                "matched_via": m.matched_via,
            },
        )
        sub_st["posts_matched"] += 1
        self.count("matched")
        self.count("posts_matched")
        return raw_post, True

    def _process_thread_comments(
        self,
        sub: str,
        post_fullname: str,
        post_title: str,
        post_selftext: str,
        comments: List[Dict[str, Any]],
        num_comments_hint: Optional[int],
    ) -> List[RawPost]:
        sub_st = self._sub_stats(sub)
        sub_st["threads_fetched"] += 1
        cov = self.state["thread_coverage"]
        cov["threads_fetched"] += 1
        cov["total_comments_returned"] += len(comments)
        if num_comments_hint is not None:
            cov["threads_with_num_comments"] += 1
            cov["total_num_comments_reported"] += int(num_comments_hint)
        if len(cov["samples"]) < 15:
            cov["samples"].append({
                "post_id": post_fullname,
                "subreddit": sub,
                "comments_returned": len(comments),
                "num_comments": num_comments_hint,
            })

        thread_has_anchor = self.matcher.has_anchor(post_title) or self.matcher.has_anchor(post_selftext)
        candidates: List[Tuple[Optional[int], datetime, Dict[str, Any], Any]] = []

        for c in comments:
            sub_st["comments_scanned"] += 1
            self.count("scanned")
            self.count("comments_scanned")

            dt = c.get("dt")
            if not self.in_window(dt):
                self.drop("out_of_window")
                continue

            body = (c.get("body") or "").strip()
            if not body:
                self.drop("empty_text")
                continue
            if is_removed_or_deleted_body(body):
                self.drop("deleted_or_removed")
                continue
            if is_bot_or_moderator(c.get("author", ""), body, c.get("distinguished")):
                self.drop("bot_or_mod")
                continue

            fullname = c["fullname"]
            if f"{self.source}:{fullname}" in self.existing_ids:
                self.drop("duplicate")
                continue

            m = self.matcher.match_with_context(body, thread_has_anchor=thread_has_anchor)
            if not m.matched:
                self.drop("no_keyword_match")
                continue

            candidates.append((c.get("score"), dt, c, m))

        # Per-thread cap (default 30): keep highest-scored when scores exist, else earliest by timestamp
        cap = self.cfg.max_comments_per_thread
        if len(candidates) > cap:
            has_scores = any(score is not None for score, _, _, _ in candidates)
            if has_scores:
                candidates.sort(key=lambda item: (-(item[0] if item[0] is not None else -10**9), item[1]))
            else:
                candidates.sort(key=lambda item: item[1])
            capped_count = len(candidates) - cap
            candidates = candidates[:cap]
            for _ in range(capped_count):
                self.drop("thread_comment_cap")
            self.count("comments_capped", capped_count)

        out: List[RawPost] = []
        for score, dt, c, m in candidates:
            post = self.make_post(
                native_id=c["fullname"],
                url=c["url"],
                dt=dt,
                username=c.get("author") or "[deleted]",
                text=c["body"],
                match=m,
                raw_meta={
                    "record_type": "comment",
                    "subreddit": c.get("subreddit") or sub,
                    "post_id": post_fullname,
                    "parent_id": c.get("parent_id") or post_fullname,
                    "post_title": post_title,
                    "score": score,
                    "format": c.get("format"),
                    "matched_via": m.matched_via,
                },
            )
            out.append(post)
            sub_st["comments_matched"] += 1
            self.count("matched")
            self.count("comments_matched")
            self.count(f"matched_via_{m.matched_via}")
        return out

    # ------------------------------------------------------------------ main generator

    def _queue_thread(self, p: Dict[str, Any]) -> None:
        fullname = p["fullname"]
        if fullname in self.state["done_threads"] or fullname in self.state["pending_threads"]:
            return
        self.state["pending_threads"][fullname] = {
            "sub": p["subreddit"],
            "short_id": p["short_id"],
            "title": p.get("title") or "",
            "num_comments": p.get("num_comments"),
        }

    def iter_pages(self) -> Iterator[List[RawPost]]:
        streams = self.state["streams"]

        # Phase A: Search discovery across (sub, sort, query) — prefer sort=new across all queries first
        for sub in self.cfg.subs:
            for sl in self.cfg.sorts:
                for q in self.cfg.queries:
                    if self._limit_reached():
                        log.info("reddit reached request limit (%d); stopping discovery", self.scan_limit)
                        yield []
                        return
                    skey = f"{sub}|{q}|{sl.sort}|{sl.t}"
                    st = streams.setdefault(skey, {"after": None, "pages": 0, "done": False})
                    while not st["done"] and st["pages"] < self.cfg.max_pages_per_stream:
                        if self._limit_reached():
                            yield []
                            return
                        after_param = f"&after={st['after']}" if st["after"] else ""
                        fmt, payload = self._fetch_raw(
                            SEARCH_URL,
                            sub=sub,
                            q=quote_plus(q),
                            sort=sl.sort,
                            t=sl.t,
                            after=after_param,
                        )
                        entries, next_after = (
                            parse_search_json(payload, sub)
                            if fmt == "json"
                            else parse_search_rss(payload, sub)
                        )
                        page_posts: List[RawPost] = []
                        old_streak = 0
                        out_of_window_on_page = 0
                        for p in entries:
                            in_win = self.in_window(p.get("dt"))
                            if not in_win:
                                out_of_window_on_page += 1
                            raw_post, should_queue = self._process_post(p)
                            if raw_post is not None:
                                page_posts.append(raw_post)
                            if should_queue:
                                self._queue_thread(p)
                            if sl.sort == "new" and not in_win:
                                old_streak += 1
                            else:
                                old_streak = 0

                        st["pages"] += 1
                        out_of_window_ratio = (out_of_window_on_page / len(entries)) if entries else 0.0
                        over_out_of_window_ratio = (
                            bool(entries) and out_of_window_ratio > self.cfg.out_of_window_page_ratio_stop
                        )
                        if over_out_of_window_ratio:
                            self.count("slices_stopped_out_of_window")
                            log.info(
                                "reddit slice r/%s q=%r (%s/%s) p%d is %.0f%% out of window (%d/%d > %.0f%%); stopping slice",
                                sub,
                                q,
                                sl.sort,
                                sl.t,
                                st["pages"],
                                out_of_window_ratio * 100.0,
                                out_of_window_on_page,
                                len(entries),
                                self.cfg.out_of_window_page_ratio_stop * 100.0,
                            )
                        if (
                            not entries
                            or not next_after
                            or next_after == st["after"]
                            or over_out_of_window_ratio
                            or (sl.sort == "new" and old_streak >= self.collect_cfg.old_streak_stop)
                            or st["pages"] >= self.cfg.max_pages_per_stream
                        ):
                            st["done"] = True
                        st["after"] = next_after
                        log.info(
                            "reddit search r/%s q=%r (%s/%s) p%d: %d entries, %d matched posts, %d threads queued",
                            sub,
                            q,
                            sl.sort,
                            sl.t,
                            st["pages"],
                            len(entries),
                            len(page_posts),
                            len(self.state["pending_threads"]),
                        )
                        yield page_posts
                        self._polite_pause()

                        # Interleave up to 2 pending thread fetches per search page so even short/capped runs
                        # harvest both posts and comments!
                        for _ in range(2):
                            if not self.state["pending_threads"] or self._limit_reached():
                                break
                            yield self._harvest_one_thread()
                            self._polite_pause()

        # Phase B: Harvest any remaining queued threads
        while self.state["pending_threads"]:
            if self._limit_reached():
                log.info("reddit reached request limit (%d) during thread harvesting", self.scan_limit)
                yield []
                return
            yield self._harvest_one_thread()
            self._polite_pause()

    def _harvest_one_thread(self) -> List[RawPost]:
        fullname = next(iter(self.state["pending_threads"]))
        info = self.state["pending_threads"][fullname]
        sub = info["sub"]
        short_id = info["short_id"]

        fmt, payload = self._fetch_raw(THREAD_URL, sub=sub, short_id=short_id)
        post_dict, comments = (
            parse_thread_json(payload, sub, short_id)
            if fmt == "json"
            else parse_thread_rss(payload, sub, short_id)
        )

        out: List[RawPost] = []
        post_title = info.get("title") or ""
        post_selftext = ""
        num_comments = info.get("num_comments")

        if post_dict is not None:
            post_title = post_dict.get("title") or post_title
            post_selftext = post_dict.get("selftext") or ""
            if post_dict.get("num_comments") is not None:
                num_comments = post_dict["num_comments"]
            if f"{self.source}:{fullname}" not in self.existing_ids:
                rp, _ = self._process_post(post_dict)
                if rp is not None:
                    out.append(rp)

        comment_posts = self._process_thread_comments(
            sub=sub,
            post_fullname=fullname,
            post_title=post_title,
            post_selftext=post_selftext,
            comments=comments,
            num_comments_hint=num_comments,
        )
        out.extend(comment_posts)

        self.state["pending_threads"].pop(fullname, None)
        if fullname not in self.state["done_threads"]:
            self.state["done_threads"].append(fullname)

        log.info(
            "reddit thread r/%s %s: %d comments returned, %d matched (pending threads: %d)",
            sub,
            short_id,
            len(comments),
            len(comment_posts),
            len(self.state["pending_threads"]),
        )
        return out

    def extra_stats(self) -> Dict[str, Any]:
        c = self.state["counters"]
        return {
            "active_format": self.active_format,
            "format_fallback": self.state.get("format_fallback"),
            "requests_made": c.get("requests_made", 0),
            "posts_scanned": c.get("posts_scanned", 0),
            "posts_matched": c.get("posts_matched", 0),
            "comments_scanned": c.get("comments_scanned", 0),
            "comments_matched": c.get("comments_matched", 0),
            "comments_capped": c.get("comments_capped", 0),
            "slices_stopped_out_of_window": c.get("slices_stopped_out_of_window", 0),
            "matched_via": {
                "self": c.get("matched_via_self", 0),
                "thread_context": c.get("matched_via_thread_context", 0),
            },
            "threads_pending": len(self.state["pending_threads"]),
            "threads_done": len(self.state["done_threads"]),
            "thread_coverage": self.state["thread_coverage"],
            "per_sub": self.state["per_sub"],
        }
