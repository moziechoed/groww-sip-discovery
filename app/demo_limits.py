"""Demo rate limits (Architecture §4.10, PRD UI4 / AC17).

Enforces:
- 3 calls per session (tracked in session_state dict)
- 50 calls per day globally across all sessions (tracked in a local JSON file with a lock;
  resets automatically when the UTC/IST date changes or if the ephemeral filesystem restarts)
"""
from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Callable, MutableMapping, Optional, Tuple

SESSION_LIMIT_MSG = "Session demo limit (3 analyses) reached. Refresh to start a new session."
DAILY_LIMIT_MSG = "Daily demo limit reached. Please try again tomorrow."


class DailyDemoCounter:
    """Thread-safe file-backed daily counter (intended to be held via st.cache_resource)."""

    def __init__(self, store_path: Path, daily_cap: int = 50, today_fn: Callable[[], str] = lambda: date.today().isoformat()):
        self.store_path = Path(store_path)
        self.daily_cap = daily_cap
        self.today_fn = today_fn
        self._lock = threading.Lock()

    def _read_unlocked(self) -> Tuple[str, int]:
        today = self.today_fn()
        if not self.store_path.exists():
            return today, 0
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            if data.get("date") == today:
                return today, int(data.get("count", 0))
        except Exception:
            pass
        return today, 0

    def _write_unlocked(self, day: str, count: int) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(json.dumps({"date": day, "count": count}), encoding="utf-8")

    def get_count(self) -> int:
        with self._lock:
            _, count = self._read_unlocked()
            return count

    def set_count(self, count: int, day: Optional[str] = None) -> None:
        with self._lock:
            self._write_unlocked(day or self.today_fn(), count)

    def check_and_increment(self, session_state: MutableMapping[str, int], session_cap: int = 3) -> Tuple[bool, Optional[str]]:
        """Atomically verify session and daily caps; increment both if allowed."""
        used_session = int(session_state.get("demo_calls_used", 0))
        if used_session >= session_cap:
            return False, SESSION_LIMIT_MSG

        with self._lock:
            today, used_daily = self._read_unlocked()
            if used_daily >= self.daily_cap:
                return False, DAILY_LIMIT_MSG
            self._write_unlocked(today, used_daily + 1)
            session_state["demo_calls_used"] = used_session + 1
            return True, None
