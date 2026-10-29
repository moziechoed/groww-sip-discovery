"""CLI entry point.

    python run.py --stage preflight    # SDK check + models.list (free metadata call, still gated)
    python run.py --stage test-call    # ONE tiny Gemini call to verify model ID, thinking control, cost math
    python run.py --stage collect [--source play_store|app_store|reddit|all] [--limit N] [--subs s1,s2]   # P1–P2, no LLM calls
    python run.py --stage clean                  # P6, rules only
    python run.py --stage golden-sample          # P7: pick 20 golden posts to label blind (0 LLM calls)
    python run.py --stage enrich --limit 20      # P7: paid 20-post test (excludes golden posts)
    python run.py --stage enrich --limit 20 --redo-test   # same 20 posts again after a prompt change
    python run.py --stage enrich                 # P7: paid full run (needs the test + golden labels)
    python run.py --stage evaluate               # P9: golden-set agreement report (0 LLM calls)
    python run.py --stage analyse                # P8: 4 theme-clustering calls (gated), then pandas stats
    python run.py --stage surface                # P8: 1 call for card text + actionability (gated), scoring

Approval gate (Guardrails §2/§5, Architecture §4.11): before any Gemini call, an estimate is printed
and the run blocks until the PM types `yes`. Any other input exits cleanly. There is no bypass flag.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Optional

from core.budget import BudgetError, BudgetTracker, compute_cost_inr, estimate_single_call, estimate_tokens, format_estimate
from core.config import ROOT, Settings, get_api_key, load_config
from core.llm_client import LLMClient, LLMProvider, LLMUnavailable, thinking_kwargs
from core.logger import get_logger
from core.schemas import SmokeTestResult

log = get_logger("run")

LLM_STAGES = ["enrich", "analyse", "surface", "evaluate"]
ALL_STAGES = ["collect", "clean"] + LLM_STAGES
PLANNED_PHASE = {"collect": "P1–P5", "clean": "P6", "enrich": "P7", "analyse": "P8", "surface": "P8", "evaluate": "P9"}

SMOKE_POST_ID = "smoke-1"
SMOKE_POST = ("Started my first SIP of 3k on Groww last year. Paused it in month 4 after my rent went up, "
              "restarted in January once things settled.")


def approval_gate(input_fn: Callable[[str], str] = input) -> bool:
    try:
        answer = input_fn("Proceed? Type 'yes' to continue, anything else to exit: ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() == "yes":
        return True
    print("Not approved. Exiting cleanly — no Gemini calls made.")
    return False


def _tracker(settings: Settings, stage: str) -> BudgetTracker:
    return BudgetTracker(settings.budget, settings.paths.resolve("spend_log"), stage=stage,
                         model=settings.llm.model, prompt_version=settings.run.prompt_version)


def _need_key(provider: Optional[LLMProvider]) -> Optional[str]:
    """Returns the API key, or None if missing (prints guidance). Tests inject a provider and skip this."""
    if provider is not None:
        return "injected"
    key = get_api_key()
    if not key:
        print("\nGEMINI_API_KEY not found. Copy .env.example to .env, add your key, then re-run.")
    return key


# --------------------------------------------------------------------------- preflight


def sdk_info(settings: Settings) -> None:
    try:
        import google.genai as genai
        from google.genai import types
    except ImportError:
        print("google-genai is not installed. Run: pip install -r requirements.txt")
        return
    fields = sorted(types.ThinkingConfig.model_fields.keys())
    print(f"google-genai version : {getattr(genai, '__version__', 'unknown')}")
    print(f"ThinkingConfig fields: {', '.join(fields)}")
    param = settings.llm.thinking_param
    print(f"Configured control   : {param} -> {thinking_kwargs(settings)}  "
          f"[{'supported by SDK' if param in fields else 'NOT in this SDK version'}]")


def cmd_preflight(settings: Settings, input_fn=input, provider: Optional[LLMProvider] = None) -> int:
    print("\n== P0 preflight ==")
    sdk_info(settings)
    tracker = _tracker(settings, "preflight")
    tracker.check_can_start()
    est = estimate_single_call("preflight (models.list)", 0, 0, 0, settings.budget,
                               note="metadata call, no tokens billed")
    print(format_estimate(est, tracker))
    key = _need_key(provider)
    if not key or not approval_gate(input_fn):
        return 0 if key else 2
    client = LLMClient(settings, tracker, provider=provider, api_key=None if provider else key)
    try:
        names = client.list_models()
    except Exception as e:
        print(f"models.list failed: {e}")
        return 1
    wanted = f"models/{settings.llm.model}"
    flash = sorted(n for n in names if "flash" in n)
    print(f"\nFlash models visible to this key ({len(flash)}):")
    for n in flash:
        print(f"  {'*' if n == wanted else ' '} {n}")
    if wanted in names:
        print(f"\nOK: configured model '{settings.llm.model}' found.")
        return 0
    print(f"\nMISMATCH: '{wanted}' not found. Set llm.model in config.yaml to the exact ID above.")
    return 1


# --------------------------------------------------------------------------- test call


def smoke_prompt(settings: Settings) -> tuple:
    system = (settings.paths.resolve("prompts") / "smoke_test_v1.txt").read_text(encoding="utf-8")
    prompt = f'post_id: {SMOKE_POST_ID}\npost: """{SMOKE_POST}"""'
    return system, prompt


def cmd_test_call(settings: Settings, input_fn=input, provider: Optional[LLMProvider] = None) -> int:
    print("\n== P0 test call (1 Gemini call) ==")
    tracker = _tracker(settings, "test_call")
    tracker.check_can_start()
    system, prompt = smoke_prompt(settings)
    max_out = settings.llm.max_output_tokens["test_call"]
    schema_overhead = 60  # response schema sent alongside the prompt
    in_tok = estimate_tokens(system) + estimate_tokens(prompt) + schema_overhead
    expected_out = 60
    est = estimate_single_call("test-call", in_tok, expected_out, max_out, settings.budget,
                               note=f"model {settings.llm.model}, thinking {thinking_kwargs(settings)}")
    print(format_estimate(est, tracker))
    key = _need_key(provider)
    if not key or not approval_gate(input_fn):
        return 0 if key else 2

    client = LLMClient(settings, tracker, provider=provider, api_key=None if provider else key)
    try:
        result = client.generate_json(prompt=prompt, system=system, schema=SmokeTestResult,
                                      batch_id="test-call-1", max_output_tokens=max_out,
                                      expected_cost_inr=est.cost_inr)
    except LLMUnavailable as e:
        print(f"\nCall failed (HTTP {e.code or 'unknown'}):")
        if e.error_field:
            print(f"  error_field  : {e.error_field}")
        print(f"  error_message: {e.error_message}")
        return 1
    except BudgetError as e:
        print(f"\nBudget stop: {e}")
        return 1

    u = result.usage
    actual = compute_cost_inr(u, settings.budget)
    print("\n== Result ==")
    print(f"status          : {result.status} (attempts: {result.attempts})")
    print(f"parsed          : {result.data.model_dump() if result.data is not None else result.raw_text}")
    print(f"tokens          : input {u.input_tokens}, output {u.output_tokens}, "
          f"thinking {u.thinking_tokens}, cached {u.cached_tokens}")
    print(f"cost            : actual ₹{actual:.4f} vs estimate ₹{est.cost_inr:.4f} "
          f"(worst case ₹{est.worst_case_cost_inr:.4f})")
    ac15 = u.thinking_tokens <= max(5, 0.10 * u.output_tokens)
    print(f"AC15 thinking ~0: {'PASS' if ac15 else 'FAIL — thinking control not effective; stop and review'}")
    if result.data is not None and result.data.evidence_span:
        print(f"evidence verbatim: {'yes' if result.data.evidence_span in SMOKE_POST else 'NO'}")
    print(f"project spend   : ₹{tracker.project_spend:.4f} of ₹{settings.budget.project_cap_inr:.0f}")
    return 0 if result.status == "ok" and ac15 else 1


# --------------------------------------------------------------------------- main


def _positive_int(value: str) -> int:
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("--limit must be a positive integer")
    return n


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Groww SIP Continuation Discovery Engine")
    parser.add_argument("--stage", required=True,
                        choices=["preflight", "test-call", "context", "golden-sample"] + ALL_STAGES + ["all"])
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--source", default="all",
                        choices=["all", "play_store", "app_store", "reddit", "youtube", "tradingqna"],
                        help="collect only: which source to run (default: all enabled P1–P5 sources)")
    parser.add_argument("--limit", type=_positive_int, default=None,
                        help="collect: capped test run (max N reviews per store, max N HTTP requests for reddit, "
                             "max N videos for youtube, or max N topics for tradingqna), written to data/raw/test_run/. "
                             "enrich: N-post paid test run (use 20).")
    parser.add_argument("--subs", default=None,
                        help="collect --source reddit only: comma-separated subreddit subset (e.g. IndiaInvestments,MutualfundsIndia)")
    parser.add_argument("--channels", default=None,
                        help="collect --source youtube only: comma-separated channel handles (e.g. Groww,ETMONEY)")
    parser.add_argument("--redo-test", action="store_true",
                        help="enrich --limit only: re-run the previous test posts with the current prompt and "
                             "print a before/after comparison (previous outputs are archived)")
    parser.add_argument("--event-sample", action="store_true",
                        help="enrich --limit only: test on posts with first-hand stop/pause/failure/continue wording")
    parser.add_argument("--queries", default=None,
                        help="collect --source tradingqna only: comma-separated search queries (e.g. 'SIP stopped,SIP mandate')")
    args = parser.parse_args(argv)
    settings = load_config(args.config)

    try:
        if args.stage == "preflight":
            return cmd_preflight(settings)
        if args.stage == "test-call":
            return cmd_test_call(settings)
    except BudgetError as e:
        print(f"Budget stop: {e}")
        return 1

    if args.stage == "context":
        from collectors.context_data import run_context

        return run_context(settings)

    if args.stage == "golden-sample":
        from pipeline.golden import run_golden_sample

        return run_golden_sample(settings)

    stages = ALL_STAGES if args.stage == "all" else [args.stage]
    rc = 0
    for s in stages:
        if s == "collect":
            from collectors.runner import run_collect

            sources = None if args.source == "all" else [args.source]
            ckw = {}
            if args.subs:
                sub_list = [x.strip() for x in args.subs.split(",") if x.strip()]
                if sub_list:
                    ckw["reddit"] = {"subs_override": sub_list}
            if args.channels:
                ch_list = [x.strip() for x in args.channels.split(",") if x.strip()]
                if ch_list:
                    ckw["youtube"] = {"channels_override": ch_list}
            if args.queries:
                q_list = [x.strip() for x in args.queries.split(",") if x.strip()]
                if q_list:
                    ckw["tradingqna"] = {"queries_override": q_list}
            rc = max(rc, run_collect(settings, sources=sources, limit=args.limit, collector_kwargs=ckw or None))
            continue
        if s == "clean":
            from pipeline.clean import run_clean

            rc = max(rc, run_clean(settings))
            continue
        if s == "enrich":
            from pipeline.enrich import run_enrich

            key = _need_key(None)
            if not key:
                rc = max(rc, 2)
                break
            try:
                rc = max(rc, run_enrich(settings, limit=args.limit, approve=approval_gate, api_key=key,
                                        redo_test=args.redo_test, event_sample=args.event_sample))
            except BudgetError as e:
                print(f"Budget stop: {e}")
                rc = max(rc, 1)
            if rc:
                break
            continue
        if s in ("analyse", "surface"):
            if s == "analyse":
                from pipeline.analyse import run_analyse as runner
            else:
                from pipeline.surface import run_surface as runner
            key = get_api_key()  # only needed if a Gemini call is due; cached results need no key
            try:
                rc = max(rc, runner(settings, approve=approval_gate, api_key=key))
            except BudgetError as e:
                print(f"Budget stop: {e}")
                rc = max(rc, 1)
            if rc:
                break
            continue
        if s == "evaluate":
            from pipeline.evaluate import run_evaluate

            rc = max(rc, run_evaluate(settings))
            continue
        print(f"Stage '{s}' is not built yet (planned for {PLANNED_PHASE[s]}).")
    return rc


if __name__ == "__main__":
    sys.exit(main())
