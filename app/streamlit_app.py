"""Groww SIP Continuation — Discovery Engine (public demo). PRD §10.

Reads precomputed files in outputs/ (UI5). The only live Gemini call is the Live Analysis tab
(3 per session, 50 per day, ≤2,000 characters; UI4/UI7). Raw and clean post text is never deployed.

    streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.demo_limits import DailyDemoCounter  # noqa: E402

OUT = ROOT / "outputs"
SEEDS = ROOT / "data" / "seeds"
FOOTER = "Research tool built on public data. Not investment advice."
BAR = "#2a78d6"  # one hue; every bar is also labelled with its value (never colour alone)
STAGE_ORDER = ["pre_start", "month_0_1", "month_2_3", "month_4_6", "month_7_12", "month_12_plus", "post_stop"]
STAGE_LABEL = {"pre_start": "Before starting", "month_0_1": "Month 0–1", "month_2_3": "Months 2–3",
               "month_4_6": "Months 4–6", "month_7_12": "Months 7–12", "month_12_plus": "12+ months",
               "post_stop": "After stopping", "unknown": "Not stated"}
SOURCE_LABEL = {"reddit": "Reddit", "play_store": "Play Store", "youtube": "YouTube", "tradingqna": "TradingQ&A",
                "app_store": "App Store"}

st.set_page_config(page_title="SIP Continuation Discovery Engine", page_icon="📈", layout="wide")


# --------------------------------------------------------------------------- data


@st.cache_data
def load_json(name: str):
    p = OUT / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


@st.cache_data
def load_csv(path: str):
    p = Path(path)
    return pd.read_csv(p, dtype=str, keep_default_na=False) if p.exists() else None


A = load_json("analysis.json")
O = load_json("opportunities.json")
E = load_json("eval_report.json")
CS = load_json("corpus_stats.json") or {}
POSTS = load_csv(str(OUT / "posts_public.csv"))
FAIL = load_csv(str(OUT / "eval_failures.csv"))
G_H = load_csv(str(SEEDS / "golden_human.csv"))
G_E = load_csv(str(OUT / "golden_engine.csv"))
SPEND = load_csv(str(OUT / "spend_log.csv"))


def hbar(rows, label: str, value: str, title: str = "", pct: str | None = None, height_per=26, tooltip=None):
    """Horizontal bar chart, one hue, value labels at the bar end, hover tooltip."""
    df = pd.DataFrame(rows)
    if df.empty:
        st.caption("No data.")
        return
    shown = pct or value
    tip = tooltip or [label, value] + ([pct] if pct else [])
    base = alt.Chart(df).encode(y=alt.Y(f"{label}:N", sort=None, title=None, axis=alt.Axis(labelLimit=260)))
    bars = base.mark_bar(color=BAR, cornerRadiusEnd=4, height={"band": 0.7}).encode(
        x=alt.X(f"{shown}:Q", title=None, axis=alt.Axis(grid=True, tickCount=4)), tooltip=tip)
    text = base.mark_text(align="left", dx=4).encode(x=f"{shown}:Q", text=alt.Text(f"{shown}:Q"))
    chart = (bars + text).properties(height=max(80, height_per * len(df)), title=title)
    st.altair_chart(chart, width="stretch")


def footer():
    st.divider()
    st.caption(FOOTER)


def need(*objs) -> bool:
    if any(o is None for o in objs):
        st.info("This section appears once the pipeline outputs are generated.")
        return False
    return True


# --------------------------------------------------------------------------- header

st.title("Why do first-time investors stop their SIPs?")
st.caption("A discovery engine for Groww: 12-month SIP continuation of first-time mutual fund investors "
           "(salaried, 22–28, tier-1 cities, first 4 years of work).")

tabs = st.tabs(["Overview", "Opportunities", "Insights", "Evaluation", "Live Analysis", "Method"])

# --------------------------------------------------------------------------- Overview

with tabs[0]:
    st.markdown(
        "- **Collects** public posts about SIPs from Reddit, YouTube comments, the Play Store, the App Store and "
        "TradingQ&A.\n"
        "- **Tags** each post with an LLM (Gemini Flash) against the KPI tree: did the SIP continue, stop or fail, why, "
        "and when. Every tag needs a verbatim quote from the post.\n"
        "- **Ranks** the stop reasons into opportunity cards and checks the tagging against 20 hand-labelled posts.")
    if need(A):
        pop = A["populations"]
        c = st.columns(4)
        c[0].metric("Posts collected", f"{CS.get('collect', {}).get('raw_posts_total', 0):,}")
        c[1].metric("After cleaning", f"{pop['total_posts']:,}")
        c[2].metric("Relevant to SIPs", f"{pop['relevant']:,}")
        c[3].metric("State a SIP change", f"{pop['event']:,}")

        st.subheader("Corpus by source")
        cl = CS.get("clean", {})
        src = sorted(cl.get("per_source_out", {}).items(), key=lambda kv: -kv[1])
        st.dataframe(pd.DataFrame([{
            "Source": SOURCE_LABEL.get(s, s), "Collected": cl.get("per_source_in", {}).get(s, 0), "After cleaning": n,
            "Relevant": pop["relevant_by_source"].get(s, 0), "State a SIP change": pop["event_by_source"].get(s, 0)}
            for s, n in src]), hide_index=True, width="stretch")
        if cl.get("app_store_shortfall_note"):
            st.caption("App Store shortfall: " + cl["app_store_shortfall_note"])
        st.caption("Reddit was capped at 40% of the cleaned corpus and down-sampled evenly by month and subreddit; "
                   "headline shares are also shown source-balanced.")

        st.subheader("What was dropped while cleaning")
        drops = {}
        for srcname, reasons in cl.get("dropped_by_source_and_reason", {}).items():
            for r, n in reasons.items():
                drops[r] = drops.get(r, 0) + n
        hbar([{"Reason": k.replace("_", " "), "Posts": v} for k, v in sorted(drops.items(), key=lambda kv: -kv[1])],
             "Reason", "Posts")

        st.subheader("Segment coverage")
        sc = A["q8_segments"]["segment_confidence"]
        st.write("How many relevant posts state enough about the writer (age, job, city, income) to place them in a "
                 "segment. Most public posts don't, so segment cuts carry low sample sizes.")
        hbar([{"Segment confidence": v["value"], "Posts": v["n"], "Share %": v["pct"]} for v in sc["values"]],
             "Segment confidence", "Posts", pct="Share %")
        st.caption(f"Last pipeline run: {A['generated_at'][:10]}.")
    footer()

# --------------------------------------------------------------------------- Opportunities

with tabs[1]:
    if need(O):
        st.write("Each card is one stop reason from the KPI tree, ranked by **Frequency × Segment fit × "
                 "Behavioural specificity × Actionability**. Cards need at least 15 posts; smaller ones are listed "
                 "under Emerging signals.")
        cards = O["cards"]
        branches = sorted({b for c in cards for b in c["kpi_branches"]})
        stages = [s for s in STAGE_ORDER if any(s in c["journey_stages"] for c in cards)]
        f1, f2, f3 = st.columns(3)
        sel_stage = f1.multiselect("Journey stage", stages, format_func=lambda s: STAGE_LABEL.get(s, s))
        sel_branch = f2.multiselect("KPI branch", branches)
        groww_only = f3.toggle("Groww-tagged posts only", value=False)

        def matches(c):
            if sel_stage and not any(s in c["journey_stages"] for s in sel_stage):
                return False
            if sel_branch and not any(b in c["kpi_branches"] for b in sel_branch):
                return False
            return True

        shown = [c for c in cards if matches(c)]
        if not shown:
            st.info("No cards match these filters.")
        for c in shown:
            n_f = c["n"]
            if POSTS is not None and (sel_stage or groww_only):
                p = POSTS[(POSTS["primary_scenario"] == str(c["scenario_id"])) & (POSTS["event"] == "True")]
                if sel_stage:
                    p = p[p["journey_stage"].isin(sel_stage)]
                if groww_only:
                    p = p[p["platform_tag"] == "groww"]
                n_f = len(p)
            label = f"#{c['rank']}  {c['title']}  ·  score {c['score_display']}  ·  {c['n']} posts"
            with st.expander(label, expanded=c["rank"] <= 2):
                st.markdown(f"**{c['description']}**" if c["description"] else "")
                st.caption(f"Stop scenario {c['scenario_id']}: {c['scenario']}"
                           + (f"  ·  {n_f} posts match the filters" if (sel_stage or groww_only) else ""))
                m = st.columns(4)
                m[0].metric("Posts", c["n"], help="Stop-event posts whose main reason is this scenario")
                m[1].metric("Share of stop events", f"{c['pct_of_stop_events']}%")
                m[2].metric("Actionability", f"{c['actionability']} / 3" if c["actionability"] else "–")
                m[3].metric("Groww-tagged", c["groww_tagged"])
                if c.get("actionability_rationale"):
                    st.write("**Why this actionability:** " + c["actionability_rationale"])
                fx = c["factors"]
                st.dataframe(pd.DataFrame([
                    {"Factor": "Frequency", "Value (0–1)": fx["frequency"], "How": "posts ÷ largest card"},
                    {"Factor": "Segment fit", "Value (0–1)": fx["segment_fit"], "How": "mean post-level fit to the target segment"},
                    {"Factor": "Behavioural specificity", "Value (0–1)": fx["behavioural_specificity"],
                     "How": "verified quote + stated checking behaviour, banded 1–3"},
                    {"Factor": "Actionability", "Value (0–1)": fx["actionability"], "How": "LLM-rated 1–3 against written anchors"},
                ]), hide_index=True, width="stretch")
                st.markdown("**What investors said** (verbatim)")
                for q in c["quotes"]:
                    st.markdown(f"> {q['quote']}  \n"
                                f"<small>{SOURCE_LABEL.get(q['source'], q['source'])} · {q['date']} · "
                                f"[source]({q['url']})</small>", unsafe_allow_html=True)
                d1, d2 = st.columns(2)
                with d1:
                    cv = c["continuer_vs_dropoff"]
                    st.markdown("**Outcome mix**")
                    st.write(f"Stopped, paused or reduced: {cv['stopped_paused_reduced']} · considering stopping: "
                             f"{cv['considering_stop']} · continued or restarted: {cv['continued_or_restarted']}")
                    st.markdown("**Journey stage** (where stated, "
                                f"{c['journey_stage_known_pct']}% of posts)")
                    st.write(", ".join(f"{STAGE_LABEL.get(k, k)}: {v}" for k, v in c["journey_stages"].items()) or "Not stated")
                with d2:
                    st.markdown("**Sources**")
                    st.write(", ".join(f"{SOURCE_LABEL.get(k, k)}: {v}" for k, v in c["source_distribution"].items()))
                    st.markdown("**KPI branch**")
                    st.write(", ".join(f"{k}: {v}" for k, v in c["kpi_branches"].items()))
                    st.markdown("**Segment coverage**")
                    st.write(f"{c['segment_coverage_pct']}% of posts state segment details")
                if c.get("top_unmet_need_themes"):
                    st.markdown("**Unmet needs in this card:** " + "; ".join(
                        f"{t['theme']} ({t['n']})" for t in c["top_unmet_need_themes"]))

        st.subheader("Emerging signals")
        st.caption("Stop reasons with fewer than 15 posts. Not scored.")
        em = O.get("emerging", [])
        disc = O.get("emerging_discovered_categories", [])
        if not em and not disc:
            st.write("No emerging signals yet")
        else:
            st.dataframe(pd.DataFrame([{"Stop reason": c["title"] if c.get("title") else c["scenario"],
                                        "Scenario": c["scenario_id"], "Posts": c["n"]} for c in em]),
                         hide_index=True, width="stretch")
            if disc:
                st.caption("New categories the engine proposed outside the 6 seeds: " +
                           ", ".join(f"{d['category'].replace('_', ' ')} ({d['n']})" for d in disc))
    footer()

# --------------------------------------------------------------------------- Insights

with tabs[2]:
    if need(A):
        low = A["label_confidence"]["low_confidence_fields"]
        if low:
            st.info("Labels with under 60% agreement on the golden set are shown for completeness but are not used "
                    "for headline claims: " + ", ".join(low) + ".")
        section = st.radio("Section", ["KPI tree sizing", "Q1 Why start", "Q2 Behaviour over time", "Q3 Stop triggers",
                                       "Q4 What they check", "Q5 In-app confusion", "Q6 Outside the app",
                                       "Q7 Continuers vs drop-offs", "Q8 Segments", "Q9 Unmet needs"],
                           horizontal=True, label_visibility="collapsed")

        def themes_block(ts, what):
            if not ts.get("clustered"):
                st.caption(f"{what}: {ts.get('n_items', 0)} items, not clustered yet.")
                return
            hbar([{"Theme": t["theme"], "Posts": t["n"], "Share %": t["pct_of_items"],
                   "Description": t["description"]} for t in ts["themes"]], "Theme", "Posts", pct="Share %",
                 tooltip=["Theme", "Posts", "Share %", "Description"])
            st.caption(f"{ts['n_items']} items grouped by the LLM; {ts.get('unclustered', 0)} left unclustered.")

        if section == "KPI tree sizing":
            ks = A["kpi_sizing"]
            st.subheader("Which branch of the KPI tree do posts show?")
            st.caption(f"Posts that state a SIP change (n = {ks['branches_among_events']['base_n']}). "
                       "Balanced = the average of each source's share, so Reddit's down-sampling doesn't tilt it.")
            hbar([{"Branch": v["label"], "Raw %": v["pct"], "Balanced %": v["balanced_pct"], "Posts": v["n"]}
                  for v in ks["branches_among_events"]["values"]], "Branch", "Balanced %",
                 tooltip=["Branch", "Posts", "Raw %", "Balanced %"])
            st.subheader("Main stop reason")
            st.caption(f"Stop events only (n = {ks['scenarios_primary']['base_n']}); each post counted once, by its "
                       "main reason.")
            hbar([{"Stop reason": f"{v['value']}. {v['label']}", "Raw %": v["pct"], "Balanced %": v["balanced_pct"],
                   "Posts": v["n"]} for v in ks["scenarios_primary"]["values"]], "Stop reason", "Balanced %",
                 tooltip=["Stop reason", "Posts", "Raw %", "Balanced %"])
            with st.expander("By source"):
                st.dataframe(pd.DataFrame([{"Stop reason": f"{v['value']}. {v['label']}",
                                            **{SOURCE_LABEL.get(k, k): p for k, p in v["by_source_pct"].items()}}
                                           for v in ks["scenarios_primary"]["values"]]), hide_index=True,
                             width="stretch")
        elif section == "Q1 Why start":
            q = A["q1_start"]
            st.subheader("Why people start, and what they expect")
            themes_block(q["start_reason_themes"], "Start reasons")
            themes_block(q["expectation_themes"], "Expectations")
            st.caption("Few public posts state a start reason or expectation; interviews cover this question.")
        elif section == "Q2 Behaviour over time":
            q = A["q2_behaviour_change"]
            st.subheader("When in the journey do SIP changes happen?")
            js = q["journey_stage_among_events"]
            st.caption(f"Timing is stated in {js['coverage_pct']}% of posts that describe a SIP change.")
            hbar([{"Stage": STAGE_LABEL.get(r["stage"], r["stage"]), "Posts": r["n"]} for r in q["stages"]],
                 "Stage", "Posts")
            st.dataframe(pd.DataFrame([{
                "Stage": STAGE_LABEL.get(r["stage"], r["stage"]), "Posts": r["n"],
                "Low sample": "yes" if r["low_sample"] else "",
                "Top stop reasons": "; ".join(f"{s['label']} ({s['n']})" for s in r["top_scenarios"]),
                "Top unmet needs": "; ".join(f"{t['theme']} ({t['n']})" for t in r["top_unmet_need_themes"])}
                for r in q["stages"]]), hide_index=True, width="stretch")
        elif section == "Q3 Stop triggers":
            q = A["q3_triggers"]
            st.subheader("What triggers a pause, reduction or stop?")
            st.caption(f"Posts that stopped, paused, reduced or are considering it (n = {q['n']}).")
            hbar([{"Stop reason": f"{v['value']}. {v['label']}", "Balanced %": v["balanced_pct"], "Posts": v["n"]}
                  for v in q["stop_scenarios"]["values"][:10]], "Stop reason", "Posts")
            with st.expander("Trigger type (low confidence)" if q["trigger_type_low_confidence"] else "Trigger type"):
                hbar([{"Trigger": v["value"], "Posts": v["n"]} for v in q["trigger_type"]["values"]], "Trigger", "Posts")
        elif section == "Q4 What they check":
            q = A["q4_checking"]
            st.subheader("What investors check, and how often")
            st.caption(f"Only {q['checking_what_coverage_pct']}% of relevant posts say what they check; frequency is "
                       "almost never stated. Interviews cover this question.")
            hbar([{"Checks": v["value"], "Posts": v["n"]} for v in q["checking_what_top"]], "Checks", "Posts")
        elif section == "Q5 In-app confusion":
            q = A["q5_in_app_confusion"]
            st.subheader("What confuses or worries investors inside the app?")
            themes_block(q["all"], "In-app confusion")
            st.caption(f"{q['groww_only_n']} of these posts are tagged as about Groww.")
        elif section == "Q6 Outside the app":
            q = A["q6_outside_app"]
            st.subheader("What do they do outside the app when unsure?")
            st.caption(f"Stated in {q['coverage_pct']}% of posts (the posts themselves are on Reddit, YouTube and "
                       "TradingQ&A, so 'asked a forum' is built in).")
            hbar([{"Behaviour": v["value"], "Posts": v["n"]} for v in q["values"] if v["value"] != "unknown"],
                 "Behaviour", "Posts")
        elif section == "Q7 Continuers vs drop-offs":
            q = A["q7_continuers_vs_dropoffs"]
            st.subheader("What separates continuers from drop-offs?")
            st.caption(f"Continuers n = {q['n']['continuer']}, drop-offs (stopped/paused/reduced) n = "
                       f"{q['n']['drop_off']}, considering stopping n = {q['n']['considering']}.")
            fld = st.selectbox("Compare by", list(q["fields"].keys()))
            rows = [r for r in q["fields"][fld] if r["value"] not in ("unknown", "none", "low")][:10] or q["fields"][fld][:10]
            long = []
            for r in rows:
                for g, name in (("continuer", "Continuers"), ("drop_off", "Drop-offs"), ("considering", "Considering")):
                    long.append({"Value": r["value"], "Group": name, "Share %": r[g]["pct"], "Posts": r[g]["n"]})
            df = pd.DataFrame(long)
            ch = alt.Chart(df).mark_bar(cornerRadiusEnd=3).encode(
                y=alt.Y("Value:N", title=None, sort=None, axis=alt.Axis(labelLimit=220)),
                yOffset=alt.YOffset("Group:N", sort=["Continuers", "Drop-offs", "Considering"]),
                x=alt.X("Share %:Q"),
                color=alt.Color("Group:N", sort=["Continuers", "Drop-offs", "Considering"],
                                scale=alt.Scale(range=["#2a78d6", "#eb6834", "#1baf7a"]),
                                legend=alt.Legend(orient="top", title=None)),
                tooltip=["Value", "Group", "Posts", "Share %"]).properties(height=max(120, 54 * len(rows)))
            st.altair_chart(ch, width="stretch")
            with st.expander("Table"):
                st.dataframe(df, hide_index=True, width="stretch")
            st.markdown("**Top unmet needs by group**")
            um = q["unmet_need_themes"]
            st.dataframe(pd.DataFrame({k: pd.Series([f"{t['theme']} ({t['n']})" for t in v]) for k, v in um.items()}),
                         hide_index=True, width="stretch")
        elif section == "Q8 Segments":
            q = A["q8_segments"]
            st.subheader("How do patterns differ by segment?")
            st.caption(q["note"] + " Most posts don't state segment details.")
            key = st.selectbox("Segment", list(q["cuts"].keys()))
            cut = q["cuts"][key]
            st.caption(f"Stated in {cut['coverage_pct']}% of labelled posts.")
            st.dataframe(pd.DataFrame([{"Value": v["value"], "Posts": v["n"], "Low sample": "yes" if v["low_sample"] else "",
                                        "Continuers %": v["continuer_pct"], "Drop-offs %": v["drop_off_pct"],
                                        "Considering %": v["considering_pct"],
                                        "Top stop reasons": "; ".join(f"{s['label']} ({s['n']})" for s in v["top_scenarios"])}
                                       for v in cut["values"]]), hide_index=True, width="stretch")
        elif section == "Q9 Unmet needs":
            st.subheader("What unmet needs come up again and again?")
            themes_block(A["q9_unmet_needs"]["themes"], "Unmet needs")
    footer()

# --------------------------------------------------------------------------- Evaluation

with tabs[3]:
    if need(E):
        st.write("20 posts were hand-labelled by the PM **before** seeing any engine output, then compared field by "
                 "field with the engine.")
        o, ac = E["overall"], E["ac9"]
        c = st.columns(3)
        c[0].metric("Overall agreement", f"{o['pct']}%", help=f"{o['agree']} of {o['n']} field comparisons")
        c[1].metric("Outcome agreement", f"{E['per_field']['outcome']['pct']}%")
        c[2].metric("Relevance agreement", f"{E['per_field']['is_relevant']['pct']}%")
        st.write(f"Targets (AC9): overall ≥ 70% — **{'pass' if ac['overall_pass'] else 'not met'}**; outcome ≥ 75% — "
                 f"**{'pass' if ac['outcome_pass'] else 'not met'}**.")
        st.subheader("Agreement by field")
        hbar([{"Field": f, "Agreement %": v["pct"], "Agree / n": f"{v['agree']} / {v['n']}"}
              for f, v in E["per_field"].items()], "Field", "Agreement %", tooltip=["Field", "Agree / n", "Agreement %"])
        st.caption("Fields after is_relevant are compared on the 8 posts the PM marked relevant, so each rate has a "
                   "wide margin of error. Stop scenarios agree when they share at least one id "
                   f"(exact match: {E['stop_scenario_exact_set']['pct']}%).")
        ev = E.get("event_relevance_vs_human_relevance")
        if ev:
            st.info(f"Post-hoc reading: posts the engine says **state a SIP change** match the PM's relevance call in "
                    f"{ev['agree']} of {ev['n']} posts ({ev['pct']}%). {ev['note']}")
        st.subheader("By bucket")
        st.dataframe(pd.DataFrame([{"Bucket": b, "Agreement %": v["pct"], "Comparisons": v["n"]}
                                   for b, v in E["by_bucket"].items()]), hide_index=True, width="stretch")
        st.subheader(f"Weakest field: {E['weakest_field']}")
        st.dataframe(pd.DataFrame(E["weakest_field_confusion"]).rename(
            columns={"engine": "Engine", "human": "PM", "count": "Posts"}), hide_index=True, width="stretch")
        if FAIL is not None and len(FAIL):
            st.subheader("Every disagreement, with its likely reason")
            st.dataframe(FAIL.rename(columns={"post_id": "Post", "field": "Field", "engine": "Engine", "human": "PM",
                                              "likely_reason": "Likely reason", "reason_source": "Reason by"})
                         [["Post", "Field", "Engine", "PM", "Likely reason", "Reason by"]],
                         hide_index=True, width="stretch")
        if G_H is not None and G_E is not None:
            with st.expander("Side by side: the 20 golden posts"):
                m = G_H.merge(G_E, on="post_id", suffixes=(" (PM)", " (engine)"))
                cols = ["post_id", "text (PM)"] + [f"{f} ({w})" for f in ["is_relevant", "outcome", "kpi_branch",
                                                                         "stop_scenario_ids"] for w in ("PM", "engine")]
                st.dataframe(m[cols].rename(columns={"text (PM)": "Post"}), hide_index=True, width="stretch")
    footer()

# --------------------------------------------------------------------------- Live Analysis

with tabs[4]:
    st.subheader("Try the tagger on one post")
    st.write("Paste a post about a SIP (English or Hinglish). The engine returns the same labels it used for the "
             "corpus, or says there isn't enough signal.")
    st.caption("Limits: 3 analyses per session, 50 per day for everyone, 2,000 characters. "
               "Research tool. Not investment advice.")

    @st.cache_resource
    def daily_counter():
        from core.config import load_config
        cfg = load_config()
        return DailyDemoCounter(Path("/tmp/sip_demo_daily.json"), daily_cap=cfg.live.daily_demo_cap)

    text = st.text_area("Post", max_chars=2000, height=140,
                        placeholder="e.g. Started a 2k SIP on Groww in March. Autopay failed twice in July because "
                                    "salary came late, so I just cancelled it.")
    if st.button("Analyse", type="primary"):
        from app.live import analyse_post, precheck
        from core.config import get_api_key, load_config

        cfg = load_config()
        pre = precheck(text, cfg.live.max_input_chars)
        if pre:
            (st.warning if pre.status == "declined_fund" else st.info)(pre.message)
        else:
            key = None
            try:
                key = st.secrets.get("GEMINI_API_KEY")
            except Exception:
                key = None
            key = key or get_api_key()
            if not key:
                st.error("Live analysis is not configured on this deployment.")
            else:
                ok, msg = daily_counter().check_and_increment(st.session_state, cfg.live.session_call_limit)
                if not ok:
                    st.warning(msg)
                else:
                    with st.spinner("Analysing…"):
                        try:
                            res = analyse_post(text, cfg, api_key=key)
                        except Exception as e:  # budget caps, 402, network
                            res = None
                            st.error("Live analysis is unavailable right now. Please try again later.")
                            print(f"live error: {type(e).__name__}: {e}")
                    if res is not None:
                        if res.status == "declined_fund":
                            st.warning(res.message)
                        elif res.status in ("no_signal", "error"):
                            st.info(res.message)
                            for n in res.notes:
                                st.caption(n)
                        else:
                            f = res.fields
                            seg = {k: v for k, v in (f.get("segment_signals") or {}).items() if v not in (None, "unknown")}
                            rows = [("Outcome", f["outcome"]), ("KPI branch", f["kpi_branch"]),
                                    ("Stop scenarios", ", ".join(str(i) for i in f["stop_scenario_ids"]) or "none"),
                                    ("Journey stage", STAGE_LABEL.get(f["journey_stage"], f["journey_stage"])),
                                    ("Trigger", f["trigger_type"]), ("Trigger detail", f.get("trigger_detail") or ""),
                                    ("Unmet need", f.get("unmet_need") or ""),
                                    ("In-app confusion", f.get("in_app_confusion") or ""),
                                    ("Platform", f["platform_tag"]), ("Segment signals", ", ".join(f"{k}: {v}" for k, v in seg.items())),
                                    ("Evidence (verbatim)", f.get("evidence_span") or ""),
                                    ("Confidence", f["enrichment_confidence"])]
                            st.dataframe(pd.DataFrame(rows, columns=["Label", "Value"]), hide_index=True,
                                         width="stretch")
                            if res.evidence_verified:
                                st.caption("✓ Evidence found word for word in the post.")
                            for n in res.notes:
                                st.caption(n)
    used = int(st.session_state.get("demo_calls_used", 0))
    st.caption(f"Analyses used this session: {used} of 3.")
    footer()

# --------------------------------------------------------------------------- Method

with tabs[5]:
    st.subheader("Pipeline")
    st.graphviz_chart("""
digraph { rankdir=LR; node [shape=box, style=rounded, fontname="Helvetica", fontsize=11];
  C [label="1 Collect\\nReddit · YouTube · Play Store\\nApp Store · TradingQ&A"];
  K [label="2 Clean\\nrules only: dedupe, language,\\nPII mask, Reddit ≤40%"];
  N [label="3 Enrich\\nGemini Flash, batch 30\\nverbatim evidence check"];
  A [label="4 Analyse\\npandas stats +\\nLLM theme grouping"];
  S [label="5 Surface\\nscored cards\\n(LLM actionability)"];
  V [label="6 Evaluate\\n20 hand-labelled posts"];
  C -> K -> N -> A -> S; N -> V; }""")
    st.subheader("Guardrails")
    st.markdown(
        "- Every outcome or trigger tag needs a quote copied word for word from the post; quotes that can't be "
        "found are dropped and the tag is set to unknown.\n"
        "- No fund or stock recommendations anywhere; card quotes that name a fund house or scheme are skipped.\n"
        "- No usernames or personal data: authors are hashed, emails/UPI/PAN/phone/account numbers are masked, and "
        "only short quotes are published.\n"
        "- Every paid run printed a cost estimate and waited for the PM to type `yes`; a ₹300 per-run and ₹1,000 "
        "project cap are enforced in code.")
    st.subheader("Prompts")
    st.markdown("Versioned in `/prompts`: `enrich_v3_20261027` (tagging; v1 and v2 rejected after test runs), "
                "`themes_v1_20261028` (grouping free text), `surface_v1_20261028` (card text + actionability), "
                "`live_v1_20261028` (this demo's safety check).")
    if SPEND is not None and len(SPEND):
        sp = SPEND.copy()
        sp["cost_inr"] = pd.to_numeric(sp["cost_inr"], errors="coerce").fillna(0)
        by = sp.groupby("stage")["cost_inr"].sum().round(2).sort_values(ascending=False)
        st.subheader("What it cost")
        st.write(f"Total Gemini spend: **₹{by.sum():.2f}** across {len(sp):,} logged calls.")
        st.dataframe(by.reset_index().rename(columns={"stage": "Stage", "cost_inr": "₹"}), hide_index=True)
    st.subheader("Limitations")
    st.markdown(
        "- Public posts over-represent people with problems and long-time investors: most posts that state timing are "
        "from investors 12+ months in, while the target segment is in its first 12 months. Interviews fill this gap.\n"
        "- Segment details (age, job, city, income) are rarely stated, so segment cuts have small samples.\n"
        "- Trigger type and seeded category agreed with the hand labels only 25% of the time; stop scenarios (75%) "
        "and journey stage (88%) are used for the headline findings instead.\n"
        "- 245 relevant posts (14%) had evidence the checker couldn't match word for word; their labels are excluded.\n"
        "- App Store reviews are capped by Apple's feed (29 posts after cleaning); X and LinkedIn were not collected.")
    footer()
