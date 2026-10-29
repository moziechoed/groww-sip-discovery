"""Golden set (PRD §9.6, FR6.1–FR6.3): pick 20 posts BEFORE enrichment so the PM labels them blind.

    python run.py --stage golden-sample      # zero LLM calls

Writes
- outputs/golden_set.csv        blank template (post_id, bucket, source, text + empty label columns)
- data/seeds/golden_human.csv   the PM's copy to fill in. Never overwritten once any label is filled.
- docs/labelling_guide.md       label definitions copied verbatim from the enrichment prompt
The 20 posts stay in the corpus and are enriched normally; they are only excluded from the 20-post
test run and its printed samples, so the PM never sees engine labels for them before labelling.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from core.config import ROOT, Settings
from core.logger import get_logger

log = get_logger("golden")

REQUIRED_LABELS = ["is_relevant", "outcome", "kpi_branch", "stop_scenario_ids", "trigger_type",
                   "seeded_category_primary", "journey_stage"]
OPTIONAL_LABELS = ["outside_app_behaviour", "segment_confidence"]
LABEL_COLUMNS = REQUIRED_LABELS + OPTIONAL_LABELS
CSV_COLUMNS = ["post_id", "bucket", "source", "text"] + LABEL_COLUMNS + ["notes"]

STRATIFIED_SOURCE_QUOTA = {"youtube": 3, "reddit": 3, "play_store": 3, "tradingqna": 2, "app_store": 1}
MIN_CHARS, MAX_CHARS = 80, 1200

CUES = {
    "voluntary": re.compile(r"\b(stopp?ed|stopping|paus(e|ed|ing)|cancel+(ed|ling)?|band kar|redeem(ed)?|withdr[ae]wn?)\b", re.I),
    "payment": re.compile(r"\b(mandate|auto ?-?pay|auto ?-?debit|debit(ed)?|bounc(e|ed)|failed|kyc)\b", re.I),
    "continuer": re.compile(r"\b(continu(e|ed|ing)|still (going|running|investing)|kept|never stopped|(\d+|one|two|three) (years?|yrs))\b", re.I),
    "restart": re.compile(r"\b(restart(ed)?|resum(e|ed)|start(ed)? again)\b", re.I),
    "returns": re.compile(r"\b(returns?|loss(es)?|negative|xirr)\b", re.I),
    "reduce": re.compile(r"\b(reduc(e|ed)|lower(ed)?|decreas(e|ed)|step ?down)\b", re.I),
}
STRATIFIED_ROTATION = ["voluntary", "payment", "continuer", "restart", "returns"]
# Stratified picks must read as first-hand experience, so agreement isn't inflated by easy "not relevant" rows.
FIRST_HAND = re.compile(
    r"\b(my (first )?sips?|my (mutual fund|mf|autopay|auto pay|mandate|portfolio|investment)s?"
    r"|i (started|stopped|paused|cancel+ed|restarted|resumed|invested|missed|reduced|increased|had|was|have|"
    r"am investing|began|set up|created|switched|moved|withdrew|redeemed)"
    r"|i'?ve been|i'?m investing|i'?ve (started|stopped|paused|invested)"
    r"|maine|mera sip|meri sip|mere sip|mera paisa|mere paise)\b", re.I)


def _first_hand(p: dict) -> bool:
    return bool(FIRST_HAND.search(p.get("clean_text") or p["text"]))


def _rank(seed: int, post_id: str) -> str:
    return hashlib.sha256(f"{seed}:{post_id}".encode()).hexdigest()


def _cues(text: str) -> Set[str]:
    return {name for name, rx in CUES.items() if rx.search(text or "")}


def select_golden(posts: List[dict], settings: Settings) -> List[Tuple[dict, str]]:
    """Deterministic: (post, bucket) pairs, ~12 stratified + ~8 ambiguous (FR6.1)."""
    cfg = settings.enrich
    pool = [p for p in posts if MIN_CHARS <= len(p.get("clean_text") or p["text"]) <= MAX_CHARS]
    pool.sort(key=lambda p: _rank(cfg.golden_seed, p["id"]))
    for p in pool:
        p["_cues"] = _cues(p.get("clean_text") or p["text"])
    chosen: List[Tuple[dict, str]] = []
    used: Set[str] = set()

    def take(cands: List[dict], n: int, bucket: str) -> int:
        got = 0
        for p in cands:
            if got >= n:
                break
            if p["id"] in used:
                continue
            chosen.append((p, bucket))
            used.add(p["id"])
            got += 1
        return got

    # ~12 stratified: per-source quota, non-borderline, rotating through behaviour cues
    quota = dict(STRATIFIED_SOURCE_QUOTA)
    scale = cfg.golden_size_stratified / max(sum(quota.values()), 1)
    quota = {k: max(1, round(v * scale)) if cfg.golden_size_stratified else 0 for k, v in quota.items()}
    rot = 0
    for source, n in quota.items():
        src = [p for p in pool if p["source"] == source and not p.get("clean_borderline") and _first_hand(p)]
        for _ in range(n):
            picked = 0
            for k in range(len(STRATIFIED_ROTATION)):
                cue = STRATIFIED_ROTATION[(rot + k) % len(STRATIFIED_ROTATION)]
                if take([p for p in src if cue in p["_cues"]], 1, "stratified"):
                    picked = 1
                    break
            if not picked:
                take(src, 1, "stratified")
            rot += 1

    # ~8 ambiguous: deliberately hard cases
    n_amb = cfg.golden_size_ambiguous
    rules = [
        (2, lambda p: {"voluntary", "payment"} <= p["_cues"] and _first_hand(p)),  # chose vs failed?
        (2, lambda p: p.get("hinglish") and p["_cues"] & {"voluntary", "payment", "restart"} and _first_hand(p)),
        (2, lambda p: p.get("clean_borderline") and p["_cues"]),                 # rule thought generic (may be irrelevant)
        (1, lambda p: "reduce" in p["_cues"] and _first_hand(p)),                 # reduced vs stopped
        (1, lambda p: {"continuer", "voluntary"} <= p["_cues"] and _first_hand(p)),  # considering vs continuing
    ]
    got_amb = 0
    for n, rule in rules:
        if got_amb >= n_amb:
            break
        got_amb += take([p for p in pool if rule(p)], min(n, n_amb - got_amb), "ambiguous")
    if got_amb < n_amb:  # top up deterministically
        got_amb += take([p for p in pool if p["_cues"]], n_amb - got_amb, "ambiguous")

    for p, _ in chosen:
        p.pop("_cues", None)
    for p in pool:
        p.pop("_cues", None)
    chosen.sort(key=lambda pb: _rank(cfg.golden_seed + 1, pb[0]["id"]))  # mix buckets in the sheet
    return chosen


def _paths(settings: Settings) -> Dict[str, Path]:
    return {
        "template": settings.paths.resolve("outputs") / "golden_set.csv",
        "human": settings.paths.resolve("seeds_dir") / "golden_human.csv",
        "guide": ROOT / "docs" / "labelling_guide.md",
        "prompt": settings.paths.resolve("prompts") / settings.enrich.prompt_file,
    }


def read_golden_rows(settings: Settings) -> List[dict]:
    path = _paths(settings)["human"]
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def golden_ids(settings: Settings) -> Set[str]:
    rows = read_golden_rows(settings)
    if not rows:
        tpl = _paths(settings)["template"]
        if tpl.exists():
            with open(tpl, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
    return {r["post_id"] for r in rows if r.get("post_id")}


def labels_status(settings: Settings) -> Tuple[bool, int, int]:
    """(complete, rows_missing_labels, total_rows). A row is complete when is_relevant is filled and,
    if is_relevant is true, every required label is filled ("none" counts as filled)."""
    rows = read_golden_rows(settings)
    missing = 0
    for r in rows:
        rel = (r.get("is_relevant") or "").strip().lower()
        if rel not in ("true", "false"):
            missing += 1
            continue
        if rel == "true" and any(not (r.get(c) or "").strip() for c in REQUIRED_LABELS):
            missing += 1
    return (bool(rows) and missing == 0), missing, len(rows)


def _any_label_filled(rows: List[dict]) -> bool:
    return any((r.get(c) or "").strip() for r in rows for c in LABEL_COLUMNS + ["notes"])


def _write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in CSV_COLUMNS})


def extract_definitions(prompt_text: str) -> str:
    m = re.search(r"### DEFINITIONS START\s*(.*?)\s*### DEFINITIONS END", prompt_text, re.S)
    if not m:
        raise ValueError("Prompt file has no '### DEFINITIONS START/END' section")
    return m.group(1).strip()


def build_guide(prompt_text: str, prompt_file: str) -> str:
    defs = extract_definitions(prompt_text)
    return f"""# Golden Set Labelling Guide

Label the 20 posts in `data/seeds/golden_human.csv` **before** looking at any engine output. The engine's
labels for these posts are kept separately (`outputs/golden_engine.csv`, written in P9) so this stays blind.

The definitions below are copied word for word from the enrichment prompt (`prompts/{prompt_file}`), so you
and the engine label against exactly the same rules.

## How to fill the sheet
- One row per post. Read `text`, then fill the label columns. Leave `post_id`, `bucket`, `source`, `text` unchanged.
- `is_relevant`: write `true` or `false`. If `false`, you can leave the other columns blank.
- If `true`, fill every required column. Write `none` when nothing applies (e.g., `stop_scenario_ids` = `none`,
  `seeded_category_primary` = `none` when only a new, unlisted category fits; describe it in `notes`).
- `stop_scenario_ids`: up to 3 ids separated by `;` (e.g., `2;5`), most important first.
- Use the exact values listed (lowercase, underscores). Use `unknown` when the post doesn't say.
- `outside_app_behaviour` and `segment_confidence` are optional; blank means "not scored".
- `notes`: anything you were unsure about. It helps explain disagreements later.
- Editing tips: Google Sheets keeps the text intact (File → Import → Upload, then File → Download → CSV and
  replace `data/seeds/golden_human.csv`). Excel can garble symbols like ₹ unless saved as "CSV UTF-8".

## Columns
| Column | Required | Allowed values |
|---|---|---|
| is_relevant | yes | true / false |
| outcome | yes (if relevant) | continued / paused / reduced / stopped / restarted / considering_stop / unknown |
| kpi_branch | yes (if relevant) | voluntary_stop / involuntary_payment_failure / lapse_and_return / platform_switch / continuing / not_applicable |
| stop_scenario_ids | yes (if relevant) | `none`, or 1–3 ids from 1–17 separated by `;` |
| trigger_type | yes (if relevant) | life_event_financial_pressure / emotional / returns_disappointment / ux_friction / financial_literacy_gap / feature_gap / social_peer_influence / other / none / unknown |
| seeded_category_primary | yes (if relevant) | ux_friction / emotional_triggers / financial_literacy_gaps / feature_gaps / life_event_financial_pressure / social_peer_influence / none |
| journey_stage | yes (if relevant) | pre_start / month_0_1 / month_2_3 / month_4_6 / month_7_12 / month_12_plus / post_stop / unknown |
| outside_app_behaviour | optional | asked_family_friends / youtube / reddit_forums / advisor / news / did_nothing / other / unknown |
| segment_confidence | optional | high / medium / low |

## How agreement will be measured (P9)
- Per field: your label vs the engine's label, exact match.
- `stop_scenario_ids`: counted as agreement if the two lists share at least one id; exact-set match is reported too.
- Reported per field, overall, and split by bucket (stratified vs ambiguous). Targets: ≥70% overall, ≥75% on `outcome`.

## Definitions (verbatim from the prompt)

{defs}
"""


def run_golden_sample(settings: Settings) -> int:
    paths = _paths(settings)
    clean_path = settings.paths.resolve("clean_dir") / "clean_posts.jsonl"
    if not clean_path.exists():
        print(f"Clean output not found at {clean_path}. Run `--stage clean` first.")
        return 1
    posts = [json.loads(l) for l in open(clean_path, encoding="utf-8") if l.strip()]

    existing = read_golden_rows(settings)
    if existing and _any_label_filled(existing):
        complete, missing, total = labels_status(settings)
        print(f"{paths['human']} already has labels ({total - missing}/{total} rows complete). "
              "Not overwriting. Delete it by hand only if you really want a new sample.")
        return 0

    chosen = select_golden(posts, settings)
    rows = [{"post_id": p["id"], "bucket": b, "source": p["source"], "text": p.get("clean_text") or p["text"]}
            for p, b in chosen]
    _write_csv(paths["template"], rows)
    _write_csv(paths["human"], rows)
    prompt_text = paths["prompt"].read_text(encoding="utf-8")
    paths["guide"].parent.mkdir(parents=True, exist_ok=True)
    paths["guide"].write_text(build_guide(prompt_text, settings.enrich.prompt_file), encoding="utf-8")

    by_bucket: Dict[str, int] = defaultdict(int)
    by_source: Dict[str, int] = defaultdict(int)
    for p, b in chosen:
        by_bucket[b] += 1
        by_source[p["source"]] += 1
    print("\n== Golden set sampled (0 LLM calls) ==")
    print(f"  posts      : {len(rows)}  {dict(by_bucket)}")
    print(f"  by source  : {dict(by_source)}")
    print(f"  template   : {paths['template']}")
    print(f"  label this : {paths['human']}")
    print(f"  guide      : {paths['guide']}")
    print("Label all rows blind before running the full enrichment.")
    return 0
