"""BaseCollector: shared 24-month cutoff, matching, author hashing, retries, JSONL writing and resume state.

Each collector writes data/raw/{source}.jsonl (append-only) and keeps its resume state in
data/checkpoints/collect_{source}.json. Already-written IDs are read back from the JSONL itself,
so a re-run never duplicates rows even if the state file is behind by one page.
"""
from __future__ import annotations

import calendar
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set

from core.config import Settings
from core.logger import get_logger
from core.schemas import RawPost
from core.utils import hash_author, require_author_salt

from collectors.matcher import KeywordMatcher, MatchResult

log = get_logger("collect")


class CollectorError(Exception):
    """A source failed after retries. The runner logs it and moves on to the next source (FR1.5)."""


class TransientError(Exception):
    """Retryable: timeout, connection error, HTTP 429/5xx, or a suspicious empty page."""


# ----------------------------------------------------------------------------- helpers


def months_before(now: datetime, months: int) -> datetime:
    y, m = divmod(now.month - 1 - months, 12)
    year, month = now.year + y, m + 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day)


def to_utc(value: Any) -> Optional[datetime]:
    """Accepts datetime or ISO string. Naive datetimes are local time (google-play-scraper uses fromtimestamp)."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        return None
    return value.astimezone(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("Could not parse %s; starting from defaults", path)
        return default


def load_jsonl_ids(path: Path) -> Set[str]:
    """IDs already written. Truncates a partial last line left by a crash so later appends stay valid."""
    ids: Set[str] = set()
    if not path.exists():
        return ids
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        cut = data.rfind(b"\n") + 1
        log.warning("%s ended with a partial line (crash mid-write); truncating it", path.name)
        with open(path, "r+b") as f:
            f.truncate(cut)
        data = data[:cut]
    for line in data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ids.add(json.loads(line)["id"])
        except (json.JSONDecodeError, KeyError, TypeError):
            log.warning("Skipping malformed line in %s", path.name)
    return ids


# ----------------------------------------------------------------------------- base class


class BaseCollector:
    source: str = ""

    def __init__(self, settings: Settings, *, raw_dir: Path, checkpoint_dir: Path,
                 now: Optional[datetime] = None, sleep: Callable[[float], None] = time.sleep,
                 scan_limit: Optional[int] = None, matcher: Optional[KeywordMatcher] = None):
        require_author_salt()
        self.settings = settings
        self.collect_cfg = settings.collect
        self.now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.cutoff = months_before(self.now, settings.run.window_months)
        self.sleep = sleep
        self.scan_limit = scan_limit
        self.matcher = matcher or KeywordMatcher.from_settings(settings.keywords)
        self.out_path = Path(raw_dir) / f"{self.source}.jsonl"
        self.state_path = Path(checkpoint_dir) / f"collect_{self.source}.json"
        self.existing_ids: Set[str] = load_jsonl_ids(self.out_path)
        self.state: Dict[str, Any] = read_json(self.state_path, {})
        self.state.setdefault("counters", {})
        self.state.setdefault("dropped", {})
        self.state.setdefault("keyword_group", {})
        self.written_this_run = 0

    # -- subclass API -------------------------------------------------------------------------

    def iter_pages(self) -> Iterator[List[RawPost]]:  # pragma: no cover - abstract
        """Yield one list of new, matched RawPosts per fetched page. Update self.state before yielding."""
        raise NotImplementedError

    def is_retryable(self, exc: Exception) -> bool:
        return isinstance(exc, TransientError)

    def extra_stats(self) -> Dict[str, Any]:
        return {}

    # -- shared helpers -----------------------------------------------------------------------

    def count(self, key: str, n: int = 1) -> None:
        self.state["counters"][key] = self.state["counters"].get(key, 0) + n

    def drop(self, reason: str) -> None:
        self.state["dropped"][reason] = self.state["dropped"].get(reason, 0) + 1

    def in_window(self, dt: Optional[datetime]) -> bool:
        return dt is not None and dt >= self.cutoff

    def match(self, text: str) -> MatchResult:
        return self.matcher.match(text)

    def make_post(self, *, native_id: str, url: str, dt: datetime, username: str, text: str,
                  match: MatchResult, raw_meta: Dict[str, Any]) -> RawPost:
        meta = dict(raw_meta)
        meta["matched_keywords"] = list(match.matched_keywords)
        post = RawPost(
            id=f"{self.source}:{native_id}",
            source=self.source,
            url=url,
            date=iso_z(dt),
            author_hash=hash_author(username or ""),   # plain username is never stored (FR1.4)
            text=text,
            collection_method="scraped",
            keyword_group=match.keyword_group,
            raw_meta=meta,
        )
        self.existing_ids.add(post.id)
        kg = self.state["keyword_group"]
        kg[match.keyword_group] = kg.get(match.keyword_group, 0) + 1
        d = post.date
        if not self.state.get("oldest_date") or d < self.state["oldest_date"]:
            self.state["oldest_date"] = d
        if not self.state.get("newest_date") or d > self.state["newest_date"]:
            self.state["newest_date"] = d
        return post

    def call_with_retries(self, fn: Callable[[], Any], what: str) -> Any:
        attempt = 0
        while True:
            try:
                return fn()
            except Exception as e:  # noqa: BLE001 - classified below
                if not self.is_retryable(e) or attempt >= self.collect_cfg.max_retries:
                    raise CollectorError(f"{what} failed after {attempt + 1} attempt(s): {e}") from e
                delay = self.collect_cfg.backoff_base_sec * (2 ** attempt)
                log.warning("%s: %s; retry %d/%d in %.0fs", what, e, attempt + 1,
                            self.collect_cfg.max_retries, delay)
                self.sleep(delay)
                attempt += 1

    def pause(self) -> None:
        if self.collect_cfg.page_delay_sec:
            self.sleep(self.collect_cfg.page_delay_sec)

    # -- persistence --------------------------------------------------------------------------

    def _append(self, posts: List[RawPost]) -> None:
        if not posts:
            return
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.out_path, "a", encoding="utf-8") as f:
            for p in posts:
                f.write(json.dumps(p.model_dump(), ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.written_this_run += len(posts)

    def save_state(self) -> None:
        self.state["updated_at"] = iso_z(datetime.now(timezone.utc))
        atomic_write_text(self.state_path, json.dumps(self.state, indent=2, sort_keys=True))

    # -- driver -------------------------------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        """Collect, appending + checkpointing after every page. Never raises: failures are reported in stats."""
        status, error = "ok", None
        try:
            for posts in self.iter_pages():
                self._append(posts)
                self.save_state()
        except KeyboardInterrupt:
            status, error = "interrupted", "Stopped by user (Ctrl+C)"
            log.warning("%s interrupted by user; checkpoint saved — re-run the same command to resume", self.source)
            self.save_state()
        except Exception as e:  # noqa: BLE001 - FR1.5: one failing source must not stop the others
            status, error = "failed", str(e)
            log.error("%s failed: %s (progress saved; re-run the same command to resume)", self.source, e)
            self.save_state()
        return self.stats(status, error)

    def stats(self, status: str, error: Optional[str] = None) -> Dict[str, Any]:
        out = {
            "status": status,
            "window_start": iso_z(self.cutoff),
            "window_end": iso_z(self.now),
            "scanned": self.state["counters"].get("scanned", 0),
            "matched": self.state["counters"].get("matched", 0),
            "written_this_run": self.written_this_run,
            "total_in_file": len(self.existing_ids),
            "dropped": dict(Counter(self.state["dropped"])),
            "keyword_group": dict(self.state["keyword_group"]),
            "oldest_date": self.state.get("oldest_date"),
            "newest_date": self.state.get("newest_date"),
            "scan_limit": self.scan_limit,
            "output": str(self.out_path),
        }
        if error:
            out["error"] = error
        out.update(self.extra_stats())
        return out
