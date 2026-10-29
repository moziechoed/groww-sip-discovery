"""App Store collector: iTunes customer-reviews RSS (JSON), most recent first.

- Apple exposes at most 10 pages x 50 reviews (~500). Accepted (PRD v1.1 change #4); the shortfall
  and the oldest review date reached are logged in corpus_stats.json.
- text = "{title}. {content}" unless the title is already in the body; the title is also kept in
  raw_meta.title. Evidence spans are later verified against this final text field.
- No per-review public URL exists, so url points to the app's reviews page; raw_meta keeps the review ID.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Optional

import requests
from pydantic import BaseModel, ConfigDict, Field

from core.logger import get_logger
from core.schemas import RawPost

from collectors.base import BaseCollector, CollectorError, TransientError, to_utc

log = get_logger("collect.app_store")

RSS_URL = "https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json"
RSS_CAP_ENTRIES = 500
_WS = re.compile(r"\s+")


class AppStoreCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    priority: int = 2
    app_id: str
    country: str = "in"
    max_pages: int = Field(10, ge=1, le=10)
    cap_note: Optional[str] = None


def compose_text(title: Optional[str], content: Optional[str]) -> str:
    title, content = (title or "").strip(), (content or "").strip()
    if not title:
        return content
    if not content:
        return title
    if _WS.sub(" ", title).lower() in _WS.sub(" ", content).lower():
        return content
    sep = "" if title[-1] in ".!?" else "."   # avoid "Great!. ..."
    return f"{title}{sep} {content}"


def _label(entry: Dict[str, Any], *keys: str) -> Optional[str]:
    node: Any = entry
    for k in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    if isinstance(node, dict):
        node = node.get("label")
    return node if isinstance(node, str) else None


class _EndOfFeed(Exception):
    pass


class AppStoreCollector(BaseCollector):
    source = "app_store"

    def __init__(self, settings, *, http_get=None, **kwargs):
        super().__init__(settings, **kwargs)
        self.cfg = AppStoreCfg.model_validate(settings.sources["app_store"])
        self._http_get = http_get
        self.state.setdefault("pages_done", [])
        self.state.setdefault("per_star", {})
        self.state.setdefault("end_reason", None)

    def is_retryable(self, exc: Exception) -> bool:
        return isinstance(exc, (TransientError, requests.Timeout, requests.ConnectionError))

    def _get_page(self, page: int) -> List[Dict[str, Any]]:
        url = RSS_URL.format(country=self.cfg.country, page=page, app_id=self.cfg.app_id)
        get = self._http_get or requests.get
        resp = get(url, timeout=self.collect_cfg.request_timeout_sec)
        code = resp.status_code
        if code == 429 or code >= 500:
            raise TransientError(f"HTTP {code}")
        if code >= 400:
            if page > 1:
                raise _EndOfFeed(f"HTTP {code} on page {page}")
            raise CollectorError(f"HTTP {code} on page 1 (check app_id/country)")
        try:
            data = resp.json()
        except ValueError as e:
            raise TransientError(f"invalid JSON: {e}") from e
        entries = (data.get("feed") or {}).get("entry") or []
        if isinstance(entries, dict):   # a single entry comes back as an object, not a list
            entries = [entries]
        return [e for e in entries if isinstance(e, dict) and "im:rating" in e]   # skip app-metadata entry

    def iter_pages(self) -> Iterator[List[RawPost]]:
        if self.state["end_reason"]:
            log.info("app_store already complete (%s); delete %s to re-collect", self.state["end_reason"],
                     self.state_path)
            return
        for page in range(1, self.cfg.max_pages + 1):
            if page in self.state["pages_done"]:
                continue
            if self.scan_limit and self.state["counters"].get("scanned", 0) >= self.scan_limit:
                self.state["end_reason"] = "scan_limit"
                yield []
                return
            try:
                entries = self.call_with_retries(lambda: self._get_page(page), f"app_store RSS page {page}")
            except CollectorError as e:
                if isinstance(e.__cause__, _EndOfFeed):
                    entries = []
                else:
                    raise
            if not entries:
                self.state["end_reason"] = "end_of_feed"
                yield []
                return
            posts = self._process(entries)
            self.state["pages_done"].append(page)
            log.info("app_store page %d: %d entries, %d matched (total scanned %d)", page, len(entries),
                     len(posts), self.state["counters"].get("scanned", 0))
            yield posts
            self.pause()
        self.state["end_reason"] = "rss_page_limit"
        yield []

    def _process(self, entries: List[Dict[str, Any]]) -> List[RawPost]:
        posts: List[RawPost] = []
        for e in entries:
            if self.scan_limit and self.state["counters"].get("scanned", 0) >= self.scan_limit:
                break
            self.count("scanned")
            try:
                score: Optional[int] = int(_label(e, "im:rating") or 0) or None
            except ValueError:
                score = None
            star = self.state["per_star"].setdefault(str(score), {"scanned": 0, "matched": 0})
            star["scanned"] += 1
            dt = to_utc(_label(e, "updated"))
            if dt is not None:
                d = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                if not self.state.get("oldest_scanned_date") or d < self.state["oldest_scanned_date"]:
                    self.state["oldest_scanned_date"] = d
            if not self.in_window(dt):
                self.drop("out_of_window")
                continue
            title = _label(e, "title")
            text = compose_text(title, _label(e, "content"))
            if not text:
                self.drop("empty_text")
                continue
            rid = _label(e, "id") or ""
            if not rid or f"{self.source}:{rid}" in self.existing_ids:
                self.drop("duplicate")
                continue
            m = self.match(text)
            if not m.matched:
                self.drop("no_keyword_match")
                continue
            posts.append(self.make_post(
                native_id=rid,
                url=f"https://apps.apple.com/{self.cfg.country}/app/id{self.cfg.app_id}?see-all=reviews",
                dt=dt,
                username=_label(e, "author", "name") or "",
                text=text,
                match=m,
                raw_meta={
                    "score": score,
                    "title": title,
                    "version": _label(e, "im:version"),
                    "vote_sum": int(_label(e, "im:voteSum") or 0),
                    "vote_count": int(_label(e, "im:voteCount") or 0),
                },
            ))
            star["matched"] += 1
            self.count("matched")
        return posts

    def extra_stats(self) -> Dict[str, Any]:
        scanned = self.state["counters"].get("scanned", 0)
        return {
            "app_id": self.cfg.app_id,
            "pages_fetched": len(self.state["pages_done"]),
            "end_reason": self.state["end_reason"],
            "per_star": self.state["per_star"],
            "oldest_scanned_date": self.state.get("oldest_scanned_date"),
            "rss_cap_entries": RSS_CAP_ENTRIES,
            "shortfall_vs_cap": max(0, RSS_CAP_ENTRIES - scanned),
            "shortfall_note": (f"Apple's RSS feed exposes at most ~{RSS_CAP_ENTRIES} most recent reviews "
                               f"(10 pages x 50). {scanned} scanned, {self.state['counters'].get('matched', 0)} "
                               f"matched SIP/MF terms; oldest review reached: {self.state.get('oldest_scanned_date')}. "
                               "Accepted per PRD (App Store cap); compensated by other sources."),
        }
