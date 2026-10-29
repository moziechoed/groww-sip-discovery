"""Logging + per-stage summaries (NFR5): in, out, dropped by reason, failed, LLM calls, time taken."""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            datefmt="%H:%M:%S")
        _CONFIGURED = True
    return logging.getLogger(name)


class StageSummary:
    def __init__(self, stage: str):
        self.stage = stage
        self.records_in = 0
        self.records_out = 0
        self.dropped: Counter = Counter()
        self.failed = 0
        self.llm_calls = 0
        self.cost_inr = 0.0
        self._t0 = time.monotonic()

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "records_in": self.records_in,
            "records_out": self.records_out,
            "dropped": dict(self.dropped),
            "failed": self.failed,
            "llm_calls": self.llm_calls,
            "cost_inr": round(self.cost_inr, 4),
            "time_taken_sec": round(time.monotonic() - self._t0, 1),
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def finish(self, corpus_stats_path: Optional[Path] = None) -> dict:
        d = self.as_dict()
        print(f"\n--- Stage summary: {self.stage} ---")
        for k, v in d.items():
            if k != "stage":
                print(f"  {k:<15}: {v}")
        if corpus_stats_path:
            p = Path(corpus_stats_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            stats = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            stats.setdefault("stages", {})[self.stage] = d
            p.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        return d
