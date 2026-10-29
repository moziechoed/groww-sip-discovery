# PRD: Groww SIP Continuation Discovery Engine

| Field | Value |
|---|---|
| Version | v1.2 |
| Status | Locked for build |
| Owner | PM, Groww Growth Team |
| Build tool | Antigravity |
| Engine done-by | 30 Oct 2026 |
| Project deadline | 9 Nov 2026, 3:59 PM IST |

> [!NOTE]
> **Changes from v1.0 → v1.1**
> 1. Flash is the sole LLM for all stages. Pro removed entirely (0 expected calls).
> 2. `checking_behaviour` split into `checking_what` + `checking_frequency`.
> 3. `seeded_category` split into `seeded_category_primary` + `seeded_category_secondary` (nullable); scoring uses primary only.
> 4. App Store: accept RSS cap (~500 reviews), log shortfall, no extra effort.
> 5. `continuer_keywords` pre-loaded as a second keyword group in `config.yaml`; re-collection = one flag flip.
> 6. Discovered categories: bucket under "Emerging" until n ≥ 15, then promote to standalone card. Never merged into nearest seed.
> 7. FR1.7 added: hard per-source cap at 40% of cleaned corpus; excess sampled down (stratified by date) and logged in drop log.

> [!NOTE]
> **Changes from v1.1 → v1.2** (Guardrails Addendum v1.2 absorbed; Guardrails override any conflicting line)
> 1. Billing: Gemini paid tier on Google Cloud Postpay. ₹1,000 hard ceiling (Cloud spend cap + mandatory in-code cap). Cost estimate: ₹300–450.
> 2. Model: `gemini-3.6-flash` for every stage; exact ID verified via `models.list` in P0. Thinking set to the lowest allowed level for tagging.
> 3. Corpus target: 3,000–4,000 posts; 5,000 only if the 20-post test shows it fits under ₹1,000.
> 4. Clean is rules-only (no LLM). Borderline posts kept and flagged `clean_borderline=true`; `is_relevant` added to Enrich so off-topic posts drop out before Analyse at no extra calls.
> 5. Behavioural Specificity is now deterministic (config thresholds). Actionability stays LLM-rated with rubric anchors and a one-line rationale shown in the UI.
> 6. Live Analysis: 3 calls/session, 50/day global (file-backed counter, not Streamlit Secrets). Safety check + enrichment in one call. Main key only; no separate demo key.
> 7. New §11.1 Budget & Approval rules (approval gate, per-run ₹300 / project ₹1,000 caps, cost formula, spend log). AC13–AC18 added.
> 8. Golden set files: `outputs/golden_engine.csv` (engine) and `data/seeds/golden_human.csv` (PM). Both CSV.
>
> **Post-lock corrections (25 Oct, still v1.2; P1 kickoff)**
> 1. §18 P6 row corrected to "Clean (rules-only, no LLM → FR1.7 source cap → drop log)". It still described a Flash off-topic check, which conflicted with §7 and FR2.3.
> 2. Stage 1 output: each collector writes `data/raw/{source}.jsonl`; Stage 1 then collates them into `data/raw/raw_posts.jsonl` (the §7 input to Clean).
> 3. App Store app ID `1404871703` verified via the iTunes Lookup API ("Groww Stocks, Mutual Fund, IPO", seller Billionbrains Garage Ventures Pvt Ltd, bundle `com.nextbillion.groww`).
> 4. App-store collection matches SIP/MF anchor terms plus both keyword groups in one pass (whole-word, case-insensitive). Phrases without an anchor (e.g., "band kar diya", "still going") count only if an anchor appears in the same review. Play Store is stratified by star rating; per-star scanned vs matched counts are logged in `corpus_stats.json` for disclosure.
>
> **Post-lock corrections (25 Oct, still v1.2; P2 kickoff)**
> 1. Reddit collection uses public no-login `.json` / `.rss` (Atom XML) endpoints instead of PRAW OAuth (Reddit's official API now requires manual approval). `.json` automatically falls back to `.rss` when blocked by Reddit's WAF (HTTP 403).
> 2. Subreddits expanded from 5 to 8: `r/IndiaInvestments`, `r/personalfinanceindia`, `r/MutualfundsIndia` (verified active), `r/FIREIndia`, `r/IndianStreetBets`, `r/StockMarketIndia`, `r/IndianStockMarket`, `r/IndiaTax`.
> 3. Comments inside a SIP-anchored thread match either on their own (`raw_meta.matched_via = "self"`) or when they contain any `core`, `continuer`, or `hinglish` phrase (`raw_meta.matched_via = "thread_context"`). Matched comments are capped at 30 per thread (logged under `thread_comment_cap`).
>
> **Post-lock corrections (26 Oct, still v1.2; Billing update)**
> 1. Billing switched from Google Cloud Postpay to **AI Studio Prepay** (₹1,000 credits, auto-reload OFF; calls fail with HTTP 402 at ₹0). The Google Cloud ₹1,000 manual credit was refunded. The API key now lives in `"My First Project"` (paid tier, Tier 1). The in-code ₹1,000 project cap and ₹300 per-run cap in `core/budget.py` remain mandatory.
> 2. `llm_client` treats HTTP 402 as a clean stop (checkpoints and exits immediately with zero retries) and logs `"Gemini prepay credits exhausted — top up in AI Studio"`.
> 3. Note on `outputs/spend_log.csv`: the first P0 `test-call` ran on the free tier, so it actually cost ₹0, even though `spend_log.csv` records the formula value of ₹0.0247.
>
> **Post-lock corrections (26 Oct, still v1.2; P4 YouTube kickoff)**
> 1. Expanded YouTube channels from 5 to 10 verified handles (`Groww`, `CARachanaRanade`, `LabourLawAdvisor`, `warikoo`, `pranjalkamra`, `ETMONEY`, `VarsitybyZerodha`, `AkshatZayn`, `nehanagar`, `shankarnath`; `@shashankudupa` dropped after `channels.list` verification showed 0 in-window videos).
> 2. Discovery combines 1-unit channel uploads playlist scans (`playlistItems.list`) with 6 targeted `search.list` queries (`daily_quota_cap_units: 8000`). Videos qualify only if their title or lead description (`description[:250]`) contains a whole-word SIP/MF anchor.
> 3. Matched comments are capped at 40 per video (by `like_count` descending), 200 per channel (harvested round-robin across channels), and 1,500 total. Creator self-comments/replies (`authorChannelId == channel_id`) and promo/scam spam are dropped, while genuine investor comments mentioning `"crypto"` or `"+91"` without solicitation cues are retained. Inside SIP/MF-anchored videos, comments with a first-person marker + behaviour term also qualify as `matched_via = "thread_context"`.
> 4. Collection refuses to start if `AUTHOR_HASH_SALT` is missing or set to a placeholder; existing P1/P2 JSONL files remain untouched.
>
> **Post-lock corrections (26 Oct, still v1.2; P5 TradingQ&A + Context Data)**
> 1. **TradingQ&A collector (`collectors/tradingqna.py` → `data/raw/tradingqna.jsonl`):** Promoted from manual CSV seed to an automated public Discourse JSON collector (`/search.json` + `/t/{topic_id}.json`, no login). Queries use 12 single-phrase searches (`"SIP stopped"`, `"pause SIP"`, `"cancel SIP"`, `"SIP mandate"`, `"autopay SIP"`, `"SIP installment failed"`, `"first SIP"`, `"Coin SIP"`, `"step up SIP"`, `"ELSS SIP"`, `"XIRR mutual fund"`, `"mutual fund SIP"`) because live testing confirmed Discourse treats quoted `OR` as an `AND` literal (0 hits).
> 2. TradingQ&A strips `<aside class="quote">...</aside>` before matching, applies the shared first-person + behaviour `thread_context` rule inside SIP/MF-anchored topics, caps replies at 20 per topic (by `like_count DESC, created_at ASC`) and 400 total matched records, and sets `raw_meta.platform_tag = "zerodha"` (competitor posts retained and tagged).
> 3. **Context data (`collectors/context_data.py` → `data/context/`, `--stage context`):** Strictly isolated from `data/raw/raw_posts.jsonl` and never enters Clean or Enrich (0 LLM calls). Writes three verified CSVs:
>    - `data/context/amfi_sip_monthly.csv`: last 24 months of SIP figures + 3 FY summary rows parsed from `https://www.amfiindia.com/mutual-fund` with `source_url` per row.
>    - `data/context/factbook_demographics.csv`: investor demographics (age bands, gender, T30 vs B30 city tier, holding period, state-wise age allocation) extracted from the official AMFI–Crisil Factbook 2026 PDF (`https://www.amfiindia.com/uploads/AMFI_Factbook2026_14b34a6447.pdf`) with `pdf_page` and `confidence` (`high` | `low`).
>    - `data/context/mfsh_index.csv`: curated official-source fallback index discovered strictly from `https://www.mutualfundssahihai.com/sitemap.xml` (`sitemap-0.xml`), verified `http_status == 200`, with `summary` taken verbatim from each page's `<meta name="description">`, `og:description`, or lead paragraph.
>
> **Post-lock corrections (26 Oct, still v1.2; P6 Clean)**
> 1. **Non-Latin script filter (FR2.4):** Expanded beyond Devanagari to drop posts where `>50%` of alphabetic characters are in **any** non-Latin script (Devanagari, Tamil, Telugu, Kannada, Malayalam, Bengali, Gujarati, Gurmukhi, Oriya, etc.), logging drops per script and source in `corpus_stats.json`. Roman-script Hinglish is retained with `hinglish=true`.
> 2. **Strict PII isolation (FR2.5):** PII (`[EMAIL]`, `[UPI]`, `[PAN]`, `[PHONE]`, `[ACCOUNT_NUM]`) is masked in-place and stored as **both** `text` and `clean_text` in `data/clean/clean_posts.jsonl` so unmasked text never leaves `data/raw/`. `outputs/drop_log.csv` records dropped posts only; PII mask counts by type are logged in `outputs/corpus_stats.json`.
> 3. **Whole-text near-dedupe (FR2.1):** Uses `rapidfuzz.fuzz.token_sort_ratio >= 90` with a length-ratio guard (`>=0.85`) instead of `token_set_ratio`, so short posts are never falsely collapsed into longer ones.
> 4. **Reddit 40% cap & selection-bias audit (FR1.7, FR2.7):** Clamps total corpus at `target_max = 4,000` and downsamples Reddit using **square-root damping** ($\propto \sqrt{n_{m,s}}$) across `(YYYY-MM, subreddit)` strata, ranked by `quality_tier` (preferring continuation/drop-off/mandate behaviour and Groww mentions over generic portfolio-review questions). Every kept Reddit record stores `raw_meta.quality_tier` and `raw_meta.stratum`, excess is archived to `data/clean/source_cap_overflow.jsonl`, and before/after theme-keyword counts are logged in `outputs/corpus_stats.json`.

>
> **Post-lock corrections (27 Oct, still v1.2; P7 Enrich)** — built directly in the repo (Antigravity quota exhausted until 31 Oct)
> 1. **Schema:** `kpi_branch` (voluntary_stop / involuntary_payment_failure / lapse_and_return / platform_switch / continuing / not_applicable) and `stop_scenario_ids` (0–3 ids from the 17 stop scenarios in KPI Tree v1) added to `EnrichmentFields`, so enrichment sizes the KPI tree directly. `journey_stage` gains `month_12_plus`. `in_app_confusion` limited to 25 words. `EnrichedPost.unverified_labels` holds labels whose evidence could not be verified (audit only).
> 2. **Prompt:** `prompts/enrich_v1_20261027.txt` (FR3.4: versioned + dated). All label definitions sit between `### DEFINITIONS START/END`; `docs/labelling_guide.md` copies that section verbatim so the PM and the engine label against identical rules. 8 few-shot examples (English, Hinglish, payment failure, salary-date mismatch, lapse, platform switch, continuer, off-topic, Roman-script Telugu → `is_relevant=false`). Gemini `propertyOrdering` makes the model decide relevance and outcome before choosing evidence.
> 3. **Golden set before enrichment (FR6.1–6.3):** `python run.py --stage golden-sample` (0 LLM calls) picks 20 posts (12 stratified by source × behaviour cue, first-hand wording only; 8 ambiguous: chose-vs-failed, Hinglish, clean_borderline, reduced, considering-vs-continuing). Writes `outputs/golden_set.csv` and `data/seeds/golden_human.csv` (never overwritten once labelled). Hand-labelled fields: `is_relevant`, `outcome`, `kpi_branch`, `stop_scenario_ids`, `trigger_type`, `seeded_category_primary`, `journey_stage` (required); `outside_app_behaviour`, `segment_confidence` (optional). `stop_scenario_ids` agreement = any overlap (exact-set match also reported). Golden posts are enriched normally but excluded from the 20-post test and its printed samples.
> 4. **Order enforced in code:** golden-sample → PM labels blind → `--stage enrich --limit 20` (paid test, calibrates tokens/cost) → `--stage enrich` (full run refuses to start without calibration and complete golden labels).
> 5. **Batching & recovery:** batch 30; one retry for missing/invalid items (validated item by item, so one bad item never fails a batch); one evidence retry; still-unverifiable labels are downgraded to unknown with `status = evidence_span_failed` (AC5 holds by construction). A reply cut off at `max_output_tokens` (`finish_reason = MAX_TOKENS`) is split in half and retried, never "repaired". Output limit per post = max(350, measured average × 1.5).
> 6. **Cost:** pre-run estimate uses measured cost per post after the 20-post test and shows how many runs the ₹300 per-run cap implies. Uncalibrated estimate for 4,000 posts: ~₹350 (worst case ~₹570). HTTP 402, budget caps and Ctrl+C all stop cleanly with finished batches saved.
> 7. **Segment Fit:** `scoring/segment_fit.py` implements the §9.5 deterministic weights; computed per post at enrichment (None for irrelevant or failed posts).
> 8. **Prompt v2 after the 20-post test (27 Oct).** v1 test: 100% schema-valid, 100% evidence verified after retry, but ₹0.174/post (full run ~₹692) and over-tagging (questions tagged as events, redemption tagged as "stopped", outcome "continued" with branch "voluntary_stop"). `prompts/enrich_v2_20261027.txt`: omit unknown/default fields, questions and hypotheticals keep outcome "unknown", outcome→kpi_branch consistency table (also enforced in code: `consistent_branch`), evidence spans 5–20 words, output floor 500 tokens/post. `--redo-test` re-runs the same 20 posts and prints a before/after comparison; v1 outputs archived in `outputs/enrich_tests/` for the Evidence Log (rejected/corrected AI output). Calibration resets when the prompt version changes. `docs/labelling_guide.md` regenerated from v2.
> 9. **Prompt v3 (27 Oct).** v2 cut cost to ₹0.062/post (full run ~₹245) and fixed every v1 over-tag, but over-corrected: all 20 test outcomes became "unknown", including "should I redeem my funds to clear debt?" (should be considering_stop), and one evidence span did not support its tag. v3: weighing a change to one's OWN SIP = considering_stop; a SIP stated as running for a period = continued; the evidence phrase must show the outcome or trigger; new worked example 11. Added `--event-sample` (20 posts with first-hand stop/pause/failure/restart/continue wording) to check that real events are still tagged.
>
> **Post-lock corrections (28 Oct, still v1.2; P7 full run, P8 Analyse + Surface, P9 Evaluate, P10 UI)** — built directly in the repo
> 1. **P7 full run:** 4,000 posts enriched in 2 runs (₹387; project total ₹396). 100% schema-valid, 0 `enrichment_failed`; 1,731 relevant; 245 relevant posts (14%) downgraded to `evidence_span_failed` and excluded from label statistics. The PM's golden labels were imported from the Google Sheet (stop scenarios + 3 branch/outcome edits made after a guide re-read, marked in `notes`).
> 2. **P9 Evaluate (`pipeline/evaluate.py`, 0 LLM calls):** writes `golden_engine.csv`, `eval_report.json`, `eval_failures.csv`. Downstream fields are compared on rows the PM marked relevant; seeded "none" = null. Result: overall 57.4% (39/68), outcome 62.5%, is_relevant 60%, stop scenarios 75%, journey stage 87.5%, trigger_type and seeded category 25% → **AC9 not met**, reported as is. 7 of 8 relevance misses are definition gaps (the guide counts own mandate/KYC problems and own SIP confusion as relevant; the PM did not). A post-hoc "event relevance" reading (engine relevant + KPI branch set) matches the PM's relevance 18/20; reported as a hypothesis only. Failure reasons (FR6.5) are drafted by Claude in `data/seeds/failure_reasons.csv` for PM review instead of a Flash call. No prompt re-tune on the golden set (it would overfit 20 posts and a full re-run costs ~₹390).
> 3. **P8 Analyse (`pipeline/analyse.py`):** populations relevant / labelled (status ok) / event (kpi_branch set). Every headline share is shown raw and **source-balanced** (mean of per-source shares over sources with ≥30 posts) to undo Reddit down-sampling. Free-text fields (`unmet_need`, `in_app_confusion`, `start_reason`, `expectation`) are grouped into 5–12 themes by 4 Flash calls (`prompts/themes_v1_20261028.txt`, gated, cached in `outputs/themes.json`). Fields under 60% golden agreement are flagged low-confidence and kept out of headline claims. Exports `analysis.json` and `posts_public.csv` (labels + verified short evidence span only, no post text, no author hash).
> 4. **P8 Surface (`pipeline/surface.py`):** opportunity clusters = the **primary stop scenario** (KPI Tree v1) of each stop-event post instead of trigger_type × journey_stage × unmet_need (trigger_type 25% golden agreement; journey stage unknown for most posts). Frequency = n ÷ largest card (not min-max, which would zero the smallest card in a product). `score_display` rescales so the top card = 100. One Flash call (`prompts/surface_v1_20261028.txt`) writes titles, descriptions and actionability (1–3 + rationale) for all clusters; cached in `outputs/surface_llm.json`. Quotes: verified spans ≤30 words, ranked by scenario cue words → expected branch → Groww tag → segment fit; quotes naming a fund house or scheme are skipped (guardrail). Card text is scanned for fear/urgency/recommendation words.
> 5. **Synthesis stages and the 20-post rule:** the "test on 20 posts first" rule applies to per-post stages; Analyse (4 calls) and Surface (1 call) print a worst-case estimate and need a typed `yes` instead.
> 6. **P10 UI (`app/streamlit_app.py`, `app/live.py`):** six tabs per §10; reads only `outputs/` and `data/seeds/`. Live Analysis = enrich prompt v3 + `prompts/live_v1_20261028.txt` (safety + tagging in one call), local pre-check declines fund-pick requests with no call, PII masked before the call, evidence verified after it. Tests: `tests/test_live.py` (AC10), `tests/test_analyse_surface.py`, `tests/test_evaluate.py`.
---

## 1. Purpose
Build an AI-powered discovery engine that analyses public conversations at scale to find **why first-time SIP investors stop, pause or reduce their SIPs within 12 months**, and to rank the opportunity areas that most influence 12-month SIP continuation on Groww.

The engine replaces weeks of manual research: collecting, cleaning, tagging, comparing and ranking thousands of posts. Its output feeds problem definition, user interview design and the final deck.

## 2. Background
- **Business goal:** increase the % of first-time mutual fund investors on Groww who still have an active SIP 12 months after starting their first one.
- **Active SIP:** at least one successful instalment in the last 30 days.
- **Target segment:** salaried professionals aged 22–28 in tier 1 cities, in their first four years of work, who started their first SIP on Groww less than 12 months ago.
- **The gap:** we do not know why continuation breaks down despite good intent. "Markets fell" is not an acceptable root cause. We need behavioural and contextual causes tied to specific moments in the first 12 months.

## 3. Scope

### 3.1 In scope
- Collecting public text from app stores, Reddit, forums, YouTube comments and LinkedIn.
- Cleaning, deduplicating and removing PII.
- LLM-based structured enrichment of every post (`gemini-3.6-flash` only, paid tier, budget-capped).
- Pattern analysis: continuers vs drop-offs, journey stages, segment cuts, co-occurrence.
- Ranked opportunity areas with evidence.
- A golden set workflow with agreement reporting.
- A public Streamlit UI for demonstration and single-post live analysis.

### 3.2 Out of scope
- Any Groww internal data (public data only).
- Building the end-user product feature (that is the separate MVP).
- Fund or stock recommendations of any kind.
- Re-running the full pipeline from the UI.
- Pure Hindi (Devanagari) content.
- Real-time or scheduled monitoring (the engine is re-runnable, not continuous).
- Gemini Pro usage (0 expected calls; Flash handles all stages).
- Any LLM call in the Clean stage.

## 4. Goals and Non-Goals

### 4.1 Goals
1. Answer the 9 key research questions (Section 6) with evidence.
2. Quantify and rank opportunity areas, not just summarise them.
3. Compare continuers vs drop-offs.
4. Prove the engine's accuracy against a hand-labelled golden set.
5. Be testable by anyone through a public link.

### 4.2 Non-Goals
- Sentiment analysis as an end in itself.
- Generating solutions (the engine finds problems; the PM defines solutions).
- Perfect segment identification (unknowns are allowed and reported).

## 5. Users

| User | Need | Primary surface |
|---|---|---|
| PM (builder/researcher) | Run the pipeline, label the golden set, extract insights for the deck | Scripts + UI |
| Mentors / graders | Test that the engine works and the insights are credible | Public Streamlit UI |
| Future re-run user | Re-run on fresh data with the same config | Scripts + config file |

## 6. Research Questions the Engine Must Answer
1. Why do people start a SIP, and what do they expect to happen?
2. How does their behaviour change in the months after starting?
3. What triggers a pause, a reduction or a stop?
4. What do they check, and how often?
5. What confuses or worries them inside the Groww app?
6. What do they do outside the app when they feel unsure?
7. What separates investors who continue from those who drop off?
8. How do patterns differ by income stability, age, source of money and who influenced them?
9. What unmet needs come up consistently?

Each question maps to at least one enrichment field and one analysis output (see traceability table in Section 9.4).

## 7. Framework: 6-Stage Pipeline

```
[1 Collect] → [2 Clean] → [3 Enrich] → [4 Analyse] → [5 Surface] → [6 Evaluate]
   raw text     clean corpus   tagged posts   patterns      ranked cards   accuracy report
```

| Stage | Input | Output | Model |
|---|---|---|---|
| 1 Collect | Source list + config.yaml | raw_posts.jsonl | None |
| 2 Clean | raw_posts.jsonl | clean_posts.jsonl + drop_log.csv | Rules only (no LLM) |
| 3 Enrich | clean_posts.jsonl | enriched_posts.jsonl | Gemini Flash (every clean post is enriched; `is_relevant=false` posts are excluded from Stage 4 onward, no extra calls) |
| 4 Analyse | enriched_posts.jsonl | analysis.json | Gemini Flash (synthesis only; stats computed with pandas) |
| 5 Surface | analysis.json | opportunities.json + .csv | Gemini Flash (Actionability rating + card titles/descriptions; other factors deterministic) |
| 6 Evaluate | enriched posts + human labels | golden_set.csv, golden_engine.csv + eval_report.json | Gemini Flash (failure-reason drafts only) |

> [!IMPORTANT]
> **`gemini-3.6-flash` is the sole LLM for all stages** (paid tier, Tier 1 in `"My First Project"`, AI Studio Prepay with ₹1,000 credits and auto-reload OFF). Pro is planned for 0 calls. Every Gemini-calling stage runs on a 20-post sample first and needs an approved pre-run estimate (§11.1). FR3.3 checkpoint/resume is mandatory. The model ID is set in `config.yaml` and verified via `models.list` in P0.

## 8. User Journeys

### 8.1 Journey A: PM runs the pipeline
1. Adds the Gemini key to `.env`.
2. Edits `config.yaml` (sources, date window, volume targets, keyword groups, model names).
3. Runs stages one at a time (`python run.py --stage collect`, etc.) or all at once (`--stage all`). Every Gemini-calling stage first runs on a 20-post sample.
4. Before any Gemini call, `run.py` prints a pre-run estimate (§11.1) and waits for the PM to type `yes`.
5. Reviews the drop log, stage summaries and `spend_log.csv` (actual vs estimated cost) after each stage.
6. Pushes outputs to the repo; the Streamlit app reads them.

### 8.2 Journey B: PM labels the golden set
1. Runs Stage 6 → the engine proposes 20 candidates in `outputs/golden_set.csv` (post text + blank label columns + bucket). Engine labels go to `outputs/golden_engine.csv`, which the PM does not open until after labelling.
2. PM fills in human labels and saves them as `data/seeds/golden_human.csv`.
3. Re-runs Stage 6 in compare mode → the agreement report is generated.
4. PM reviews the failure cases and records 2–3 corrections for the Evidence Log.

### 8.3 Journey C: Grader explores insights
1. Opens the public link → lands on the Overview (corpus size by source, drop counts, segment coverage).
2. Opens the Opportunities tab → sees the ranked cards, including any "Emerging" discovered-category cards.
3. Filters by journey stage, trigger, segment confidence or platform.
4. Expands a card → sees the score breakdown and 3–5 evidence quotes with source links.
5. Opens the Evaluation tab → sees the golden set agreement % and failure cases.

### 8.4 Journey D: Grader tests live analysis
1. Opens the Live Analysis tab → pastes one post.
2. The engine returns the enrichment fields with a confidence label (safety check + enrichment in one Gemini call).
   - Limits: 3 analyses per session, 50 per day across all users, input ≤ 2,000 characters.
3. **Safe fallback:**
   - If the post is off-topic or confidence is low → shows "Not enough signal to classify" instead of guessing.
   - If the post asks for a fund or stock recommendation → declines and points to AMFI / Mutual Funds Sahi Hai.
4. A disclaimer is always shown: "Research tool. Not investment advice."

## 9. Functional Requirements

### 9.1 Stage 1: Collect

| Source | Method | Priority | Notes |
|---|---|---|---|
| Play Store (Groww) | `google-play-scraper` | 1 | — |
| Reddit: r/IndiaInvestments, r/personalfinanceindia, r/MutualfundsIndia, r/FIREIndia, r/IndianStreetBets, r/StockMarketIndia, r/IndianStockMarket, r/IndiaTax | Public `.json` / `.rss` endpoints (no login / no API key) | 1 | Keyword search across sort/time slices + thread comment feeds (≤30 matched comments/thread); auto-falls back from `.json` to `.rss` on HTTP 403 |
| App Store (Groww) | RSS reviews feed | 2 | Cap ~500 reviews; log actual volume and shortfall; no extra effort |
| YouTube: Groww, CA Rachana Ranade, Labour Law Advisor, Warikoo, Pranjal Kamra, ET Money, Zerodha Varsity, Akshat Shrivastava, Neha Nagar, Shankar Nath | YouTube Data API v3 (`channels.list` + `playlistItems.list` + 6 targeted `search.list` + `commentThreads.list`) | 2 | SIP/MF-anchored videos only; round-robin across channels (≤200/channel), ≤40 matched comments/video, ≤1,500 total; 8,000-unit/day in-code quota guard |
| TradingQ&A (`tradingqna.com`) | Public Discourse JSON API (`/search.json` + `/t/{id}.json`, no login) | 3 | 12 single-phrase SIP queries; ≤20 replies/topic, ≤400 matched total; `raw_meta.platform_tag = "zerodha"` |
| Quora, LinkedIn, ValuePickr, Freefincal | Manual CSV seed | 3 | LinkedIn is manual-only (ToS); use CSV template (see FR1.8) |
| AMFI monthly SIP data, AMFI–Crisil Factbook 2026 PDF, Mutual Funds Sahi Hai index | Automated context builder (`python run.py --stage context` → `data/context/*.csv`) | Context | Context only (`amfi_sip_monthly.csv`, `factbook_demographics.csv`, `mfsh_index.csv`); never enters Clean/Enrich |
| Twitter/X | Optional | — | Skip if blocked |

**Keyword groups (both configurable in `config.yaml`)**

- **`sip_keywords`** (drop-off focused): SIP stopped, paused SIP, cancel SIP, first SIP, SIP missed, SIP returns, Groww SIP, started investing, mutual fund beginner, SIP worth it, SIP band, SIP ruk, SIP chhod, band kar diya.
- **`continuer_keywords`** (continuer focused): still going, 1 year SIP, didn't stop SIP, continued SIP, SIP anniversary, never stopped, kept my SIP, SIP chalu hai, band nahi kiya. Re-collection against this group is triggered by a single flag in `config.yaml`.

**Requirements**
- FR1.1 Both keyword groups are configurable in `config.yaml`. The active group is set per run.
- FR1.2 Date window: last 24 months (configurable).
- FR1.3 Every record carries: `id, source, url, date, author_hash, text, collection_method (scraped|manual), raw_meta`.
- FR1.4 Usernames are hashed at collection and never stored in plain text.
- FR1.5 Each source runs independently; one failing source must not stop the others.
- FR1.6 Log volume collected per source.
- FR1.7 **Hard per-source cap: no single source may exceed 40% of the cleaned corpus.** If exceeded, sample down that source stratified by date (keeping the most recent posts proportionally), archive the excess, and log the count in the drop log under reason `source_cap`.
> [!IMPORTANT]
> **P0 prerequisite:** call the Gemini API `models.list` endpoint with the project key to confirm the exact model ID string for `gemini-3.6-flash` before any other Gemini-dependent code is written.

- FR1.8 **Manual CSV seed template:** columns must match FR1.3 exactly (`id, source, url, date, author_hash, text, collection_method=manual, raw_meta`). A blank template is provided in `/data/templates/manual_seed_template.csv`.

### 9.2 Stage 2: Clean
- FR2.1 Exact dedupe (canonical SHA-256) + whole-text near-duplicate removal (`rapidfuzz.fuzz.token_sort_ratio >= 90` with a `>=0.85` length-ratio guard; never `token_set_ratio`), within and across sources.
- FR2.2 Remove spam, promotional and referral-code posts, plus posts `< 5` words / `< 20` characters (`too_short_or_junk`).
- FR2.3 Off-topic filter: **keyword rules only; no LLM call in this stage.** Keep posts about SIPs, mutual funds, investing habits or investing apps. Borderline posts are **kept** (err toward inclusion) and flagged `clean_borderline=true`; the Enrich stage's `is_relevant` field makes the final call at no extra cost. The former `offtopic_v1.txt` prompt is moved to `prompts/archive/`.
- FR2.4 Language filter: keep English + Roman-script Hinglish (setting `hinglish=true`); drop posts where `>50%` of alphabetic characters are in **any** non-Latin script (Devanagari, Tamil, Telugu, Kannada, Malayalam, Bengali, Gujarati, etc.) and log drops per script and source.
- FR2.5 PII strip: mask phone numbers (`[PHONE]`), emails (`[EMAIL]`), PAN patterns (`[PAN]`), UPI IDs (`[UPI]`), and 9–16 digit account/folio/Aadhaar numbers (`[ACCOUNT_NUM]`) in-place. Store **only** the masked text in both `text` and `clean_text` of `clean_posts.jsonl` so unmasked text never leaves `data/raw/`. Log PII mask counts by type in `corpus_stats.json` (never the values).
- FR2.6 Drop log (`outputs/drop_log.csv`): count and reason for every **dropped** post, per source (`too_short_or_junk`, `language`, `spam_promo`, `duplicate`, `off_topic`, `source_cap`).
- FR2.8 Each CleanPost carries `clean_borderline: bool` (true if the post was borderline off-topic / generic portfolio query but retained) and carries through `raw_meta.platform_tag`.
- FR2.7 Target **3,000–4,000** posts after cleaning (clamped at `target_max = 4,000` with no single source `>40%`; Reddit downsampled via square-root damping across `(YYYY-MM, subreddit)` strata and ranked by `quality_tier`, with `quality_tier` and `stratum` stored in `raw_meta` and before/after theme-keyword counts logged in `corpus_stats.json`). Expand to 5,000 only if the 20-post Enrich test shows the projected total project cost fits under ₹1,000. If under 3,000, flag it and suggest which sources or keywords to expand. If the App Store shortfall contributes to this, note it explicitly.

### 9.3 Stage 3: Enrich
Batch LLM tagging (`gemini-3.6-flash`, 25–40 posts per call; start at 30 and tune on the 20-post test). **Every clean post is enriched**, because `is_relevant` is decided inside the same call. Every field allows `unknown`. Output is strict JSON validated against a schema; invalid outputs are retried once and then marked `enrichment_failed`.

| Field | Values / Format |
|---|---|
| `is_relevant` | boolean — false if the post contains no first-hand or directly observed SIP start/continuation/stop experience (e.g., generic fund advice, unrelated app complaint). `is_relevant=false` posts are kept in `enriched_posts.jsonl` but excluded from Stages 4–5 |
| `clean_borderline` | boolean, pass-through from Clean (not LLM-assessed) |
| `journey_stage` | pre_start / month_0_1 / month_2_3 / month_4_6 / month_7_12 / month_12_plus / post_stop / unknown |
| `outcome` | continued / paused / reduced / stopped / restarted / considering_stop / unknown |
| `start_reason` | free text, ≤ 20 words |
| `expectation` | free text, ≤ 20 words |
| `trigger_type` | life_event_financial_pressure / emotional / ux_friction / financial_literacy_gap / feature_gap / social_peer_influence / returns_disappointment / other / none |
| `trigger_detail` | free text, ≤ 25 words |
| `checking_what` | free text, ≤ 15 words — what the investor checks (e.g. "portfolio value", "XIRR", "NAV") |
| `checking_frequency` | daily / weekly / monthly / rarely / unknown |
| `in_app_confusion` | free text or none |
| `outside_app_behaviour` | asked_family_friends / youtube / reddit_forums / advisor / news / did_nothing / other / unknown |
| `unmet_need` | free text, ≤ 25 words |
| `segment_signals` | `age_band`, `employment` (salaried / self_employed / student / other), `years_of_work`, `city_tier`, `income_stability`, `source_of_money`, `influencer` — each may be unknown |
| `segment_confidence` | high / medium / low |
| `is_first_time_investor` | yes / no / unknown |
| `seeded_category_primary` | one of the 6 seeds (mandatory if any seed applies) |
| `seeded_category_secondary` | one of the 6 seeds or null (nullable; used when a post clearly spans two seeds) |
| `discovered_category` | free label or null — flagged as `discovered`; used when post does not fit any seed |
| `platform_tag` | groww / zerodha / indmoney / paytm_money / mfcentral / other / unspecified |
| `no_fund_stock_flag` | true if the post centres on specific fund or stock picks |
| `enrichment_confidence` | high / medium / low |
| `evidence_span` | exact verbatim phrase from the post supporting the trigger/outcome tag |
| `kpi_branch` | voluntary_stop / involuntary_payment_failure / lapse_and_return / platform_switch / continuing / not_applicable (P7) |
| `stop_scenario_ids` | 0–3 ids from the 17 stop scenarios (KPI Tree v1); [] if none (P7) |

> [!IMPORTANT]
> **Scoring uses `seeded_category_primary` only.** `seeded_category_secondary` is recorded for analysis but does not affect card assignment or opportunity scoring.

**Seed taxonomy (6 seeds):**
1. UX friction
2. Emotional triggers
3. Financial literacy gaps
4. Feature gaps
5. Life event / external financial pressure
6. Social / peer influence

New categories may emerge; they must be set in `discovered_category` and flagged as `discovered`. They are **never merged into the nearest seed**.

**Requirements**
- FR3.1 Never infer a segment value without textual evidence. Use `unknown` instead.
- FR3.2 `evidence_span` is mandatory for any non-unknown `outcome` or `trigger_type`. The span must be copied verbatim from the post text — no paraphrasing.
- FR3.3 Batching, exponential backoff, inter-batch pauses and **resume from checkpoint** (no re-enriching already-tagged posts). Multi-day enrichment runs are expected and supported.
- FR3.4 The prompt is stored in `/prompts` and versioned (filename includes version and date).
- FR3.6 Output never echoes the post text: only `post_id` + fields. Input text is truncated to ~1,500 characters per post. Temperature 0–0.2; thinking set to the lowest allowed level.
- FR3.5 A post-enrichment validation script confirms each `evidence_span` appears verbatim in `text`. Failures are auto-retried once; persistent failures are marked `evidence_span_failed` and excluded from scoring.

### 9.4 Stage 4: Analyse

> [!NOTE]
> Statistical aggregation (counts, ratios, cross-tabs) is computed with `pandas`. Gemini Flash is used only to cluster short free-text fields (`unmet_need`, `start_reason`, `expectation`) and synthesise findings — never full posts.
>
> All analysis runs on the `is_relevant=true` subset. `is_relevant=false` posts are counted in `corpus_stats.json` but excluded from every distribution, comparison and segment cut.

- FR4.1 **Continuers vs drop-offs:** compare trigger_type, journey_stage, checking_what, checking_frequency, outside_app_behaviour and expectation between `outcome = continued` and `outcome ∈ {paused, reduced, stopped}`.
- FR4.2 **Journey-stage breakdown:** top trigger_type and unmet_need per stage.
- FR4.3 **Segment cuts:** employment, income_stability, influencer, source_of_money, city_tier. Report sample size (n) for every cut; flag cuts with n < 30 as "low sample".
- FR4.4 **Co-occurrence:** top trigger_type × journey_stage × checking_frequency combinations (e.g., month_2_3 + returns_check + rarely → pause).
- FR4.5 **Seeded vs discovered** category distribution. Discovered categories shown separately; those promoted to standalone cards are flagged.
- FR4.6 Groww-specific posts (platform_tag = groww) weighted higher for in-app UX findings (FR4.1, FR4.2 for ux_friction and feature_gap seeds) only.
- FR4.7 **Continuer floor:** at least 15% of `is_relevant=true` enriched posts, or ≥ 300 posts, should be tagged `outcome = continued`. If not met, log the shortfall and flip the `continuer_keywords` flag in `config.yaml` to trigger targeted re-collection before re-running Stage 1.

**Question traceability**

| Research question | Fields used | Analysis output |
|---|---|---|
| Q1 Why start / expectations | start_reason, expectation | Start-reason distribution |
| Q2 Behaviour change | journey_stage, checking_what, checking_frequency | Stage breakdown |
| Q3 Pause/stop triggers | trigger_type, outcome | Trigger ranking among drop-offs |
| Q4 What they check | checking_what, checking_frequency | Frequency table |
| Q5 In-app confusion | in_app_confusion, platform_tag | Groww UX friction list |
| Q6 Outside-app behaviour | outside_app_behaviour | Behaviour distribution |
| Q7 Continuers vs drop-offs | all fields, split by outcome | Comparison table |
| Q8 Segment differences | segment_signals | Segment cuts |
| Q9 Unmet needs | unmet_need | Clustered need themes |

### 9.5 Stage 5: Surface
Group posts into opportunity areas (clusters of trigger_type + journey_stage + unmet_need), then score them.

**Score = Frequency × Segment Fit × Behavioural Specificity × Actionability**, normalised to 0–100. Recency is a tiebreaker only. Frequency, Segment Fit and Behavioural Specificity are deterministic. Actionability is LLM-rated (one Flash call per cluster). Flash also writes card titles and one-line descriptions. All Surface calls are included in the pre-run estimate.

| Factor | Definition |
|---|---|
| Frequency | Posts in the cluster ÷ total relevant posts, min-max normalised to 0–1 |
| Segment Fit | Mean post-level segment fit across the cluster (0–1, deterministic rule below) |
| Behavioural Specificity | **Deterministic.** Share of cluster posts where (a) `evidence_span` for trigger or outcome is verified verbatim AND (b) `checking_what` is non-unknown. Mapped to 1–3 by `scoring.specificity_thresholds` in `config.yaml` (defaults: ≥ 0.6 → 3, ≥ 0.3 → 2, < 0.3 → 1). Normalised to 0–1 |
| Actionability | **LLM-rated 1–3 per cluster** against written anchors: 1 = outside product control (e.g., job loss alone, medical emergency); 2 = partly influenceable (Groww can reduce friction or improve information but not remove the root cause); 3 = Groww can directly address within constraints (no recommendations, no fear/FOMO, no urgency). Flash returns the score + a one-line rationale, stored on the card and shown in the UI. PM spot-checks every cluster. Normalised to 0–1 |

**Post-level Segment Fit (deterministic, not LLM-judged)**

| Signal | Weight | Match | Unknown | Mismatch |
|---|---|---|---|---|
| employment = salaried | 0.30 | 1 | 0.4 | 0 |
| age 22–28 or years_of_work 0–4 | 0.25 | 1 | 0.4 | 0 |
| is_first_time_investor = yes, or SIP < 12 months | 0.25 | 1 | 0.4 | 0 |
| city_tier = 1 | 0.10 | 1 | 0.5 | 0.2 |
| income_stability = stable monthly | 0.10 | 1 | 0.5 | 0.2 |

Segment Fit = Σ(weight × value).

**Discovered-category card lifecycle**
- Discovered categories (posts where `discovered_category` is set and no seed applies) are grouped separately.
- While n < 15: shown in an **"Emerging Signals"** section in the UI, not scored, labelled `emerging`.
- When n ≥ 15: promoted to a standalone opportunity card, scored normally, flagged `discovered` throughout.
- Discovered categories are **never merged into the nearest seed**.

**Each opportunity card shows:** rank, title, one-line description, total score + factor breakdown, actionability rationale, n posts, % of corpus, journey stages affected, continuer vs drop-off ratio, 3–5 evidence quotes (each ≤ 30 words, verbatim, with source + link), source distribution, seeded/discovered flag, segment coverage.

- FR5.1 Exclude `no_fund_stock_flag = true` posts from scoring.
- FR5.2 Any card with n < 15 is labelled "emerging" (applies to both seeded and discovered categories).
- FR5.3 Export `opportunities.json` and `opportunities.csv`.

### 9.6 Stage 6: Evaluate
- FR6.1 Propose 20 candidates: ~12 stratified across categories, journey stages and sources + ~8 deliberately ambiguous (low confidence or conflicting fields). Tag the bucket (`stratified` / `ambiguous`) on each row.
- FR6.2 Engine labels are stored in `outputs/golden_engine.csv` so the human labeller is blind to them. The PM labels `outputs/golden_set.csv` (shows only `id`, `text`, `bucket` and blank label columns) and saves it as `data/seeds/golden_human.csv`. Both are CSV so they can be compared side by side in a sheet.
- FR6.3 Fields labelled by hand (P7 update): required `is_relevant`, `outcome`, `kpi_branch`, `stop_scenario_ids`, `trigger_type`, `seeded_category_primary`, `journey_stage`; optional `outside_app_behaviour`, `segment_confidence`. `stop_scenario_ids` agreement = any overlap; exact-set match also reported.
- FR6.4 Report: per-field agreement %, overall agreement %, agreement split by bucket (stratified vs ambiguous), and a confusion matrix summary for the weakest field.
- FR6.5 Failure-case table: post id, engine label, human label, likely reason.
- FR6.6 Optional: re-run the 20 items after a prompt fix and report before/after agreement delta.

## 10. UI Requirements (Streamlit)

| Tab | Content |
|---|---|
| Overview | What the engine does (3 lines), corpus by source (with App Store shortfall noted if applicable), drop log summary, segment coverage, last run date |
| Opportunities | Ranked cards (seeded); "Emerging Signals" section for discovered categories with n < 15; filters for journey stage, trigger, platform, segment confidence; expandable evidence |
| Insights | Continuers vs drop-offs comparison, stage breakdown, segment cuts (with n), charts |
| Evaluation | Golden set agreement %, per-field table, agreement by bucket, failure cases |
| Live Analysis | Paste a post → enrichment output + confidence; safe fallback |
| Method | Pipeline diagram, sources, prompts summary, limitations |

- UI1 Persistent footer: "Research tool built on public data. Not investment advice."
- UI2 Works on mobile width.
- UI3 Colour-blind-safe palette; never rely on colour alone.
- UI4 Live Analysis limits: **3 calls per session; 50 calls per day global**, input ≤ 2,000 characters. When the daily cap is hit, show: "Daily demo limit reached. Please try again tomorrow." The daily counter lives in a small file-backed store (via `st.cache_resource`), **not** in Streamlit Secrets (which are read-only). It resets if the app restarts, which is acceptable for a demo; the project budget cap remains the final backstop.
- UI7 Live Analysis uses the main Gemini key (from Streamlit Secrets). Safety classification and enrichment happen in a single call per post. No separate demo key.
- UI5 The app loads precomputed outputs; no pipeline runs from the UI.
- UI6 "Emerging Signals" section is always visible on the Opportunities tab even if empty (shows "No emerging signals yet").

## 11. Non-Functional Requirements
- NFR1 **Cost:** Hard ceiling **₹1,000** for the whole project. Gemini runs on **AI Studio Prepay** (₹1,000 credits in project `"My First Project"`, paid tier Tier 1, **auto-reload OFF**; calls fail with HTTP 402 at ₹0). The former Google Cloud ₹1,000 manual credit was refunded. The in-code ₹1,000 project cap and ₹300 per-run cap in `core/budget.py` remain mandatory. Note: the first P0 `test-call` ran on the free tier, so it actually cost ₹0 even though `spend_log.csv` shows ₹0.0247. Estimated ₹300–450; actual cost per post measured on the 20-post test and used for all further estimates.
- NFR2 **Security:** API key in `.env` locally and Streamlit Secrets in deployment (Secrets hold only the API key); `.env` in `.gitignore`. The key never goes into chat or GitHub. After 9 Nov, rotate/delete the key or turn billing off.
- NFR3 **Re-runnable:** one `config.yaml`; each stage is idempotent and resumable.
- NFR4 **Swappable LLM:** all model calls go through one `llm_client` module. Model name is a config parameter, not hardcoded.
- NFR5 **Logging:** per-stage summary (in, out, dropped, failed, time taken).
- NFR6 **Reproducibility:** prompts versioned; outputs stamped with run date and prompt version.
- NFR7 **Privacy:** no plain-text usernames, no PII in outputs, quotes kept short.

### 11.1 Budget & Approval Rules (from Guardrails Addendum v1.2)
- **Approval gate:** no Gemini call runs without a printed pre-run estimate (posts remaining, calls, input/output tokens, ₹ cost, cumulative spend, budget remaining) and the PM typing `yes`. Any other input exits cleanly. No bypass flag.
- **Test small first:** every Gemini-calling stage runs on 20 posts first; the full run needs approval of that sample's output and actual cost.
- **Caps (in `config.yaml` → `budget:`; raised only manually by the PM):** per-run ₹300 (halts and checkpoints); project ₹1,000 cumulative (refuses to start once reached); per-call sanity check (single call > 3× estimated average → stop and report).
- **Cost formula (`core/budget.py`):** `cost_usd = (input_tokens × input_price + (output_tokens + thinking_tokens) × output_price + cached_tokens × cached_input_price) / 1,000,000`; `cost_inr = cost_usd × usd_inr_rate`. Thinking tokens bill as output. Prices (USD per 1M): input 0.75, output 3.75, cached input 0.075; `usd_inr_rate` 96. Confirm prices in P0.
- **Spend log:** `outputs/spend_log.csv`, one row per call: `timestamp, stage, batch_id, model, prompt_version, input_tokens, output_tokens, thinking_tokens, cached_tokens, cost_inr, cumulative_cost_inr, retry_count, call_status`.
- **Efficiency & HTTP 402 stop:** batch 25–40 posts per call; retries max 3 per batch (on 429/5xx only); one JSON-repair retry then `enrichment_failed`; hard stop beats retry. On **HTTP 402** (prepay credits exhausted at ₹0), `llm_client` does not retry — it logs `error_402`, checkpoints, and exits cleanly with `"Gemini prepay credits exhausted — top up in AI Studio"`.
- **Cost estimate:** Estimated ₹300–450; actual cost per post measured on the 20-post test and used for all further estimates.
- **Tests:** unit tests use mocked LLM responses only; the real API is never called in tests.

## 12. Constraints and Guardrails
- No fund or stock recommendations anywhere in the outputs or UI.
- No fear, FOMO or urgency framing in any generated text.
- No personal data (PAN, account numbers, portfolio screenshots).
- Respect source terms of service and rate limits; use official APIs where available.
- Evidence quotes must be verbatim spans from source posts, never LLM-paraphrased.
- LinkedIn is treated as manual-only (ToS); no programmatic scraping attempted.

## 13. Acceptance Criteria

| # | Criterion | Pass condition |
|---|---|---|
| AC1 | Collection | ≥ 4 sources collected programmatically; every record has all FR1.3 fields |
| AC2 | Volume | 3,000–4,000 posts after cleaning (up to 5,000 only if the 20-post cost test shows it fits under ₹1,000), or a documented shortfall with reasons (including App Store cap if relevant) |
| AC3 | Drop log | Every removal counted by reason and source, including `source_cap` entries |
| AC4 | Enrichment validity | ≥ 95% of posts produce schema-valid JSON |
| AC5 | Evidence spans | 100% of non-unknown outcome/trigger tags have an `evidence_span` confirmed verbatim in the post by the validation script |
| AC6 | Continuer floor | Met (≥ 15% or ≥ 300 posts), or flagged with targeted `continuer_keywords` re-collection attempted |
| AC7 | Question coverage | All 9 research questions have an output in the Insights tab |
| AC8 | Opportunity cards | ≥ 5 ranked cards, each with a score breakdown and ≥ 3 linked quotes |
| AC9 | Golden set | 20 items hand-labelled; agreement ≥ 70% overall and ≥ 75% on `outcome` |
| AC10 | Safe fallback | Live Analysis declines off-topic, low-confidence and fund-recommendation inputs (test with 5 such inputs) |
| AC11 | Deployment | Public Streamlit link works in an incognito window, with no login |
| AC12 | Guardrails | Zero fund/stock recommendations in UI or exports (manual spot check of all cards) |
| AC13 | Budget guard | Pipeline halts and checkpoints at the per-run cap in a simulated test (mock costs) |
| AC14 | Approval gate | No Gemini call runs without a printed estimate and a typed `yes`; any other input exits cleanly |
| AC15 | Thinking budget | Tagging calls (Enrich, Live Analysis) log ~0 `thinking_tokens` in `spend_log.csv` (from `usage_metadata.thoughts_token_count`) |
| AC16 | Spend log | Every Gemini call has a valid row in `spend_log.csv`; cumulative total ≤ ₹1,000 |
| AC17 | Demo caps | Live Analysis enforces 3/session and 50/day (tested with mock counters) |
| AC18 | Cost formula | Unit test confirms `cost = input × input_price + (output + thinking) × output_price + cached × cached_price`, converted at `usd_inr_rate` |

## 14. Validation Plan
1. **Stage checks:** after each stage, spot-check 20 random records by hand.
2. **Schema validation:** automated JSON schema check on every enriched record.
3. **Evidence check:** script confirms each `evidence_span` appears verbatim in its post (`span in text`). Failures are auto-retried once; persistent failures marked `evidence_span_failed`.
4. **Golden set:** blind human labelling → per-field agreement (AC9).
5. **Sanity checks:** source distribution; warn if any source > 40% of cleaned corpus (hard cap enforced by FR1.7); segment cuts report n.
6. **Cost check:** after every Gemini run, report actual vs estimated cost; stop and report if cost per call is ≥ 2× the estimate.
7. **Correction log:** record 2–3 places where AI output was rejected or corrected, and why (for the Evidence Log).

## 15. Dependencies
- Gemini API key (paid tier Tier 1 in `"My First Project"`, AI Studio Prepay with ₹1,000 credits and auto-reload OFF) — `gemini-3.6-flash` ID verified via `models.list` in P0.
- Reddit public `.json` / `.rss` endpoints (no OAuth credentials required; PRAW replaced due to Reddit API approval gate).
- YouTube Data API v3 key (Google Cloud, free 10,000 units/day).
- Python libraries: `google-play-scraper`, `requests`, `google-api-python-client`, `google-genai`, `pandas`, `rapidfuzz`, `pydantic`, `streamlit`.
- GitHub repo (public) + Streamlit Community Cloud account.
- Manual seed CSV files for Priority 3 sources (Quora, LinkedIn, TradingQ&A, ValuePickr, Freefincal).

## 16. Risks and Blockers

| Risk | Impact | Mitigation |
|---|---|---|
| Paid-tier cost overrun | Exceeds ₹1,000 ceiling | AI Studio Prepay (₹1,000 credits, auto-reload OFF → hard HTTP 402 stop at ₹0), approval gate, 20-post test first, per-run ₹300 / project ₹1,000 caps in code |
| Thinking tokens inflate cost silently | Unexpected spend | Thinking set to lowest allowed level; `thoughts_token_count` logged per call; warning if > 10% of output tokens |
| Public demo drains budget | Unexpected spend | 3/session + 50/day caps, 2,000-char input cap, single call per analysis, project cap backstop |
| Reddit/YouTube API access delays | Missing priority sources | Apply for keys on day 1; fall back to JSON endpoints or manual export |
| App Store volume capped at ~500 | Lower total corpus | Accepted; logged in drop log; compensate with Reddit and manual seeds |
| Few posts state segment signals | Weak segment analysis | Allow unknown; report coverage honestly; validate segment in interviews |
| Continuer posts are rare | Lopsided comparison | Continuer floor (FR4.7) + `continuer_keywords` group pre-loaded |
| LLM hallucinates tags or paraphrases evidence_span | Wrong insights | Evidence span verbatim check + auto-retry + golden set |
| Source dominance | Skewed themes | Hard 40% cap (FR1.7); excess archived and logged |
| Scraping blocked | Lost sources | Manual CSV seeds (Priority 3); LinkedIn manual-only from the start |
| Timeline (engine due 30 Oct) | No time for interviews | P0 starts immediately; stages run in parallel where possible |

## 17. Open Questions
- Exact model ID string for `gemini-3.6-flash` (verify via `models.list` in P0).
- Current pricing (confirm at `ai.google.dev/gemini-api/docs/pricing` in P0).
- Thinking-control parameter name in the current `google-genai` SDK (`thinking_level` vs `thinking_budget`) and its lowest allowed value. If no control works, stop and tell the PM.

## 18. Implementation Phases

| Phase | Work | Target date |
|---|---|---|
| P0 | Repo setup, config.yaml (both keyword groups), .env template, llm_client module, Pydantic schemas, manual seed CSV template | 24 Oct *(revised; was 23 Oct)* |
| P1 | Collect: Play Store + App Store (accept cap, log shortfall) | 24 Oct |
| P2 | Collect: Reddit (public `.json` / `.rss` endpoints across 8 subreddits, no login) | 25 Oct |
| P3 | Collect: manual CSV seeds (Quora, LinkedIn, TradingQ&A, ValuePickr, Freefincal) | 24 Oct |
| P4 | Collect: YouTube comments | 25 Oct |
| P5 | Collect: TradingQ&A Discourse JSON collector + official context data (`data/context/`) | 26 Oct |
| P6 | Clean (rules-only, no LLM → FR1.7 source cap → drop log) | 26 Oct |
| P7 | Enrich (Flash, checkpoint/resume, evidence_span validation) | 26–27 Oct |
| P8 | Analyse + Surface (pandas stats → Flash synthesis → deterministic scoring) | 28 Oct |
| P9 | Evaluate (golden set proposal → PM labels → compare mode) | 29 Oct |
| P10 | Streamlit UI + deploy to Streamlit Community Cloud | 30 Oct |

## 19. Deliverables
- Public Streamlit link (discovery engine).
- GitHub repo with README and `/prompts` directory (versioned prompts).
- Exports: `enriched_posts.jsonl`, `analysis.json`, `opportunities.json`, `opportunities.csv`, `golden_set.csv`, `golden_engine.csv`, `eval_report.json`, `drop_log.csv`, `spend_log.csv`.
- PM hand labels: `data/seeds/golden_human.csv`.
- Manual seed template: `/data/templates/manual_seed_template.csv`.
- Inputs for the Evidence Log: source volumes, App Store shortfall note, golden set results, failure cases, correction log (2–3 entries), actual spend vs estimate.
