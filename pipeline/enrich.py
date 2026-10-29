"""Stage 3: Enrich (PRD §9.3, Architecture §4.5). The first stage that spends money.

    python run.py --stage golden-sample     # 0 LLM calls: pick the 20 golden posts, label them blind
    python run.py --stage enrich --limit 20 # paid: 20-post test (excludes golden posts), calibrates cost
    python run.py --stage enrich            # paid: full run, resumable; needs the test + golden labels

Per batch of `llm.batch_size` posts: one Gemini call (strict schema), then
  1. posts missing or invalid in the reply are retried once (FR3.3 / AC4),
  2. posts whose evidence_span is not verbatim in the masked text are retried once (FR3.5),
  3. labels that still lack verbatim evidence are downgraded to unknown and marked
     `evidence_span_failed` (excluded from scoring), so AC5 holds by construction.
A reply cut off at max_output_tokens is split in half and retried, never repaired blindly.
Every finished batch is appended to data/enriched/enriched_posts.jsonl and checkpointed, so a stop
(₹300 per-run cap, ₹1,000 project cap, HTTP 402, Ctrl+C) never re-bills finished posts.
Only the PII-masked text (clean_text) is ever read or sent.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, ConfigDict, ValidationError

from collectors.base import atomic_write_text, read_json
from core.budget import (BudgetError, BudgetTracker, Estimate, Usage, compute_cost_inr, estimate_tokens,
                         format_estimate)
from core.checkpoint import Checkpoint
from core.config import Settings
from core.llm_client import LLMClient, LLMProvider, LLMUnavailable
from core.logger import get_logger
from core.schemas import EnrichedPost, EnrichmentBatchResponse, EnrichmentFields
from core.utils import verify_evidence_span
from pipeline.golden import CUES, golden_ids, labels_status
from pipeline.golden import _first_hand as first_hand
from scoring.segment_fit import segment_fit

log = get_logger("enrich")

STAGE = "enrich"
ENRICHED_NAME = "enriched_posts.jsonl"
CALIBRATION_NAME = "enrich_calibration.json"
TEST_SOURCE_WEIGHTS = {"youtube": 5, "reddit": 5, "play_store": 5, "tradingqna": 3, "app_store": 2}

MISSING_SUFFIX = ("\n\nNOTE: these posts were missing or invalid in an earlier reply. Return exactly one item "
                  "per post_id above, following the schema and rules exactly.")
EVIDENCE_SUFFIX = ("\n\nNOTE: for these posts the earlier evidence_span was not an exact substring of the text. "
                   "Copy evidence_span character-for-character as ONE contiguous phrase from the text (same "
                   "spelling, case, punctuation; no '...', no translation). If no phrase supports the outcome or "
                   "trigger, set outcome and trigger_type to \"unknown\".")


class _LenientBatch(BaseModel):
    """Parse target: one malformed item must not fail the whole batch (items are validated one by one)."""
    model_config = ConfigDict(extra="ignore")
    items: List[Dict[str, Any]]


# --------------------------------------------------------------------------- helpers


def _paths(settings: Settings) -> Dict[str, Path]:
    return {
        "clean": settings.paths.resolve("clean_dir") / "clean_posts.jsonl",
        "enriched": settings.paths.resolve("enriched_dir") / ENRICHED_NAME,
        "calibration": settings.paths.resolve("checkpoints") / CALIBRATION_NAME,
        "stats": settings.paths.resolve("corpus_stats"),
        "prompt": settings.paths.resolve("prompts") / settings.enrich.prompt_file,
    }


def prompt_version(settings: Settings) -> str:
    return Path(settings.enrich.prompt_file).stem          # e.g. enrich_v1_20261027


def masked_text(post: dict) -> str:
    return post.get("clean_text") or post.get("text") or ""


def post_payload(post: dict, max_chars: int) -> dict:
    return {
        "post_id": post["id"],
        "source": post["source"],
        "platform_hint": (post.get("raw_meta") or {}).get("platform_tag", "unspecified"),
        "clean_borderline": bool(post.get("clean_borderline")),
        "hinglish": bool(post.get("hinglish")),
        "text": masked_text(post)[:max_chars],
    }


def build_prompt(posts: List[dict], max_chars: int, suffix: str = "") -> str:
    payload = [post_payload(p, max_chars) for p in posts]
    return "Posts (JSON):\n" + json.dumps(payload, ensure_ascii=False) + suffix


def needs_evidence(f: EnrichmentFields) -> bool:
    """FR3.2: a verbatim span is mandatory for any non-unknown outcome or trigger_type."""
    return f.is_relevant and (f.outcome != "unknown" or f.trigger_type not in ("unknown", "none"))


STOP_BRANCHES = {"voluntary_stop", "involuntary_payment_failure", "lapse_and_return", "platform_switch"}


def consistent_branch(f: EnrichmentFields) -> Tuple[str, List[int]]:
    """Deterministic outcome → kpi_branch rules (prompt v2 table). Returns (branch, scenario_ids)."""
    branch, ids = f.kpi_branch, list(f.stop_scenario_ids)
    if not f.is_relevant or f.journey_stage == "pre_start":
        branch = "not_applicable"
    elif f.outcome == "continued":
        branch = "continuing"
    elif f.outcome == "restarted":
        branch = "lapse_and_return"
    elif f.outcome in ("considering_stop", "paused", "reduced", "stopped"):
        if branch not in STOP_BRANCHES:
            branch = "voluntary_stop"
    elif f.outcome == "unknown" and branch != "involuntary_payment_failure":
        branch = "not_applicable"
    if branch not in STOP_BRANCHES:
        ids = []
    return branch, ids


def postprocess(f: EnrichmentFields, post: dict) -> Tuple[EnrichmentFields, bool]:
    """Returns (fields, changed_by_consistency_rules)."""
    upd: Dict[str, Any] = {}
    branch, ids = consistent_branch(f)
    fixed = (branch, ids) != (f.kpi_branch, list(f.stop_scenario_ids))
    if fixed:
        upd.update(kpi_branch=branch, stop_scenario_ids=ids)
    hint = (post.get("raw_meta") or {}).get("platform_tag")
    if f.platform_tag == "unspecified" and hint and hint != "unspecified":
        upd["platform_tag"] = hint
    return (f.model_copy(update=upd) if upd else f), (fixed and f.is_relevant)


def downgrade(f: EnrichmentFields) -> Tuple[EnrichmentFields, Dict[str, Any]]:
    unverified = {"outcome": f.outcome, "trigger_type": f.trigger_type, "kpi_branch": f.kpi_branch,
                  "stop_scenario_ids": list(f.stop_scenario_ids), "evidence_span": f.evidence_span}
    return f.model_copy(update={"outcome": "unknown", "trigger_type": "unknown", "kpi_branch": "not_applicable",
                                "stop_scenario_ids": [], "evidence_span": None}), unverified


def _rank(post_id: str, salt: str = "enrich-test") -> str:
    return hashlib.sha256(f"{salt}:{post_id}".encode()).hexdigest()


def select_test_sample(posts: List[dict], exclude: Set[str], n: int) -> List[dict]:
    """Deterministic, stratified by source and clean_borderline; never includes golden-set posts."""
    pool = sorted((p for p in posts if p["id"] not in exclude), key=lambda p: _rank(p["id"]))
    total_w = sum(TEST_SOURCE_WEIGHTS.values())
    quota = {s: max(1, round(n * w / total_w)) for s, w in TEST_SOURCE_WEIGHTS.items()}
    chosen: List[dict] = []
    used: Set[str] = set()
    for source, q in quota.items():
        src = [p for p in pool if p["source"] == source]
        # alternate non-borderline / borderline so both kinds are tested
        nb = [p for p in src if not p.get("clean_borderline")]
        bl = [p for p in src if p.get("clean_borderline")]
        mixed: List[dict] = []
        while nb or bl:
            if nb:
                mixed.append(nb.pop(0))
            if bl and len(mixed) % 3 == 2:
                mixed.append(bl.pop(0))
            elif not nb and bl:
                mixed.append(bl.pop(0))
        for p in mixed[:q]:
            chosen.append(p)
            used.add(p["id"])
    for p in pool:  # top up if a source ran short
        if len(chosen) >= n:
            break
        if p["id"] not in used:
            chosen.append(p)
            used.add(p["id"])
    return chosen[:n]


def select_event_sample(posts: List[dict], exclude: Set[str], n: int) -> List[dict]:
    """Posts that describe a first-hand SIP event (stop / pause / failure / restart / continue), so a test can
    check that real events still get tagged. Deterministic; never includes golden-set posts."""
    cues = ("voluntary", "payment", "restart", "continuer")

    def is_event(p: dict) -> bool:
        txt = masked_text(p)
        return (p["id"] not in exclude and 60 <= len(txt) <= 1500 and first_hand(p)
                and any(CUES[c].search(txt) for c in cues))

    events = [p for p in posts if is_event(p)]
    return select_test_sample(events, exclude, n)


class Calibration:
    """Measured token use, written by the 20-post test and updated by every run."""

    def __init__(self, path: Path, version: Optional[str] = None):
        self.path = path
        self.d = read_json(path, {}) or {}
        if version and self.d and self.d.get("prompt_version") != version:
            # a new prompt changes token use: start fresh, keep the old numbers for the record
            history = self.d.pop("history", [])
            history.append(self.d)
            self.d = {"history": history}
        if version:
            self.d["prompt_version"] = version

    @property
    def ready(self) -> bool:
        return self.d.get("main_posts", 0) > 0

    def add_main(self, n_posts: int, usage: Usage) -> None:
        d = self.d
        d["main_calls"] = d.get("main_calls", 0) + 1
        d["main_posts"] = d.get("main_posts", 0) + n_posts
        d["main_output_tokens"] = d.get("main_output_tokens", 0) + usage.output_tokens + usage.thinking_tokens
        d["max_output_per_post"] = max(d.get("max_output_per_post", 0),
                                       (usage.output_tokens + usage.thinking_tokens) / max(n_posts, 1))

    def add_spend(self, n_posts: int, usage: Usage, cost_inr: float) -> None:
        d = self.d
        d["posts_done"] = d.get("posts_done", 0) + n_posts
        d["input_tokens"] = d.get("input_tokens", 0) + usage.input_tokens
        d["cached_tokens"] = d.get("cached_tokens", 0) + usage.cached_tokens
        d["output_tokens"] = d.get("output_tokens", 0) + usage.output_tokens + usage.thinking_tokens
        d["cost_inr"] = d.get("cost_inr", 0.0) + cost_inr

    @property
    def avg_output_per_post(self) -> Optional[float]:
        return self.d["main_output_tokens"] / self.d["main_posts"] if self.ready else None

    @property
    def cost_per_post(self) -> Optional[float]:
        n = self.d.get("posts_done", 0)
        return self.d.get("cost_inr", 0.0) / n if n else None

    def per_post_tokens(self) -> Tuple[Optional[float], Optional[float]]:
        n = self.d.get("posts_done", 0)
        if not n:
            return None, None
        return (self.d.get("input_tokens", 0) + self.d.get("cached_tokens", 0)) / n, self.d.get("output_tokens", 0) / n

    def save(self) -> None:
        self.d["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        atomic_write_text(self.path, json.dumps(self.d, indent=2))


def output_limit_per_post(settings: Settings, calib: Calibration) -> int:
    floor = max(settings.enrich.output_floor_tokens_per_post, settings.llm.max_output_tokens.get("enrich_per_post", 0))
    avg = calib.avg_output_per_post
    return max(floor, math.ceil(avg * settings.enrich.output_headroom)) if avg else floor


# --------------------------------------------------------------------------- enricher


class Enricher:
    def __init__(self, settings: Settings, client: LLMClient, system_prompt: str, calib: Calibration):
        self.s = settings
        self.client = client
        self.system = system_prompt
        self.calib = calib
        self.max_chars = settings.llm.max_input_chars_per_post
        self.out_per_post = output_limit_per_post(settings, calib)
        self.c: Counter = Counter()
        self.kpi: Counter = Counter()
        self.scen: Counter = Counter()
        self.outcome: Counter = Counter()
        self.by_source: Dict[str, Counter] = {}

    def _expected_cost(self, prompt: str, n: int) -> float:
        in_tok = estimate_tokens(prompt) + estimate_tokens(self.system)
        avg_out = self.calib.avg_output_per_post or self.s.budget.estimate_defaults.avg_output_tokens_per_post
        return compute_cost_inr(Usage(input_tokens=in_tok, output_tokens=int(n * avg_out)), self.s.budget)

    def call(self, posts: List[dict], batch_id: str, suffix: str = "", main: bool = False) -> Dict[str, EnrichmentFields]:
        """One Gemini call (recursively split on MAX_TOKENS / unrecoverable JSON). Returns valid items by id."""
        out: Dict[str, EnrichmentFields] = {}
        if not posts:
            return out
        prompt = build_prompt(posts, self.max_chars, suffix)
        res = self.client.generate_json(
            prompt=prompt, system=self.system, schema=EnrichmentBatchResponse, parse_schema=_LenientBatch,
            batch_id=batch_id, max_output_tokens=len(posts) * self.out_per_post,
            expected_cost_inr=self._expected_cost(prompt, len(posts)),
        )
        self.c["calls"] += res.attempts
        cost = compute_cost_inr(res.usage, self.s.budget)
        self.calib.add_spend(0, res.usage, cost)
        if res.status in ("max_tokens", "enrichment_failed"):
            self.c[f"calls_{res.status}"] += 1
            if len(posts) > 1:
                mid = len(posts) // 2
                out.update(self.call(posts[:mid], batch_id + "a", suffix, main))
                out.update(self.call(posts[mid:], batch_id + "b", suffix, main))
            return out
        if main:
            self.calib.add_main(len(posts), res.usage)
        ids = {p["id"] for p in posts}
        for item in res.data.items:
            pid = item.get("post_id")
            if pid not in ids or pid in out:
                continue
            try:
                out[pid] = EnrichmentFields.model_validate(item)
            except ValidationError:
                self.c["invalid_items"] += 1
        return out

    def process_batch(self, batch: List[dict], batch_id: str) -> List[dict]:
        cfg = self.s.enrich
        by_id = {p["id"]: p for p in batch}
        res = self.call(batch, batch_id, main=True)
        self.c["posts_attempted"] += len(batch)
        self.c["valid_first_pass"] += len(res)

        missing = [p for p in batch if p["id"] not in res]
        if missing and cfg.retry_missing_once:
            self.c["retried_missing"] += len(missing)
            res.update(self.call(missing, batch_id + "-missing", MISSING_SUFFIX))

        def verified(pid: str, f: EnrichmentFields) -> bool:
            return verify_evidence_span(f.evidence_span, masked_text(by_id[pid]))

        need = [pid for pid, f in res.items() if needs_evidence(f)]
        self.c["evidence_needed"] += len(need)
        bad = [pid for pid in need if not verified(pid, res[pid])]
        self.c["evidence_ok_first_pass"] += len(need) - len(bad)
        if bad:  # keep the failing spans: needed to diagnose the prompt, and examples for the Evidence Log
            log_dir = self.s.paths.resolve("outputs") / "enrich_tests"
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / f"evidence_failures_{prompt_version(self.s)}.jsonl", "a", encoding="utf-8") as fh:
                for pid in bad:
                    fh.write(json.dumps({"post_id": pid, "source": by_id[pid]["source"], "batch_id": batch_id,
                                         "span": res[pid].evidence_span, "outcome": res[pid].outcome,
                                         "trigger_type": res[pid].trigger_type,
                                         "text": masked_text(by_id[pid])[:800]}, ensure_ascii=False) + "\n")
        if bad and cfg.retry_evidence_once:
            self.c["retried_evidence"] += len(bad)
            again = self.call([by_id[pid] for pid in bad], batch_id + "-evidence", EVIDENCE_SUFFIX)
            for pid, f in again.items():
                if not needs_evidence(f) or verified(pid, f):
                    res[pid] = f

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = []
        for p in batch:
            pid = p["id"]
            f = res.get(pid)
            status, ev_ok, unverified = "ok", False, None
            if f is None:
                status = "enrichment_failed"
                self.c["enrichment_failed"] += 1
            else:
                f, fixed = postprocess(f, p)
                if fixed:
                    self.c["consistency_fixes"] += 1
                if needs_evidence(f):
                    ev_ok = verified(pid, f)
                    if not ev_ok:
                        f, unverified = downgrade(f)
                        status = "evidence_span_failed"
                        self.c["evidence_downgraded"] += 1
                else:
                    ev_ok = bool(f.evidence_span) and verified(pid, f)
                self.c["valid_final"] += 1
                if f.is_relevant:
                    self.c["relevant"] += 1
                self.kpi[f.kpi_branch] += 1
                self.outcome[f.outcome] += 1
                for i in f.stop_scenario_ids:
                    self.scen[i] += 1
                src = self.by_source.setdefault(p["source"], Counter())
                src["enriched"] += 1
                src["relevant"] += int(f.is_relevant)
            row = EnrichedPost(
                **{k: p[k] for k in ("id", "source", "url", "date", "author_hash", "collection_method",
                                     "keyword_group", "raw_meta", "hinglish", "pii_stripped", "clean_borderline")},
                text=masked_text(p), clean_text=masked_text(p),
                enrichment=f, evidence_verified=ev_ok,
                segment_fit=segment_fit(f) if status == "ok" else None,
                prompt_version=prompt_version(self.s), enriched_at=now, status=status,
                unverified_labels=unverified,
            )
            rows.append(row.model_dump(mode="json"))
        return rows

    def stats(self) -> Dict[str, Any]:
        c = self.c
        att = c["posts_attempted"] or 1
        need = c["evidence_needed"] or 1
        return {
            "posts_attempted": c["posts_attempted"],
            "schema_valid_first_pass_pct": round(100 * c["valid_first_pass"] / att, 1),
            "schema_valid_final_pct": round(100 * c["valid_final"] / att, 1),
            "enrichment_failed": c["enrichment_failed"],
            "evidence_needed": c["evidence_needed"],
            "evidence_verbatim_first_pass_pct": round(100 * c["evidence_ok_first_pass"] / need, 1) if c["evidence_needed"] else None,
            "evidence_downgraded": c["evidence_downgraded"],
            "retried_missing": c["retried_missing"],
            "retried_evidence": c["retried_evidence"],
            "calls": c["calls"],
            "calls_split_max_tokens": c["calls_max_tokens"],
            "calls_split_invalid_json": c["calls_enrichment_failed"],
            "relevant": c["relevant"],
            "consistency_fixes": c["consistency_fixes"],
            "kpi_branch": dict(self.kpi),
            "stop_scenarios": {str(k): v for k, v in sorted(self.scen.items())},
            "outcome": dict(self.outcome),
            "by_source": {k: dict(v) for k, v in self.by_source.items()},
        }


# --------------------------------------------------------------------------- run


def _load(path: Path) -> List[dict]:
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()] if path.exists() else []


def _done_ids(paths: Dict[str, Path], ckpt: Checkpoint) -> Set[str]:
    done = set(ckpt._done)
    for r in _load(paths["enriched"]):   # also trust rows already written (crash between write and checkpoint)
        done.add(r["id"])
    return done


def build_estimate(settings: Settings, n_posts: int, calib: Calibration, system_prompt: str,
                   sample_posts: List[dict]) -> Estimate:
    bs = settings.llm.batch_size
    calls = math.ceil(n_posts / bs) if n_posts else 0
    out_limit = output_limit_per_post(settings, calib)
    in_pp, out_pp = calib.per_post_tokens()
    if in_pp is None:  # not calibrated yet: estimate from the actual texts + prompt size
        texts = sum(estimate_tokens(json.dumps(post_payload(p, settings.llm.max_input_chars_per_post),
                                               ensure_ascii=False)) for p in sample_posts)
        in_pp = texts / max(len(sample_posts), 1) + estimate_tokens(system_prompt) / bs + 400 / bs  # + schema
        out_pp = settings.budget.estimate_defaults.avg_output_tokens_per_post
    in_tok, out_tok = int(n_posts * in_pp), int(n_posts * out_pp)
    cost = (calib.cost_per_post * n_posts) if calib.cost_per_post else \
        compute_cost_inr(Usage(input_tokens=in_tok, output_tokens=out_tok), settings.budget)
    worst = compute_cost_inr(Usage(input_tokens=in_tok, output_tokens=n_posts * out_limit), settings.budget)
    est = Estimate(STAGE, calls, in_tok, out_tok, cost, worst, posts_total=n_posts, posts_done=0, batch_size=bs)
    est.note = "calibrated from measured runs" if calib.cost_per_post else "uncalibrated defaults"
    return est


def _print_sample(rows: List[dict], k: int = 5) -> None:
    print(f"\n== {min(k, len(rows))} sample outputs (check these by eye) ==")
    picked = sorted(rows, key=lambda r: (not (r.get("enrichment") or {}).get("is_relevant", False), r["source"]))[:k]
    for r in picked:
        e = r.get("enrichment") or {}
        print(f"\n[{r['source']}] {r['id']}  status={r['status']}")
        print(f"  text      : {r['text'][:220].replace(chr(10), ' ')}{'…' if len(r['text']) > 220 else ''}")
        print(f"  relevant={e.get('is_relevant')}  outcome={e.get('outcome')}  kpi_branch={e.get('kpi_branch')}  "
              f"scenarios={e.get('stop_scenario_ids')}  stage={e.get('journey_stage')}")
        print(f"  trigger={e.get('trigger_type')}  seed={e.get('seeded_category_primary')}  "
              f"discovered={e.get('discovered_category')}  platform={e.get('platform_tag')}  "
              f"segment_fit={r.get('segment_fit')}")
        print(f"  evidence  : {e.get('evidence_span')!r}  verified={r['evidence_verified']}")


def _write_stats(paths: Dict[str, Path], key: str, data: Dict[str, Any]) -> None:
    stats = read_json(paths["stats"], {}) or {}
    stats.setdefault("enrich", {})[key] = data
    atomic_write_text(paths["stats"], json.dumps(stats, indent=2))


def _redo_previous_test(paths: Dict[str, Path], ckpt: Checkpoint, ids: List[str], version: str) -> Dict[str, dict]:
    """Archive the previous test rows (Evidence Log: corrected AI output) and make those posts pending again."""
    rows = _load(paths["enriched"])
    idset = set(ids)
    old = [r for r in rows if r["id"] in idset]
    keep = [r for r in rows if r["id"] not in idset]
    arch_dir = paths["stats"].parent / "enrich_tests"
    arch_dir.mkdir(parents=True, exist_ok=True)
    tag = old[0]["prompt_version"] if old else "unknown"
    with open(arch_dir / f"{tag}_test.jsonl", "w", encoding="utf-8") as f:
        for r in old:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    atomic_write_text(paths["enriched"], "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep))
    ckpt.unmark(ids)
    print(f"Re-testing the same {len(ids)} posts with {version}; previous ({tag}) outputs archived to "
          f"{arch_dir / (tag + '_test.jsonl')}.")
    return {r["id"]: r for r in old}


def _print_before_after(before: Dict[str, dict], after: List[dict]) -> None:
    def brief(r):
        e = r.get("enrichment") or {}
        return (f"rel={e.get('is_relevant')} outcome={e.get('outcome')} branch={e.get('kpi_branch')} "
                f"ids={e.get('stop_scenario_ids')} trigger={e.get('trigger_type')}")
    changed = [(before[r["id"]], r) for r in after if r["id"] in before and brief(before[r["id"]]) != brief(r)]
    print(f"\n== Before vs after on the same posts: {len(changed)} of {len(after)} changed ==")
    for b, a in changed:
        print(f"\n[{a['source']}] {a['text'][:160].replace(chr(10), ' ')}")
        print(f"  before ({b['prompt_version']}): {brief(b)}")
        print(f"  after  ({a['prompt_version']}): {brief(a)}")


def run_enrich(settings: Settings, limit: Optional[int] = None, approve: Callable[[], bool] = lambda: False,
               provider: Optional[LLMProvider] = None, api_key: Optional[str] = None,
               sleep: Callable[[float], None] = time.sleep, redo_test: bool = False,
               event_sample: bool = False) -> int:
    paths = _paths(settings)
    test_run = limit is not None
    posts = _load(paths["clean"])
    if not posts:
        print(f"No clean posts at {paths['clean']}. Run `--stage clean` first.")
        return 1
    ckpt = Checkpoint(STAGE, settings.paths.resolve("checkpoints"))
    prior = read_json(paths["calibration"], {}) or {}
    last_test_ids = prior.get("last_test_ids") or next(
        (h.get("last_test_ids") for h in reversed(prior.get("history", [])) if h.get("last_test_ids")), None)
    before: Dict[str, dict] = {}
    existing_rows = _load(paths["enriched"])
    if redo_test and not last_test_ids:
        # tests made before last_test_ids was recorded: if no full run has happened yet, every enriched row
        # with an older prompt version came from the test
        older = [r["id"] for r in existing_rows if r["prompt_version"] != prompt_version(settings)]
        if older and len(existing_rows) <= 2 * settings.enrich.test_sample_size:
            last_test_ids = older
    if redo_test:
        if not test_run or not last_test_ids:
            print("--redo-test needs --limit and a previous test run.")
            return 2
        if any(r["prompt_version"] == prompt_version(settings) for r in existing_rows
               if r["id"] in set(last_test_ids)):
            print(f"The previous test already used {prompt_version(settings)}; change the prompt first.")
            return 2
    calib = Calibration(paths["calibration"], prompt_version(settings))
    done = _done_ids(paths, ckpt)
    pending = [p for p in posts if p["id"] not in done]
    golden = golden_ids(settings)
    system_prompt = paths["prompt"].read_text(encoding="utf-8")

    print(f"\n== Stage 3: Enrich ({'20-post TEST' if test_run else 'FULL run'}) — prompt {prompt_version(settings)} ==")
    print(f"Clean posts: {len(posts):,}   already enriched: {len(done):,}   pending: {len(pending):,}")
    if not golden:
        print("Golden set not sampled yet. Run `python run.py --stage golden-sample` first (0 LLM calls).")
        return 2
    if test_run:
        if redo_test:
            by_id = {p["id"]: p for p in posts}
            todo = [by_id[i] for i in last_test_ids if i in by_id]
        elif event_sample:
            todo = select_event_sample(pending, golden, limit)
            print("Event sample: posts with first-hand stop/pause/failure/restart/continue wording.")
        else:
            todo = select_test_sample(pending, golden, limit)
        if not event_sample:
            calib.d["last_test_ids"] = [p["id"] for p in todo]
        print(f"Test sample: {len(todo)} posts, stratified by source, excluding the {len(golden)} golden-set posts.")
    else:
        if not calib.ready:
            print("Run the 20-post test first: `python run.py --stage enrich --limit 20`.")
            return 2
        complete, missing, total = labels_status(settings)
        if settings.enrich.require_golden_labels_before_full_run and not complete:
            print(f"Golden set not fully labelled ({total - missing}/{total} rows complete) in "
                  f"{settings.paths.resolve('seeds_dir') / 'golden_human.csv'}. Label it blind first "
                  "(see docs/labelling_guide.md), then re-run.")
            return 2
        todo = pending
    if not todo:
        print("Nothing to enrich.")
        return 0

    tracker = BudgetTracker(settings.budget, settings.paths.resolve("spend_log"), stage=STAGE,
                            model=settings.llm.model, prompt_version=prompt_version(settings))
    tracker.check_can_start()
    est = build_estimate(settings, len(todo), calib, system_prompt, todo[:50])
    print(format_estimate(est, tracker))
    usable = max(tracker.per_run_remaining - 5, 1)  # leave room for one call's worst case
    runs = max(1, math.ceil(est.cost_inr / usable))
    print(f"Estimate basis   : {est.note}. Output limit {output_limit_per_post(settings, calib)} tokens/post.")
    print(f"Per-run cap ₹{settings.budget.per_run_cap_inr:.0f} → expect about {runs} run(s) of this command "
          f"(re-run the same command after each cap stop; finished posts are never re-billed).")
    if not approve():
        return 0
    if redo_test:  # only after approval: archive the old outputs and make those posts pending again
        before = _redo_previous_test(paths, ckpt, [p["id"] for p in todo], prompt_version(settings))

    client = LLMClient(settings, tracker, provider=provider, api_key=api_key)
    enr = Enricher(settings, client, system_prompt, calib)
    bs = settings.llm.batch_size
    batches = [todo[i:i + bs] for i in range(0, len(todo), bs)]
    paths["enriched"].parent.mkdir(parents=True, exist_ok=True)
    written: List[dict] = []
    rc, stop_reason = 0, None
    t0 = time.monotonic()
    try:
        for bi, batch in enumerate(batches, 1):
            batch_id = f"{'test' if test_run else 'enrich'}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{bi}"
            rows = enr.process_batch(batch, batch_id)
            with open(paths["enriched"], "a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            ckpt.mark_done([r["id"] for r in rows])
            calib.d["posts_done"] = calib.d.get("posts_done", 0) + len(rows)
            calib.save()
            written.extend(rows)
            ok = sum(r["status"] == "ok" for r in rows)
            print(f"batch {bi}/{len(batches)}: {len(rows)} posts, ok {ok}, "
                  f"relevant {sum(bool((r.get('enrichment') or {}).get('is_relevant')) for r in rows)}, "
                  f"run spend ₹{tracker.run_spend:.2f}, project ₹{tracker.project_spend:.2f}")
            if bi < len(batches):
                sleep(settings.llm.inter_batch_pause_sec)
    except BudgetError as e:
        stop_reason, rc = f"Budget stop: {e}", 1
    except LLMUnavailable as e:
        stop_reason, rc = f"Gemini unavailable (HTTP {e.code or 'unknown'}): {e.error_message}", 1
    except KeyboardInterrupt:
        stop_reason, rc = "Stopped by you (Ctrl+C).", 1
    calib.save()

    st = enr.stats()
    st.update({"run_type": "test" if test_run else "full", "prompt_version": prompt_version(settings),
               "posts_written": len(written), "run_cost_inr": round(tracker.run_spend, 4),
               "project_spend_inr": round(tracker.project_spend, 4),
               "time_taken_sec": round(time.monotonic() - t0, 1),
               "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "stopped_early": stop_reason})
    in_pp, out_pp = calib.per_post_tokens()
    st["tokens_per_post"] = {"input_incl_cached": round(in_pp or 0, 1), "output": round(out_pp or 0, 1)}
    st["cost_per_post_inr"] = round(calib.cost_per_post or 0, 4)
    _write_stats(paths, "last_test" if test_run else "last_full", st)

    print("\n== Enrich summary ==")
    for k in ("posts_written", "schema_valid_first_pass_pct", "schema_valid_final_pct", "enrichment_failed",
              "evidence_needed", "evidence_verbatim_first_pass_pct", "evidence_downgraded", "relevant",
              "consistency_fixes",
              "kpi_branch", "stop_scenarios", "outcome", "calls", "calls_split_max_tokens",
              "tokens_per_post", "cost_per_post_inr", "run_cost_inr", "project_spend_inr", "time_taken_sec"):
        print(f"  {k:<33}: {st.get(k)}")
    print(f"  AC4 schema-valid ≥95%            : {'PASS' if st['schema_valid_final_pct'] >= 95 else 'FAIL'}")
    print("  AC5 evidence verbatim            : PASS (unverifiable labels are downgraded and excluded)")
    if stop_reason:
        print(f"\n{stop_reason}\nProgress is saved. Re-run the same command to continue.")
    if test_run and written:
        if before:
            _print_before_after(before, written)
        _print_sample(written)
        remaining = len(pending) - len(written)
        full = build_estimate(settings, remaining, calib, system_prompt, [])
        usable = max(settings.budget.per_run_cap_inr - 5, 1)
        print(f"\n== Full-run estimate (measured) ==\n  remaining posts : {remaining:,}\n"
              f"  est. cost       : ₹{full.cost_inr:.2f}  (₹{calib.cost_per_post or 0:.4f}/post)\n"
              f"  runs needed     : ~{max(1, math.ceil(full.cost_inr / usable))} at the ₹"
              f"{settings.budget.per_run_cap_inr:.0f} per-run cap\n"
              f"  output limit    : {output_limit_per_post(settings, calib)} tokens/post "
              f"(measured avg {calib.avg_output_per_post or 0:.0f} × {settings.enrich.output_headroom})")
    return rc
