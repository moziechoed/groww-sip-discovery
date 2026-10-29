"""YouTube Data API v3 collector (Stage 1, Priority 2 — PRD §9.1, Architecture §4.3).

Two-phase resumable collection within the 10,000-unit/day quota:
  1. Channel Uploads Playlist scan (1 unit/call):
     - Resolves each channel handle via `channels.list` (1 unit, cached in checkpoint).
     - Scans `playlistItems.list` (1 unit per 50 uploads, newest-first) within the 24-month window.
     - Queues a video ONLY if its title or lead description (`description[:250]`) contains a whole-word
       SIP/MF anchor (`KeywordMatcher.has_anchor`).
  2. Targeted `search.list` supplement (100 units/call, capped at 6 queries):
     - Finds cross-channel Indian SIP continuation/drop-off videos in the 24-month window.
  3. Round-robin comment & inline-reply harvesting (`commentThreads.list`, 1 unit/page):
     - Rotates round-robin across channels so every kept channel contributes evenly (no single channel starves others).
     - Fetches up to `max_comment_pages_per_video` pages (100 top-level comments + inline replies/page).
     - Filters out creator/channel-owner comments (`authorChannelId == video_channel_id`), promo/scam spam,
       and out-of-window comments.
     - Inside a SIP/MF-anchored video, matches comments via:
       (a) `KeywordMatcher.match_with_context(..., thread_has_anchor=True)`, OR
       (b) First-person marker (`i`, `my`, `mine`, `me`, `maine`, `mera`, `meri`, `mujhe`) + behaviour term
           (`stop/stopped`, `pause/paused`, `cancel`, `continue/continuing`, `started`, `withdraw`, `redeem`,
            `returns`, `loss`, `profit`, `salary`, `emergency`, `months`, `years`, `invest/investing`)
           -> tagged `matched_via = "thread_context"`.
     - Caps kept comments at `max_comments_per_video` (40, by `like_count DESC, publishedAt ASC`),
       `max_comments_per_channel` (200), and `max_matched_total` (1,500).
     - Stops cleanly if `daily_quota_cap_units` is reached or the API returns HTTP 403 `quotaExceeded`.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from pydantic import BaseModel, ConfigDict, Field

from collectors.base import BaseCollector, CollectorError, to_utc
from collectors.matcher import KeywordMatcher, MatchResult, match_anchored_reply
from core.config import Settings, get_api_key
from core.logger import get_logger
from core.schemas import RawPost

log = get_logger("collect.youtube")

YT_API_BASE = "https://www.googleapis.com/youtube/v3"
CHANNELS_URL = f"{YT_API_BASE}/channels"
PLAYLIST_ITEMS_URL = f"{YT_API_BASE}/playlistItems"
SEARCH_URL = f"{YT_API_BASE}/search"
COMMENT_THREADS_URL = f"{YT_API_BASE}/commentThreads"

QUOTA_COST_READ = 1
QUOTA_COST_SEARCH = 100
PT_ZONE = ZoneInfo("America/Los_Angeles")

HANDLE_ALIASES: Dict[str, str] = {
    "growwofficial": "Groww",
    "zerodhavarsity": "VarsitybyZerodha",
    "akshatshrivastavazayn": "AkshatZayn",
}

DEFAULT_CHANNELS: List[str] = [
    "Groww",
    "CARachanaRanade",
    "LabourLawAdvisor",
    "warikoo",
    "pranjalkamra",
    "ETMONEY",
    "VarsitybyZerodha",
    "AkshatZayn",
    "nehanagar",
    "shankarnath",
]

DEFAULT_SEARCH_QUERIES: List[str] = [
    "Groww SIP",
    "SIP stopped OR paused SIP",
    "first SIP mutual fund India",
    "Groww autopay mandate SIP",
    "1 year SIP review India",
    "why I stopped my SIP",
]

# Explicit promo/scam patterns (dropped unconditionally)
_PROMO_RE = re.compile(
    r"(?:"
    r"https?://|www\.|bit\.ly/|t\.me/|wa\.me/"
    r"|\bwhatsapp\b|\btelegram\b|\bdm\s+me\b|\bcontact\s+me\b|\bmessage\s+me\s+on\b"
    r"|\buse\s+my\s+(?:code|referral|link)\b|\breferral\s+code\b"
    r"|\bguaranteed\s+(?:return|returns|profit)\b|\brecover\s+(?:your\s+)?(?:loss|losses|money)\b"
    r"|\b100%\s+profit\b|\bdaily\s+profit\b|\bjoin\s+my\s+channel\b|\bsubscribe\s+to\s+my\s+channel\b"
    r")",
    re.IGNORECASE,
)

# Conditional terms ("crypto", "+91", "forex") are ONLY spam when paired with a contact/solicitation cue.
# Genuine investor comments like "stopped SIP and moved to crypto" are real signal and MUST be kept.
_CONDITIONAL_TERM_RE = re.compile(r"(?:\bcrypto\b|\bforex\b|\bbinary\s+option|\+91[\s-]?\d)", re.IGNORECASE)
_SOLICITATION_CUE_RE = re.compile(
    r"(?:"
    r"\bcall\b|\bcontact\b|\bdm\b|\bmessage\b|\bping\b|\bsignal\b|\bsignals\b"
    r"|\bmentor\b|\bcoach\b|\bearn\s+\$|\bweekly\s+profit\b|\bjoin\b|\+91[\s-]?\d{10}\b"
    r")",
    re.IGNORECASE,
)

# YouTube-only thread_context rule inside SIP/MF-anchored videos:
# First-person marker + behaviour term (whole-word, case-insensitive)
_YT_FIRST_PERSON_TERMS = (
    "i",
    "my",
    "mine",
    "me",
    "maine",
    "mera",
    "meri",
    "mujhe",
)
_YT_BEHAVIOUR_TERMS = (
    "stop",
    "stopped",
    "stopping",
    "pause",
    "paused",
    "pausing",
    "cancel",
    "cancelled",
    "cancelling",
    "continue",
    "continued",
    "continuing",
    "start",
    "started",
    "starting",
    "withdraw",
    "withdrew",
    "withdrawn",
    "redeem",
    "redeemed",
    "redeeming",
    "return",
    "returns",
    "loss",
    "losses",
    "profit",
    "profits",
    "salary",
    "emergency",
    "month",
    "months",
    "year",
    "years",
    "invest",
    "invested",
    "investing",
)
_YT_CONTINUER_BEHAVIOUR = {"continue", "continued", "continuing"}
_YT_DROPOFF_BEHAVIOUR = {
    "stop",
    "stopped",
    "stopping",
    "pause",
    "paused",
    "pausing",
    "cancel",
    "cancelled",
    "cancelling",
    "withdraw",
    "withdrew",
    "withdrawn",
    "redeem",
    "redeemed",
    "redeeming",
}

_YT_FIRST_PERSON_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _YT_FIRST_PERSON_TERMS) + r")\b",
    re.IGNORECASE,
)
_YT_BEHAVIOUR_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _YT_BEHAVIOUR_TERMS) + r")\b",
    re.IGNORECASE,
)

_TIMESTAMP_TOKEN_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_KEY_PARAM_RE = re.compile(r"([?&]key=)[^&\s]+", re.IGNORECASE)


def redact_api_key(text: str, api_key: Optional[str] = None) -> str:
    """Ensure YOUTUBE_API_KEY never appears in logs, checkpoints, or error strings."""
    out = _KEY_PARAM_RE.sub(r"\1REDACTED", text or "")
    if api_key and len(api_key) >= 4:
        out = out.replace(api_key, "REDACTED")
    return out


def is_youtube_spam_or_bot(author: str, text: str) -> bool:
    """Return True if a YouTube comment is promotional/scam spam.

    Does NOT drop on 'crypto' or '+91' alone unless combined with a promo/solicitation cue.
    """
    body = (text or "").strip()
    if not body:
        return False
    if _PROMO_RE.search(body):
        return True
    if _CONDITIONAL_TERM_RE.search(body) and _SOLICITATION_CUE_RE.search(body):
        return True
    author_low = (author or "").strip().lower()
    if any(tag in author_low for tag in ("telegram", "whatsapp", "crypto signal", "forex trade")):
        return True
    return False


def has_substantive_words(text: str, min_words: int = 3) -> bool:
    """Check that a comment has at least `min_words` words after stripping standalone MM:SS timestamps."""
    stripped = _TIMESTAMP_TOKEN_RE.sub(" ", text or "")
    return len(_WORD_RE.findall(stripped)) >= min_words


def match_youtube_comment(text: str, matcher: KeywordMatcher) -> MatchResult:
    """Match a comment inside a SIP/MF-anchored YouTube video (delegates to shared `match_anchored_reply`)."""
    return match_anchored_reply(text, matcher)


class YouTubeCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    priority: int = 2
    channels: List[str] = Field(default_factory=lambda: list(DEFAULT_CHANNELS))
    search_queries: List[str] = Field(default_factory=lambda: list(DEFAULT_SEARCH_QUERIES))
    max_upload_pages_per_channel: int = Field(10, ge=1, le=20)
    max_comment_pages_per_video: int = Field(3, ge=1, le=10)
    max_comments_per_video: int = Field(40, ge=1)
    max_comments_per_channel: int = Field(200, ge=1)
    max_matched_total: int = Field(1500, ge=1)
    daily_quota_cap_units: int = Field(8000, ge=1, le=10000)
    description_lead_chars: int = Field(250, ge=0)


class YouTubeQuotaExhausted(Exception):
    """Raised internally when the in-code quota guard or HTTP 403 quotaExceeded triggers."""


class YouTubeCommentsDisabled(Exception):
    """Raised when a video has comments disabled or is unavailable (403 commentsDisabled / 404)."""


class YouTubeCollector(BaseCollector):
    source = "youtube"

    def __init__(
        self,
        settings: Settings,
        *,
        raw_dir: Path,
        checkpoint_dir: Path,
        now: Optional[datetime] = None,
        sleep: Callable[[float], None] = lambda _: None,
        scan_limit: Optional[int] = None,
        matcher: Optional[KeywordMatcher] = None,
        http_get: Optional[Callable[..., Any]] = None,
        api_key: Optional[str] = None,
        channels_override: Optional[List[str]] = None,
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
        self.cfg = YouTubeCfg.model_validate(settings.sources["youtube"])
        if channels_override:
            self.cfg.channels = list(channels_override)

        self._api_key = api_key if api_key is not None else (
            "injected-test-key" if http_get is not None else get_api_key("YOUTUBE_API_KEY")
        )
        if not self._api_key:
            raise CollectorError(
                "YOUTUBE_API_KEY missing in .env. Add YOUTUBE_API_KEY to .env before running YouTube collection."
            )

        self._session = requests.Session()
        self._http_get = http_get
        self.quota_units_this_run = 0
        self._rr_cursor = 0

        self.state.setdefault("channel_meta", {})          # handle -> {channel_id, title, subscribers, uploads_playlist, scan_done, next_page_token, pages_scanned, in_window_videos, sip_mf_videos}
        self.state.setdefault("search_done", [])           # completed search queries
        self.state.setdefault("pending_videos", {})        # video_id -> {video_id, video_title, channel_id, channel_title, channel_handle, published_at, discovered_via}
        self.state.setdefault("done_videos", [])           # completed video_ids
        self.state.setdefault("per_channel", {})           # channel_handle_or_title -> stats dict
        self.state.setdefault("quota_by_pt_date", {})      # YYYY-MM-DD (PT) -> units used
        self.state["quota_exhausted"] = False

    # ------------------------------------------------------------------ quota & HTTP helpers

    def _pt_date_str(self) -> str:
        return self.now.astimezone(PT_ZONE).strftime("%Y-%m-%d")

    def _quota_used_today(self) -> int:
        return int(self.state["quota_by_pt_date"].get(self._pt_date_str(), 0))

    def _charge_quota(self, units: int) -> None:
        pt_day = self._pt_date_str()
        self.state["quota_by_pt_date"][pt_day] = self._quota_used_today() + units
        self.quota_units_this_run += units
        self.count("quota_units_total", units)

    def _check_quota_before_call(self, cost_units: int) -> None:
        used = self._quota_used_today()
        if used + cost_units > self.cfg.daily_quota_cap_units:
            self.state["quota_exhausted"] = True
            raise YouTubeQuotaExhausted(
                f"Daily quota safety cap reached ({used}/{self.cfg.daily_quota_cap_units} units on {self._pt_date_str()} PT); "
                "checkpoint saved — resume tomorrow."
            )

    @staticmethod
    def _extract_error_reason(resp: Any) -> Tuple[str, str]:
        try:
            data = resp.json()
            err = data.get("error") or {}
            msg = err.get("message") or ""
            errors = err.get("errors") or []
            reason = errors[0].get("reason", "") if errors and isinstance(errors[0], dict) else ""
            return str(reason), str(msg)
        except Exception:
            return "", getattr(resp, "text", "")[:200]

    def _api_get(self, url: str, params: Dict[str, Any], cost_units: int) -> Dict[str, Any]:
        """Execute a GET against YouTube Data API v3 with quota preflight, retry on 429/5xx, and key redaction."""
        self._check_quota_before_call(cost_units)
        get = self._http_get or self._session.get
        full_params = dict(params)
        full_params["key"] = self._api_key
        max_attempts = max(self.collect_cfg.max_retries, 3)
        attempt = 0

        while True:
            try:
                resp = get(url, params=full_params, timeout=self.collect_cfg.request_timeout_sec)
            except (requests.Timeout, requests.ConnectionError) as e:
                safe_err = redact_api_key(str(e), self._api_key)
                if attempt >= max_attempts:
                    raise CollectorError(f"youtube network error after {attempt + 1} attempts: {safe_err}") from e
                delay = self.collect_cfg.backoff_base_sec * (2 ** attempt)
                log.warning("youtube timeout/conn error (%s); retry %d/%d in %.0fs", safe_err, attempt + 1, max_attempts, delay)
                self.sleep(delay)
                attempt += 1
                continue

            self._charge_quota(cost_units)
            self.count("requests_made")
            code = resp.status_code

            if code == 200:
                try:
                    return resp.json()
                except ValueError as e:
                    raise CollectorError(f"youtube invalid JSON from {url}") from e

            reason, msg = self._extract_error_reason(resp)
            safe_msg = redact_api_key(msg, self._api_key)

            if code == 403 and reason in {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}:
                if reason == "rateLimitExceeded" and attempt < max_attempts:
                    delay = self.collect_cfg.backoff_base_sec * (2 ** attempt)
                    self.sleep(delay)
                    attempt += 1
                    continue
                self.state["quota_exhausted"] = True
                raise YouTubeQuotaExhausted(
                    f"YouTube API HTTP 403 ({reason}): {safe_msg} — checkpoint saved, resume tomorrow."
                )

            if code == 403 and reason in {"commentsDisabled", "forbidden", "ConditionallyRequiredParameter"}:
                raise YouTubeCommentsDisabled(f"{reason}: {safe_msg}")

            if code == 404:
                raise YouTubeCommentsDisabled(f"notFound: {safe_msg}")

            if code in {429, 500, 502, 503, 504}:
                if attempt >= max_attempts:
                    raise CollectorError(f"youtube HTTP {code} ({reason}) after {attempt + 1} attempts: {safe_msg}")
                delay = self.collect_cfg.backoff_base_sec * (2 ** attempt)
                log.warning("youtube HTTP %d (%s); retry %d/%d in %.0fs", code, reason, attempt + 1, max_attempts, delay)
                self.sleep(delay)
                attempt += 1
                continue

            raise CollectorError(f"youtube HTTP {code} ({reason}): {safe_msg}")

    # ------------------------------------------------------------------ channel & video discovery

    @staticmethod
    def _canonical_handle(raw_handle: str) -> str:
        clean = raw_handle.strip().lstrip("@")
        return HANDLE_ALIASES.get(clean.lower(), clean)

    def _channel_stats(self, handle: str) -> Dict[str, int]:
        return self.state["per_channel"].setdefault(handle, {
            "videos_scanned": 0,
            "videos_queued": 0,
            "videos_harvested": 0,
            "comments_scanned": 0,
            "comments_matched": 0,
            "comments_capped": 0,
        })

    def _channel_cap_reached(self, handle: str) -> bool:
        return self._channel_stats(handle).get("comments_matched", 0) >= self.cfg.max_comments_per_channel

    def _video_matches_anchor(self, title: str, description: str) -> bool:
        """Strict video filter: title or lead description (before boilerplate footer) must have a SIP/MF anchor."""
        if self.matcher.has_anchor(title or ""):
            return True
        lead_len = self.cfg.description_lead_chars
        lead = (description or "")[:lead_len] if lead_len > 0 else ""
        return bool(lead and self.matcher.has_anchor(lead))

    def _queue_video(
        self,
        *,
        video_id: str,
        video_title: str,
        channel_id: str,
        channel_title: str,
        channel_handle: str,
        published_at: str,
        discovered_via: str,
    ) -> bool:
        if not video_id:
            return False
        if video_id in self.state["done_videos"] or video_id in self.state["pending_videos"]:
            return False
        ch_key = channel_handle or (channel_title or "unknown")
        if self._channel_cap_reached(ch_key):
            return False
        self.state["pending_videos"][video_id] = {
            "video_id": video_id,
            "video_title": (video_title or "").strip(),
            "channel_id": channel_id or "",
            "channel_title": (channel_title or "").strip(),
            "channel_handle": ch_key,
            "published_at": published_at,
            "discovered_via": discovered_via,
        }
        self.count("videos_queued")
        self._channel_stats(ch_key)["videos_queued"] += 1
        return True

    def _resolve_channel(self, raw_handle: str) -> Optional[Dict[str, Any]]:
        clean = raw_handle.strip().lstrip("@")
        canonical = self._canonical_handle(raw_handle)
        meta_map = self.state["channel_meta"]
        if canonical in meta_map and meta_map[canonical].get("uploads_playlist"):
            return meta_map[canonical]

        for candidate in ([f"@{canonical}"] + ([f"@{clean}"] if clean != canonical else [])):
            data = self._api_get(
                CHANNELS_URL,
                {"part": "snippet,statistics,contentDetails", "forHandle": candidate},
                cost_units=QUOTA_COST_READ,
            )
            items = data.get("items") or []
            if items:
                ch = items[0]
                uploads = (ch.get("contentDetails") or {}).get("relatedPlaylists", {}).get("uploads")
                info = {
                    "handle": canonical,
                    "requested_handle": raw_handle,
                    "channel_id": ch["id"],
                    "title": (ch.get("snippet") or {}).get("title", canonical),
                    "subscribers": int((ch.get("statistics") or {}).get("subscriberCount", 0) or 0),
                    "uploads_playlist": uploads,
                    "scan_done": False,
                    "next_page_token": None,
                    "pages_scanned": 0,
                    "in_window_videos": 0,
                    "sip_mf_videos": 0,
                }
                meta_map[canonical] = info
                return info

        log.warning("youtube channel handle %r did not resolve; skipping", raw_handle)
        return None

    def _videos_limit_reached(self) -> bool:
        if self.scan_limit is None:
            return False
        return self.state["counters"].get("videos_harvested", 0) >= self.scan_limit

    def _matched_cap_reached(self) -> bool:
        return len(self.existing_ids) >= self.cfg.max_matched_total

    def _scan_channel_uploads(self, raw_handle: str) -> None:
        canonical = self._canonical_handle(raw_handle)
        if self._channel_cap_reached(canonical):
            return
        info = self._resolve_channel(raw_handle)
        if not info or not info.get("uploads_playlist") or info.get("scan_done"):
            return

        handle = info["handle"]
        ch_st = self._channel_stats(handle)
        old_streak = 0
        # In capped test runs (--limit N), scan 1 page per channel so every channel queues videos cheaply
        max_pages = 1 if self.scan_limit else self.cfg.max_upload_pages_per_channel

        while not info.get("scan_done") and info["pages_scanned"] < max_pages:
            if self._matched_cap_reached() or self._channel_cap_reached(handle):
                return
            params: Dict[str, Any] = {
                "part": "snippet,contentDetails",
                "playlistId": info["uploads_playlist"],
                "maxResults": 50,
            }
            if info.get("next_page_token"):
                params["pageToken"] = info["next_page_token"]

            pl_data = self._api_get(PLAYLIST_ITEMS_URL, params, cost_units=QUOTA_COST_READ)
            info["pages_scanned"] += 1
            items = pl_data.get("items") or []
            if not items:
                info["scan_done"] = True
                break

            for it in items:
                snip = it.get("snippet") or {}
                cd = it.get("contentDetails") or {}
                vid = cd.get("videoId") or (snip.get("resourceId") or {}).get("videoId")
                pub_str = cd.get("videoPublishedAt") or snip.get("publishedAt") or ""
                dt = to_utc(pub_str)
                ch_st["videos_scanned"] += 1
                self.count("videos_scanned")

                if not self.in_window(dt):
                    old_streak += 1
                    continue
                old_streak = 0
                info["in_window_videos"] += 1

                v_title = snip.get("title") or ""
                v_desc = snip.get("description") or ""
                if self._video_matches_anchor(v_title, v_desc):
                    info["sip_mf_videos"] += 1
                    self._queue_video(
                        video_id=vid,
                        video_title=v_title,
                        channel_id=info["channel_id"],
                        channel_title=info["title"],
                        channel_handle=handle,
                        published_at=pub_str,
                        discovered_via=f"playlist:{handle}",
                    )

            next_tok = pl_data.get("nextPageToken")
            info["next_page_token"] = next_tok
            if not next_tok or old_streak >= self.collect_cfg.old_streak_stop or info["pages_scanned"] >= max_pages:
                info["scan_done"] = True
                break
            self.pause()

    def _run_targeted_searches(self) -> None:
        # Skip 100-unit search calls during capped `--limit N` test runs to keep test quota minimal
        if self.scan_limit is not None:
            return
        cutoff_iso = self.cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        for query in self.cfg.search_queries:
            if query in self.state["search_done"]:
                continue
            if self._matched_cap_reached():
                return
            data = self._api_get(
                SEARCH_URL,
                {
                    "part": "snippet",
                    "q": query,
                    "type": "video",
                    "order": "relevance",
                    "publishedAfter": cutoff_iso,
                    "regionCode": "IN",
                    "relevanceLanguage": "en",
                    "maxResults": 50,
                },
                cost_units=QUOTA_COST_SEARCH,
            )
            for it in data.get("items") or []:
                vid = (it.get("id") or {}).get("videoId")
                snip = it.get("snippet") or {}
                pub_str = snip.get("publishedAt") or ""
                dt = to_utc(pub_str)
                self.count("videos_scanned")
                if not self.in_window(dt):
                    continue
                v_title = snip.get("title") or ""
                v_desc = snip.get("description") or ""
                if self._video_matches_anchor(v_title, v_desc):
                    ch_title = snip.get("channelTitle") or "search"
                    self._queue_video(
                        video_id=vid,
                        video_title=v_title,
                        channel_id=snip.get("channelId") or "",
                        channel_title=ch_title,
                        channel_handle=ch_title,
                        published_at=pub_str,
                        discovered_via=f"search:{query}",
                    )
            self.state["search_done"].append(query)
            self.pause()

    # ------------------------------------------------------------------ round-robin selection & comment harvesting

    def _ordered_channel_Pool(self) -> List[str]:
        """Return ordered list of channel handles with configured channels first, then any search channels."""
        ordered: List[str] = []
        for raw_h in self.cfg.channels:
            c = self._canonical_handle(raw_h)
            if c not in ordered:
                ordered.append(c)
        for vinfo in self.state["pending_videos"].values():
            ch = vinfo.get("channel_handle") or vinfo.get("channel_title") or "unknown"
            if ch not in ordered:
                ordered.append(ch)
        return ordered

    def _prune_capped_channels(self) -> None:
        """Remove pending videos belonging to channels that have already reached max_comments_per_channel."""
        to_remove = [
            vid
            for vid, vinfo in self.state["pending_videos"].items()
            if self._channel_cap_reached(vinfo.get("channel_handle") or vinfo.get("channel_title") or "unknown")
        ]
        for vid in to_remove:
            self.state["pending_videos"].pop(vid, None)
            if vid not in self.state["done_videos"]:
                self.state["done_videos"].append(vid)

    def _pick_next_video_round_robin(self) -> Optional[Dict[str, Any]]:
        """Pick the next pending video by rotating round-robin across channels."""
        self._prune_capped_channels()
        pending = self.state["pending_videos"]
        if not pending:
            return None

        pool = self._ordered_channel_Pool()
        if not pool:
            vid = next(iter(pending))
            return pending[vid]

        n = len(pool)
        for offset in range(n):
            idx = (self._rr_cursor + offset) % n
            target_ch = pool[idx]
            for vid, vinfo in pending.items():
                ch = vinfo.get("channel_handle") or vinfo.get("channel_title") or "unknown"
                if ch == target_ch:
                    self._rr_cursor = (idx + 1) % n
                    return vinfo

        # Fallback if a video has a channel handle not in pool
        vid = next(iter(pending))
        return pending[vid]

    def _harvest_one_video(self, vinfo: Dict[str, Any]) -> List[RawPost]:
        video_id = vinfo["video_id"]
        video_title = vinfo.get("video_title") or ""
        channel_id = vinfo.get("channel_id") or ""
        channel_title = vinfo.get("channel_title") or ""
        channel_handle = vinfo.get("channel_handle") or channel_title or "unknown"
        ch_st = self._channel_stats(channel_handle)

        raw_comments: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        pages = 0

        while pages < self.cfg.max_comment_pages_per_video:
            params: Dict[str, Any] = {
                "part": "snippet,replies",
                "videoId": video_id,
                "maxResults": 100,
                "order": "relevance",
                "textFormat": "plainText",
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                data = self._api_get(COMMENT_THREADS_URL, params, cost_units=QUOTA_COST_READ)
            except YouTubeCommentsDisabled as e:
                log.info("youtube video %s comments disabled/unavailable (%s); skipping", video_id, e)
                self.drop("comments_disabled")
                self._mark_video_done(video_id)
                return []

            pages += 1
            for item in data.get("items") or []:
                tsnip = item.get("snippet") or {}
                top = tsnip.get("topLevelComment") or {}
                top_snip = top.get("snippet") or {}
                top_id = top.get("id") or item.get("id")
                if top_id:
                    raw_comments.append({
                        "comment_id": str(top_id),
                        "record_type": "comment",
                        "parent_id": None,
                        "author": top_snip.get("authorDisplayName") or "",
                        "author_channel_id": (top_snip.get("authorChannelId") or {}).get("value") or "",
                        "text": top_snip.get("textOriginal") or top_snip.get("textDisplay") or "",
                        "like_count": int(top_snip.get("likeCount", 0) or 0),
                        "reply_count": int(tsnip.get("totalReplyCount", 0) or 0),
                        "published_at": top_snip.get("publishedAt") or "",
                    })
                for rep in (item.get("replies") or {}).get("comments") or []:
                    rep_snip = rep.get("snippet") or {}
                    rep_id = rep.get("id")
                    if rep_id:
                        raw_comments.append({
                            "comment_id": str(rep_id),
                            "record_type": "reply",
                            "parent_id": str(rep_snip.get("parentId") or top_id or ""),
                            "author": rep_snip.get("authorDisplayName") or "",
                            "author_channel_id": (rep_snip.get("authorChannelId") or {}).get("value") or "",
                            "text": rep_snip.get("textOriginal") or rep_snip.get("textDisplay") or "",
                            "like_count": int(rep_snip.get("likeCount", 0) or 0),
                            "reply_count": 0,
                            "published_at": rep_snip.get("publishedAt") or "",
                        })

            page_token = data.get("nextPageToken")
            if not page_token:
                break
            self.pause()

        ch_st["videos_harvested"] += 1
        self.count("videos_harvested")

        candidates: List[Tuple[int, datetime, Dict[str, Any], Any]] = []
        for c in raw_comments:
            self.count("scanned")
            self.count("comments_scanned")
            ch_st["comments_scanned"] += 1

            dt = to_utc(c["published_at"])
            if not self.in_window(dt):
                self.drop("out_of_window")
                continue

            # Drop creator self-comments, pinned promos, and channel-owner replies
            if channel_id and c["author_channel_id"] and c["author_channel_id"] == channel_id:
                self.drop("creator_or_channel_owner")
                continue

            body = (c["text"] or "").strip()
            if not body:
                self.drop("empty_text")
                continue

            if is_youtube_spam_or_bot(c["author"], body):
                self.drop("bot_or_spam")
                continue

            if not has_substantive_words(body, min_words=3):
                self.drop("too_short_or_timestamp")
                continue

            cid = c["comment_id"]
            if f"{self.source}:{cid}" in self.existing_ids:
                self.drop("duplicate")
                continue

            # Every queued video is verified to contain a SIP/MF anchor
            m = match_youtube_comment(body, self.matcher)
            if not m.matched:
                self.drop("no_keyword_match")
                continue

            candidates.append((c["like_count"], dt, c, m))

        # Sort by (like_count DESC, publishedAt ASC)
        candidates.sort(key=lambda item: (-item[0], item[1]))

        # 1. Per-video cap (default 40)
        cap_video = self.cfg.max_comments_per_video
        if len(candidates) > cap_video:
            capped_count = len(candidates) - cap_video
            candidates = candidates[:cap_video]
            for _ in range(capped_count):
                self.drop("video_comment_cap")
            self.count("comments_capped", capped_count)
            ch_st["comments_capped"] = ch_st.get("comments_capped", 0) + capped_count

        # 2. Per-channel cap (default 200)
        remaining_channel = max(self.cfg.max_comments_per_channel - ch_st.get("comments_matched", 0), 0)
        if len(candidates) > remaining_channel:
            ch_capped = len(candidates) - remaining_channel
            candidates = candidates[:remaining_channel]
            for _ in range(ch_capped):
                self.drop("channel_comment_cap")
            self.count("comments_capped_channel", ch_capped)
            ch_st["comments_capped"] = ch_st.get("comments_capped", 0) + ch_capped

        # 3. Total source cap (default 1500)
        remaining_total = max(self.cfg.max_matched_total - len(self.existing_ids), 0)
        if len(candidates) > remaining_total:
            candidates = candidates[:remaining_total]

        out: List[RawPost] = []
        for like_count, dt, c, m in candidates:
            cid = c["comment_id"]
            author_key = c["author"] or c["author_channel_id"] or "anonymous"
            post = self.make_post(
                native_id=cid,
                url=f"https://www.youtube.com/watch?v={video_id}&lc={cid}",
                dt=dt,
                username=author_key,
                text=c["text"].strip(),
                match=m,
                raw_meta={
                    "record_type": c["record_type"],
                    "video_id": video_id,
                    "video_title": video_title,
                    "channel_id": channel_id,
                    "channel_title": channel_title,
                    "channel_handle": channel_handle,
                    "parent_id": c["parent_id"],
                    "like_count": like_count,
                    "reply_count": c["reply_count"],
                    "matched_via": m.matched_via,
                },
            )
            out.append(post)
            ch_st["comments_matched"] += 1
            self.count("matched")
            self.count("comments_matched")
            self.count(f"matched_via_{m.matched_via}")

        self._mark_video_done(video_id)
        log.info(
            "youtube video %s (%s): %d raw comments, %d matched (channel total: %d/%d, pending videos: %d, quota today: %d)",
            video_id,
            channel_handle,
            len(raw_comments),
            len(out),
            ch_st["comments_matched"],
            self.cfg.max_comments_per_channel,
            len(self.state["pending_videos"]),
            self._quota_used_today(),
        )
        return out

    def _mark_video_done(self, video_id: str) -> None:
        self.state["pending_videos"].pop(video_id, None)
        if video_id not in self.state["done_videos"]:
            self.state["done_videos"].append(video_id)

    # ------------------------------------------------------------------ main generator

    def iter_pages(self) -> Iterator[List[RawPost]]:
        try:
            # Phase 1: Scan channel uploads playlists across ALL configured channels first
            # so pending_videos has candidates from every channel for round-robin harvesting
            for handle in self.cfg.channels:
                if self._matched_cap_reached():
                    return
                self._scan_channel_uploads(handle)
                self.save_state()

            # Phase 2: Targeted search queries (skipped during --limit test runs)
            if not self._videos_limit_reached() and not self._matched_cap_reached():
                self._run_targeted_searches()
                self.save_state()

            # Phase 3: Harvest queued videos round-robin across channels
            while True:
                if self._videos_limit_reached() or self._matched_cap_reached():
                    return
                vinfo = self._pick_next_video_round_robin()
                if vinfo is None:
                    break
                yield self._harvest_one_video(vinfo)
                self.pause()

        except YouTubeQuotaExhausted as e:
            log.warning("%s", e)
            self.state["quota_exhausted_message"] = str(e)
            self.save_state()
            return

    def extra_stats(self) -> Dict[str, Any]:
        c = self.state["counters"]
        matched_by_channel = {
            ch: st.get("comments_matched", 0)
            for ch, st in self.state["per_channel"].items()
        }
        return {
            "requests_made": c.get("requests_made", 0),
            "quota_units_this_run": self.quota_units_this_run,
            "quota_units_today_pt": self._quota_used_today(),
            "quota_units_total": c.get("quota_units_total", 0),
            "quota_exhausted": bool(self.state.get("quota_exhausted", False)),
            "videos_scanned": c.get("videos_scanned", 0),
            "videos_queued": c.get("videos_queued", 0),
            "videos_harvested": c.get("videos_harvested", 0),
            "videos_pending": len(self.state["pending_videos"]),
            "videos_done": len(self.state["done_videos"]),
            "comments_scanned": c.get("comments_scanned", 0),
            "comments_matched": c.get("comments_matched", 0),
            "comments_capped": c.get("comments_capped", 0),
            "comments_capped_channel": c.get("comments_capped_channel", 0),
            "matched_via": {
                "self": c.get("matched_via_self", 0),
                "thread_context": c.get("matched_via_thread_context", 0),
            },
            "matched_by_channel": matched_by_channel,
            "per_channel": self.state["per_channel"],
        }
