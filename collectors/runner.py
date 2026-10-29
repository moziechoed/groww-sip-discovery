"""Stage 1 runner: runs the requested collectors in isolation, collates raw_posts.jsonl, writes corpus_stats.json.

    python run.py --stage collect [--source play_store|app_store|all] [--limit N]

--limit N is a capped test run: at most N reviews scanned per store, written to data/raw/test_run/
(checkpoints in data/checkpoints/test_run/, stats in data/raw/test_run/corpus_stats_test.json).
It starts fresh every time and never touches the real collection files.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from core.config import ROOT, Settings
from core.logger import StageSummary, get_logger
from core.utils import require_author_salt

from collectors.app_store import AppStoreCollector
from collectors.base import atomic_write_text, iso_z, read_json
from collectors.play_store import PlayStoreCollector
from collectors.reddit import RedditCollector
from collectors.tradingqna import TradingQnACollector
from collectors.youtube import YouTubeCollector

log = get_logger("collect")

COLLECTORS = {
    "play_store": PlayStoreCollector,
    "app_store": AppStoreCollector,
    "reddit": RedditCollector,
    "youtube": YouTubeCollector,
    "tradingqna": TradingQnACollector,
}
COLLATED_NAME = "raw_posts.jsonl"
TEST_RUN_DIR = "test_run"


def collate(raw_dir: Path) -> int:
    """Merge every data/raw/{source}.jsonl into data/raw/raw_posts.jsonl, deduped by id. Returns row count."""
    seen = set()
    lines: List[str] = []
    for path in sorted(Path(raw_dir).glob("*.jsonl")):
        if path.name == COLLATED_NAME:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rid = json.loads(line)["id"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if rid in seen:
                continue
            seen.add(rid)
            lines.append(line)
    atomic_write_text(Path(raw_dir) / COLLATED_NAME, "".join(l + "\n" for l in lines))
    return len(lines)


def _load_salt_env() -> str:
    """Validate that AUTHOR_HASH_SALT is set and not a placeholder; refuse to start if invalid (FR1.4)."""
    return require_author_salt()


def run_collect(settings: Settings, sources: Optional[List[str]] = None, limit: Optional[int] = None, *,
                now: Optional[datetime] = None, sleep: Callable[[float], None] = time.sleep,
                collector_kwargs: Optional[Dict[str, dict]] = None) -> int:
    _load_salt_env()
    raw_dir = settings.paths.resolve("raw_dir")
    ckpt_dir = settings.paths.resolve("checkpoints")
    stats_path = settings.paths.resolve("corpus_stats")
    if limit:
        raw_dir, ckpt_dir = raw_dir / TEST_RUN_DIR, ckpt_dir / TEST_RUN_DIR
        stats_path = raw_dir / "corpus_stats_test.json"
        for d in (raw_dir, ckpt_dir):
            shutil.rmtree(d, ignore_errors=True)
        print(f"\n== Capped test run: max {limit} reviews scanned per store -> {raw_dir} ==")
    raw_dir.mkdir(parents=True, exist_ok=True)

    names = sources or list(COLLECTORS)
    summary = StageSummary("collect" if not limit else "collect_test")
    results: Dict[str, dict] = {}
    any_failed = False
    for name in names:
        cfg = settings.sources.get(name, {})
        if not cfg.get("enabled", False):
            print(f"Skipping {name}: disabled in config.yaml")
            continue
        print(f"\n-- Collecting {name} --")
        try:
            collector = COLLECTORS[name](settings, raw_dir=raw_dir, checkpoint_dir=ckpt_dir, now=now, sleep=sleep,
                                         scan_limit=limit, **(collector_kwargs or {}).get(name, {}))
            res = collector.run()
        except Exception as e:  # noqa: BLE001 - e.g. invalid source config; other sources still run (FR1.5)
            log.error("%s could not start: %s", name, e)
            res = {"status": "failed", "error": str(e)}
        results[name] = res
        any_failed = any_failed or res.get("status") != "ok"
        summary.records_in += res.get("scanned", 0)
        for reason, n in (res.get("dropped") or {}).items():
            summary.dropped[f"{name}:{reason}"] += n
        if res.get("status") != "ok":
            summary.failed += 1
        if res.get("status") == "interrupted":
            break

    total = collate(raw_dir)
    summary.records_out = total

    stats = read_json(stats_path, {})
    block = stats.setdefault("collect", {})
    block.setdefault("sources", {}).update(results)
    block["raw_posts_total"] = total
    block["raw_posts_path"] = str(raw_dir / COLLATED_NAME)
    block["updated_at"] = iso_z(datetime.now(timezone.utc))
    atomic_write_text(stats_path, json.dumps(stats, indent=2, ensure_ascii=False))
    summary.finish(stats_path)

    print("\nPer source:")
    for name, r in results.items():
        line = (f"  {name:<11} status={r.get('status')}  scanned={r.get('scanned', 0)}  "
                f"matched={r.get('matched', 0)}  new={r.get('written_this_run', 0)}  in_file={r.get('total_in_file', 0)}")
        print(line)
        if r.get("per_star"):
            print("    per star (scanned/matched): " + ", ".join(
                f"{k}★ {v['scanned']}/{v['matched']}" for k, v in sorted(r["per_star"].items())))
        if r.get("per_sub"):
            print(f"    format={r.get('active_format')}  requests={r.get('requests_made', 0)}  "
                  f"posts={r.get('posts_matched', 0)}/{r.get('posts_scanned', 0)}  "
                  f"comments={r.get('comments_matched', 0)}/{r.get('comments_scanned', 0)}  "
                  f"matched_via={r.get('matched_via')}")
        if r.get("per_channel"):
            print(f"    quota_run={r.get('quota_units_this_run', 0)}u  "
                  f"quota_today={r.get('quota_units_today_pt', 0)}u  "
                  f"videos={r.get('videos_harvested', 0)}/{r.get('videos_queued', 0)}  "
                  f"comments={r.get('comments_matched', 0)}/{r.get('comments_scanned', 0)}  "
                  f"matched_via={r.get('matched_via')}")
            print("    per channel (videos, matched/scanned): " + ", ".join(
                f"{ch} ({v.get('videos_harvested', 0)}v, {v.get('comments_matched', 0)}/{v.get('comments_scanned', 0)})"
                for ch, v in r["per_channel"].items()))
        if r.get("per_query"):
            print(f"    requests={r.get('requests_made', 0)}  "
                  f"topics_harvested={r.get('topics_harvested', 0)}  "
                  f"topics={r.get('topics_matched', 0)}/{r.get('topics_scanned', 0)}  "
                  f"replies={r.get('replies_matched', 0)}/{r.get('replies_scanned', 0)}  "
                  f"matched_via={r.get('matched_via')}  platform_tag={r.get('platform_tag')}")
        if r.get("error"):
            print(f"    error: {r['error']}")
        if name == "app_store" and r.get("shortfall_note"):
            print(f"    {r['shortfall_note']}")
    print(f"\nCollated {total} records -> {raw_dir / COLLATED_NAME}")
    print(f"Stats -> {stats_path}")
    return 1 if any_failed else 0
