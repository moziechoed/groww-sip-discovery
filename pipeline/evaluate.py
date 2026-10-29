"""Stage 6: Evaluate (PRD §9.6, FR6.2–FR6.5). Zero LLM calls.

    python run.py --stage evaluate

Compares the engine's labels for the 20 golden posts with the PM's blind labels.

Reads
- data/enriched/enriched_posts.jsonl   engine labels (final labels: downgraded labels count as the engine's answer)
- data/seeds/golden_human.csv          PM labels (must be complete, see pipeline.golden.labels_status)
- data/seeds/failure_reasons.csv       optional: likely reason per disagreement (drafted, then PM-reviewed)

Writes
- outputs/golden_engine.csv            engine labels in the same columns as golden_human.csv (FR6.2)
- outputs/eval_report.json             per-field / overall / by-bucket agreement, weakest-field confusion (FR6.4)
- outputs/eval_failures.csv            one row per disagreement: post, field, engine, human, likely reason (FR6.5)

Agreement rules
- is_relevant is compared on all 20 rows.
- The other required fields are compared on rows the PM marked relevant. If the engine called such a row
  irrelevant, its fields are not_applicable/unknown and count as disagreements.
- stop_scenario_ids: agreement = any shared id, or both empty ("none"). Exact-set match is reported too.
- Engine seeded_category_primary null == "none".
- Overall = all compared (row, field) pairs pooled. Optional fields are reported but not pooled.
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.config import Settings
from core.logger import get_logger
from pipeline.golden import CSV_COLUMNS, OPTIONAL_LABELS, REQUIRED_LABELS, labels_status, read_golden_rows

log = get_logger("evaluate")

AC9_OVERALL, AC9_OUTCOME = 0.70, 0.75
DOWNSTREAM = [f for f in REQUIRED_LABELS if f != "is_relevant"]
FAILURE_COLUMNS = ["post_id", "bucket", "source", "field", "engine", "human", "likely_reason", "reason_source"]


def _paths(settings: Settings) -> Dict[str, Path]:
    out = settings.paths.resolve("outputs")
    seeds = settings.paths.resolve("seeds_dir")
    return {
        "enriched": settings.paths.resolve("enriched_dir") / "enriched_posts.jsonl",
        "engine_csv": out / "golden_engine.csv",
        "report": out / "eval_report.json",
        "failures": out / "eval_failures.csv",
        "reasons": seeds / "failure_reasons.csv",
    }


# --------------------------------------------------------------------------- normalising


def _ids_to_str(ids) -> str:
    ids = [int(x) for x in (ids or [])]
    return ";".join(str(i) for i in ids) if ids else "none"


def _str_to_ids(s: Optional[str]) -> List[int]:
    s = (s or "").strip().lower()
    if s in ("", "none", "[]"):
        return []
    return [int(x) for x in s.replace(",", ";").split(";") if x.strip()]


def engine_labels(post: dict) -> Dict[str, str]:
    """Engine labels for one enriched post, as strings in golden_human.csv format."""
    e = post.get("enrichment") or {}
    rel = bool(e.get("is_relevant")) and post.get("status") != "enrichment_failed"
    out = {"is_relevant": "true" if rel else "false"}
    if not rel:
        return {**out, **{f: "" for f in DOWNSTREAM + OPTIONAL_LABELS}}
    out.update({
        "outcome": e.get("outcome") or "unknown",
        "kpi_branch": e.get("kpi_branch") or "not_applicable",
        "stop_scenario_ids": _ids_to_str(e.get("stop_scenario_ids")),
        "trigger_type": e.get("trigger_type") or "unknown",
        "seeded_category_primary": e.get("seeded_category_primary") or "none",
        "journey_stage": e.get("journey_stage") or "unknown",
        "outside_app_behaviour": e.get("outside_app_behaviour") or "unknown",
        "segment_confidence": e.get("segment_confidence") or "",
    })
    return out


def field_agrees(field: str, engine: str, human: str) -> Tuple[bool, Optional[bool]]:
    """(agrees, exact) — exact is only meaningful for stop_scenario_ids."""
    engine, human = (engine or "").strip().lower(), (human or "").strip().lower()
    if field == "stop_scenario_ids":
        e, h = set(_str_to_ids(engine)), set(_str_to_ids(human))
        exact = e == h
        return (bool(e & h) or (not e and not h)), exact
    if field == "seeded_category_primary":
        engine = engine or "none"
        human = human or "none"
    return engine == human, None


# --------------------------------------------------------------------------- core


def _pct(a: int, n: int) -> Optional[float]:
    return round(100 * a / n, 1) if n else None


def compare(human_rows: List[dict], engine_by_id: Dict[str, Dict[str, str]],
            reasons: Optional[Dict[Tuple[str, str], Tuple[str, str]]] = None) -> Tuple[dict, List[dict]]:
    reasons = reasons or {}
    agree, n = Counter(), Counter()
    by_bucket = defaultdict(lambda: [0, 0])
    stop_exact = [0, 0]
    confusion = defaultdict(Counter)
    failures: List[dict] = []
    opt_agree, opt_n = Counter(), Counter()
    missing = []

    for h in human_rows:
        pid = h["post_id"]
        eng = engine_by_id.get(pid)
        if eng is None:
            missing.append(pid)
            continue
        bucket = h.get("bucket", "")
        fields = ["is_relevant"] + (DOWNSTREAM if h["is_relevant"].strip().lower() == "true" else [])
        for f in fields:
            ok, exact = field_agrees(f, eng.get(f, ""), h.get(f, ""))
            n[f] += 1
            agree[f] += ok
            by_bucket[bucket][0] += ok
            by_bucket[bucket][1] += 1
            confusion[f][(eng.get(f, ""), h.get(f, "").strip().lower())] += 1
            if f == "stop_scenario_ids":
                stop_exact[0] += bool(exact)
                stop_exact[1] += 1
            if not ok:
                reason, src = reasons.get((pid, f), ("", ""))
                failures.append({"post_id": pid, "bucket": bucket, "source": h.get("source", ""), "field": f,
                                 "engine": eng.get(f, ""), "human": h.get(f, ""), "likely_reason": reason,
                                 "reason_source": src})
        if h["is_relevant"].strip().lower() == "true" and eng.get("is_relevant") == "true":
            for f in OPTIONAL_LABELS:
                hv = (h.get(f) or "").strip().lower()
                if hv:
                    opt_n[f] += 1
                    opt_agree[f] += hv == (eng.get(f) or "").strip().lower()

    # Post-hoc reading (28 Oct): "event relevance" = engine relevant AND a KPI branch other than not_applicable,
    # i.e. the post describes a SIP change or failure. Reported alongside, never instead of, is_relevant.
    ev_agree = 0
    ev_n = 0
    for h in human_rows:
        eng = engine_by_id.get(h["post_id"])
        if eng is None:
            continue
        event = eng.get("is_relevant") == "true" and eng.get("kpi_branch") not in ("", "not_applicable")
        ev_n += 1
        ev_agree += event == (h["is_relevant"].strip().lower() == "true")

    total_a, total_n = sum(agree.values()), sum(n.values())
    per_field = {f: {"agree": agree[f], "n": n[f], "pct": _pct(agree[f], n[f])} for f in REQUIRED_LABELS}
    compared = [f for f in REQUIRED_LABELS if n[f]]
    weakest = min(compared, key=lambda f: (agree[f] / n[f], f)) if compared else None
    overall = total_a / total_n if total_n else 0.0
    outcome_rate = agree["outcome"] / n["outcome"] if n["outcome"] else 0.0
    rel_rows = [h for h in human_rows if h["is_relevant"].strip().lower() == "true"]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_golden": len(human_rows),
        "n_human_relevant": len(rel_rows),
        "n_engine_relevant": sum(1 for h in human_rows if engine_by_id.get(h["post_id"], {}).get("is_relevant") == "true"),
        "missing_from_enriched": missing,
        "per_field": per_field,
        "overall": {"agree": total_a, "n": total_n, "pct": _pct(total_a, total_n)},
        "overall_excluding_is_relevant": {
            "agree": total_a - agree["is_relevant"], "n": total_n - n["is_relevant"],
            "pct": _pct(total_a - agree["is_relevant"], total_n - n["is_relevant"])},
        "event_relevance_vs_human_relevance": {
            "agree": ev_agree, "n": ev_n, "pct": _pct(ev_agree, ev_n),
            "note": "Engine 'event' posts (relevant + KPI branch set) vs PM is_relevant. Defined after seeing the "
                    "results, on the same 20 posts, so treat as a hypothesis, not a validated rate."},
        "stop_scenario_exact_set": {"agree": stop_exact[0], "n": stop_exact[1], "pct": _pct(*stop_exact)},
        "by_bucket": {b: {"agree": a, "n": m, "pct": _pct(a, m)} for b, (a, m) in sorted(by_bucket.items())},
        "optional_fields": {f: {"agree": opt_agree[f], "n": opt_n[f], "pct": _pct(opt_agree[f], opt_n[f])}
                            for f in OPTIONAL_LABELS},
        "weakest_field": weakest,
        "weakest_field_confusion": [
            {"engine": e, "human": hv, "count": c}
            for (e, hv), c in confusion[weakest].most_common()] if weakest else [],
        "relevance_confusion": {
            "engine_true_human_true": confusion["is_relevant"][("true", "true")],
            "engine_true_human_false": confusion["is_relevant"][("true", "false")],
            "engine_false_human_true": confusion["is_relevant"][("false", "true")],
            "engine_false_human_false": confusion["is_relevant"][("false", "false")],
        },
        "ac9": {
            "overall_target_pct": AC9_OVERALL * 100, "outcome_target_pct": AC9_OUTCOME * 100,
            "overall_pass": overall >= AC9_OVERALL, "outcome_pass": outcome_rate >= AC9_OUTCOME,
        },
        "notes": [
            "Downstream fields are compared only on rows the PM marked relevant; sample sizes are small, "
            "so every rate has a wide margin of error.",
            "Failure reasons are drafted (see reason_source) and must be reviewed by the PM.",
        ],
    }
    return report, failures


def _read_reasons(path: Path) -> Dict[Tuple[str, str], Tuple[str, str]]:
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {(r["post_id"], r["field"]): (r.get("likely_reason", ""), r.get("reason_source", ""))
                for r in csv.DictReader(f)}


def _write_csv(path: Path, columns: List[str], rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})


def run_evaluate(settings: Settings) -> int:
    p = _paths(settings)
    complete, missing, total = labels_status(settings)
    if not complete:
        print(f"Golden labels incomplete ({missing} of {total} rows missing labels). Finish data/seeds/golden_human.csv first.")
        return 2
    if not p["enriched"].exists():
        print("No enriched posts yet. Run --stage enrich first.")
        return 2
    human = read_golden_rows(settings)
    ids = {h["post_id"] for h in human}
    engine: Dict[str, Dict[str, str]] = {}
    meta: Dict[str, dict] = {}
    with open(p["enriched"], encoding="utf-8") as f:
        for line in f:
            post = json.loads(line)
            if post["id"] in ids:
                engine[post["id"]] = engine_labels(post)
                meta[post["id"]] = post
    report, failures = compare(human, engine, _read_reasons(p["reasons"]))

    engine_rows = []
    for h in human:
        e = engine.get(h["post_id"], {})
        post = meta.get(h["post_id"], {})
        engine_rows.append({**{c: h.get(c, "") for c in ["post_id", "bucket", "source", "text"]}, **e,
                            "notes": f"status={post.get('status', 'missing')}"})
    _write_csv(p["engine_csv"], CSV_COLUMNS, engine_rows)
    _write_csv(p["failures"], FAILURE_COLUMNS, failures)
    p["report"].write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n== Stage 6: Evaluate (golden set) ==")
    print(f"Golden posts: {report['n_golden']}   PM relevant: {report['n_human_relevant']}   "
          f"engine relevant: {report['n_engine_relevant']}")
    for f, v in report["per_field"].items():
        print(f"  {f:<26}: {v['agree']}/{v['n']}  ({v['pct']}%)")
    se = report["stop_scenario_exact_set"]
    print(f"  {'stop_scenario_ids (exact)':<26}: {se['agree']}/{se['n']}  ({se['pct']}%)")
    ev = report["event_relevance_vs_human_relevance"]
    print(f"  {'event relevance (post-hoc)':<26}: {ev['agree']}/{ev['n']}  ({ev['pct']}%)")
    o = report["overall"]
    print(f"Overall: {o['agree']}/{o['n']} ({o['pct']}%)   by bucket: "
          + ", ".join(f"{b} {v['pct']}% (n={v['n']})" for b, v in report["by_bucket"].items()))
    ac = report["ac9"]
    print(f"AC9 overall ≥70%: {'PASS' if ac['overall_pass'] else 'FAIL'}   outcome ≥75%: "
          f"{'PASS' if ac['outcome_pass'] else 'FAIL'}")
    print(f"Weakest field: {report['weakest_field']}   disagreements written: {len(failures)}")
    unexplained = sum(1 for r in failures if not r["likely_reason"])
    if unexplained:
        print(f"{unexplained} disagreements have no likely_reason yet (data/seeds/failure_reasons.csv).")
    print(f"Wrote {p['engine_csv'].name}, {p['report'].name}, {p['failures'].name} in outputs/")
    return 0
