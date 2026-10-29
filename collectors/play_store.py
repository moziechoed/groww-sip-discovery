"""Play Store collector (google-play-scraper), stratified by star rating.

- Newest-first, one stream per star rating (1–5). Pages are fetched round-robin across stars so the
  matched cap is spread across ratings instead of being used up by recent 1-star complaints.
- Each star stops at max_scan_per_star, or once old_streak_stop consecutive reviews fall outside the
  24-month window, or when the store has no more reviews. Collection stops at max_matched_total.
- text = review body only. Developer replies are excluded so Groww support text never becomes a quote.
- Per-star scanned vs matched counts are reported (stratification changes the natural rating mix).
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterator, List, Optional, Tuple

from google_play_scraper import Sort
from google_play_scraper import reviews as gp_reviews
from pydantic import BaseModel, ConfigDict, Field

from core.logger import get_logger
from core.schemas import RawPost

from collectors.base import BaseCollector, CollectorError, TransientError, to_utc

try:  # internal class; needed to resume a stream from a saved token string
    from google_play_scraper.features.reviews import _ContinuationToken
except ImportError:  # pragma: no cover
    _ContinuationToken = None

log = get_logger("collect.play_store")


class PlayStoreCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    priority: int = 1
    app_id: str
    lang: str = "en"
    country: str = "in"
    stars: List[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    max_scan_per_star: int = Field(12000, ge=1)
    max_matched_total: int = Field(2500, ge=1)
    page_size: int = Field(200, ge=1, le=4500)


class PlayStoreCollector(BaseCollector):
    source = "play_store"

    def __init__(self, settings, *, fetch=None, **kwargs):
        super().__init__(settings, **kwargs)
        self.cfg = PlayStoreCfg.model_validate(settings.sources["play_store"])
        self._fetch = fetch
        self.per_star_cap = (math.ceil(self.scan_limit / len(self.cfg.stars)) if self.scan_limit
                             else self.cfg.max_scan_per_star)
        streams = self.state.setdefault("streams", {})
        for s in self.cfg.stars:
            streams.setdefault(str(s), {"started": False, "token": None, "scanned": 0, "matched": 0,
                                        "old_streak": 0, "done": False, "end_reason": None})

    # ------------------------------------------------------------------ fetching

    def is_retryable(self, exc: Exception) -> bool:
        # The library swallows most HTTP errors and returns an empty page; anything it does raise
        # (parsing, connection) is treated as transient.
        return True

    def _fetch_page(self, star: int, st: Dict[str, Any], count: int) -> Tuple[List[dict], Optional[str]]:
        fetch = self._fetch or gp_reviews
        token_obj = None
        if st["started"]:
            if _ContinuationToken is None:
                raise CollectorError("google-play-scraper continuation token class not found; cannot resume")
            token_obj = _ContinuationToken(st["token"], self.cfg.lang, self.cfg.country, Sort.NEWEST.value,
                                           count, star, None)

        def call():
            if token_obj is not None:
                items, nxt = fetch(self.cfg.app_id, continuation_token=token_obj)
            else:
                items, nxt = fetch(self.cfg.app_id, lang=self.cfg.lang, country=self.cfg.country,
                                   sort=Sort.NEWEST, count=count, filter_score_with=star)
            if not items:
                # The library returns [] on HTTP errors as well as at the true end, so retry first.
                raise TransientError("empty page")
            return items, getattr(nxt, "token", None)

        try:
            return self.call_with_retries(call, f"play_store ★{star} page")
        except CollectorError as e:
            if isinstance(e.__cause__, TransientError) and "empty page" in str(e.__cause__):
                st["end_reason"] = "empty_after_retries" if st["started"] else "no_reviews"
                return [], None
            raise

    # ------------------------------------------------------------------ main loop

    def _total_matched(self) -> int:
        return sum(self.state["streams"][str(s)]["matched"] for s in self.cfg.stars)

    def _finish(self, st: Dict[str, Any], reason: str) -> None:
        st["done"] = True
        st["end_reason"] = st["end_reason"] or reason

    def iter_pages(self) -> Iterator[List[RawPost]]:
        streams = self.state["streams"]
        if all(streams[str(s)]["done"] for s in self.cfg.stars):
            log.info("play_store already complete; delete %s to re-collect (existing rows are kept and deduped)",
                     self.state_path)
        while True:
            active = [s for s in self.cfg.stars if not streams[str(s)]["done"]]
            if not active:
                return
            for star in active:
                st = streams[str(star)]
                if self._total_matched() >= self.cfg.max_matched_total:
                    for s in active:
                        self._finish(streams[str(s)], "matched_total_cap")
                    yield []
                    return
                remaining = self.per_star_cap - st["scanned"]
                if remaining <= 0:
                    self._finish(st, "scan_limit" if self.scan_limit else "scan_cap")
                    continue
                items, next_token = self._fetch_page(star, st, min(self.cfg.page_size, remaining))
                posts = self._process(star, st, items)
                st["started"] = True
                st["token"] = next_token
                if not st["done"] and next_token is None:
                    self._finish(st, "end_of_reviews")
                log.info("play_store ★%d: scanned %d/%d, matched %d (total matched %d/%d)%s", star,
                         st["scanned"], self.per_star_cap, st["matched"], self._total_matched(),
                         self.cfg.max_matched_total, f" — done: {st['end_reason']}" if st["done"] else "")
                yield posts
                self.pause()

    def _process(self, star: int, st: Dict[str, Any], items: List[dict]) -> List[RawPost]:
        posts: List[RawPost] = []
        for r in items:
            if self._total_matched() >= self.cfg.max_matched_total:
                break
            st["scanned"] += 1
            self.count("scanned")
            dt = to_utc(r.get("at"))
            if not self.in_window(dt):
                self.drop("out_of_window")
                st["old_streak"] += 1
                if st["old_streak"] >= self.collect_cfg.old_streak_stop:
                    self._finish(st, "window_cutoff")
                    break
                continue
            st["old_streak"] = 0
            text = (r.get("content") or "").strip()
            if not text:
                self.drop("empty_text")
                continue
            rid = str(r.get("reviewId") or "")
            if not rid or f"{self.source}:{rid}" in self.existing_ids:
                self.drop("duplicate")
                continue
            m = self.match(text)
            if not m.matched:
                self.drop("no_keyword_match")
                continue
            posts.append(self.make_post(
                native_id=rid,
                url=f"https://play.google.com/store/apps/details?id={self.cfg.app_id}&reviewId={rid}",
                dt=dt,
                username=r.get("userName") or "",
                text=text,
                match=m,
                raw_meta={
                    "score": r.get("score"),
                    "star_stratum": star,
                    "thumbs_up_count": r.get("thumbsUpCount"),
                    "review_created_version": r.get("reviewCreatedVersion"),
                    "app_version": r.get("appVersion"),
                    "has_developer_reply": bool(r.get("replyContent")),
                },
            ))
            st["matched"] += 1
            self.count("matched")
        return posts

    def extra_stats(self) -> Dict[str, Any]:
        streams = self.state["streams"]
        return {
            "app_id": self.cfg.app_id,
            "per_star_cap": self.per_star_cap,
            "max_matched_total": self.cfg.max_matched_total,
            "per_star": {str(s): {"scanned": streams[str(s)]["scanned"], "matched": streams[str(s)]["matched"],
                                  "end_reason": streams[str(s)]["end_reason"]} for s in self.cfg.stars},
            "rating_mix_note": ("Collected with equal-effort sampling per star rating (1–5), not the store's natural "
                                "rating mix. Disclose in the Evidence Log."),
        }
