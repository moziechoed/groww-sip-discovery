# Architecture: Groww SIP Continuation Discovery Engine

| Field | Value |
|---|---|
| Version | v1.1 |
| Aligned to | PRD v1.2 |
| Stack | Python 3.11+, Gemini 3.6 Flash (paid tier Tier 1, AI Studio Prepay), Streamlit |
| Hosting | Streamlit Community Cloud + GitHub |
| Cost | Hard ceiling ₹1,000 (AI Studio Prepay ₹1,000 credits, auto-reload OFF → HTTP 402 at ₹0; in-code caps mandatory). Estimated ₹300–450; actual cost per post measured on the 20-post test and used for all further estimates. |

> **Changelog: v1.0 → v1.1**
>
> - **Stack & Cost:** Upgraded from Gemini Flash free tier to Gemini 3.6 Flash paid tier. Cost ceiling changed from ₹0 to ₹1,000 hard cap.
> - **Principle 6:** Replaced "Free-tier first" with "Budget-capped" principle; every Gemini call is estimated, logged, and charged against a hard ceiling enforced in code.
> - **Repo:** Added `core/budget.py`, `outputs/spend_log.csv`, `data/seeds/golden_human.csv`, `prompts/archive/offtopic_v1.txt`. Removed top-level `golden_set_engine_labels.json` reference.
> - **Config:** Full config rewrite with `budget:` block, `scoring.specificity_thresholds`, updated `llm:` params (`batch_size: 30`, `thinking_budget_tagging: 0`), `target_max: 4000`, continuer keyword list, and per-source priorities.
> - **LLM Client:** Added `thinking_budget`, `usage logging`, and `budget integration` responsibilities. New providers must report token usage to `budget.py`.
> - **Stage 2 (Clean):** Step 7 changed — off-topic filter is now keyword-rules only; no Gemini call in this stage. Borderline posts kept with `clean_borderline=true`. `offtopic_v1.txt` archived.
> - **Stage 3 (Enrich):** Added `is_relevant` field (determined during enrichment, no extra call). Added `clean_borderline` as a pass-through context field.
> - **Stage 4 (Analyse):** All analysis restricted to `is_relevant=true` subset. Irrelevant posts counted in `corpus_stats.json` but excluded from distributions.
> - **Stage 5 (Surface):** Behavioural Specificity scoring made fully deterministic (no LLM). Actionability scoring is LLM-rated (Flash, one call per cluster) with rubric anchors and a rationale field. Thresholds moved to `config.yaml`.
> - **New §4.11:** Approval Gate added — `run.py` prints a pre-run estimate and blocks on `input('yes')` before every Gemini-calling stage.
> - **Data Contracts:** `CleanPost` gains `clean_borderline: bool`. `EnrichedPost` gains `is_relevant: bool` and `clean_borderline: bool`. `Opportunity` gains `actionability_rationale: str`.
> - **§6 Throughput:** Example calculation updated to reflect paid-tier batch sizes and cost estimates.
> - **§8 Error Handling:** Added `BudgetExceeded` row. Updated 429 row to reference budget ceiling.
> - **§10 Testing:** Added AC13–AC17 tests: `test_budget_halt`, `test_approval_gate`, `test_thinking_tokens`, `test_spend_log_schema`, `test_demo_caps_session`, `test_demo_caps_daily`.
> - **§11 Deployment:** Added deployment guidance for the daily demo counter (superseded by the 24 Oct fix below: counter is file-backed, not in Secrets).
> - **§12 Build Sequence:** P0 expanded to include API key confirmation, pricing confirmation, SDK parameter confirmation, `budget.py` implementation, and approval gate.
>
> **Post-review fixes (24 Oct, still v1.1):**
> - **§6:** every clean post is enriched (`is_relevant` is decided inside the call): 4,000 ÷ 30 ≈ 134 enrichment calls. Cost: "Estimated ₹300–450; actual cost per post measured on the 20-post test and used for all further estimates."
> - **§4.10 / §11:** daily demo counter moved to a file-backed store via `st.cache_resource` (Secrets are read-only). Secrets hold only the API key. Main key used for the demo; no separate key. Safety check + enrichment merged into one call (Guardrails §7).
> - **New §4.12:** `core/budget.py` cost formula, caps and spend log. New test `test_cost_formula` (PRD AC18).
>
> **P1 kickoff updates (25 Oct, still v1.1):**
> - **§4.1 / config.yaml:** `paths.raw_dir`, `paths.clean_dir`, `sources.app_store.app_id: "1404871703"` (verified via iTunes Lookup), Play Store star stratification settings (`max_scan_per_star: 12000`, raised from 4,000 after the 200-review live test showed a ~4% match rate), new `collect:` block, and `keywords.anchors` / `keywords.hinglish` for the app-store matcher. `config.yaml` in the repo is the source of truth for exact values.
> - **§4.3:** per-source files are collated into `data/raw/raw_posts.jsonl`; matcher rules, Play Store stratification, App Store text format and resume behaviour documented. Language filtering stays in Clean (FR2.4) so drops appear in the drop log.
>
> **P2 kickoff updates (25 Oct, still v1.1):**
> - **§4.1 / §4.3 (Reddit):** Replaced `praw` OAuth with public no-login `.json` / `.rss` (Atom XML) endpoints (`auto` fallback to `.rss` on HTTP 403 WAF blocks). Expanded subreddits from 5 to 8 (`IndiaInvestments`, `personalfinanceindia`, `MutualfundsIndia`, `FIREIndia`, `IndianStreetBets`, `StockMarketIndia`, `IndianStockMarket`, `IndiaTax`).
> - **§4.3 (Reddit comments & caps):** Two-phase resumable collection (search discovery across query × sort × time slices, then per-thread comment harvesting). Comments inside a SIP-anchored thread match either directly (`raw_meta.matched_via = "self"`) or via `core`/`continuer`/`hinglish` phrases (`raw_meta.matched_via = "thread_context"`). Capped at 30 matched comments per thread (logged as `thread_comment_cap`). Per-thread `comments_returned` vs `num_comments` (where known) logged in `corpus_stats.json`.
> - **§4.3 (P2 post-test tuning):** Base request delay raised to `10s`; HTTP 429 cooldown enforces a `60s` minimum floor (`max(60s * 2^attempt, Retry-After, x-ratelimit-reset)`) and waits the full reset window before the next request. Search discovery prioritizes `sort=new` across all queries first, drops `relevance/all`, and stops any `(query, sort)` slice immediately once a page is `>80%` out of window (`out_of_window_page_ratio_stop: 0.80`).
>
> **Billing update (26 Oct, still v1.1):**
> - **Header / §4.2 / §4.12 / §8:** Switched billing from Google Cloud Postpay to **AI Studio Prepay** (₹1,000 credits in project `"My First Project"`, paid tier Tier 1, auto-reload OFF; calls fail with HTTP 402 at ₹0). The Google Cloud ₹1,000 manual credit was refunded. Note that the first P0 `test-call` ran on the free tier (₹0 actual charge, though `spend_log.csv` shows ₹0.0247).
> - **§4.2 / §8 (`llm_client.py`):** HTTP 402 is treated as a non-retryable clean stop: logs `error_402`, checkpoints, and exits with `"Gemini prepay credits exhausted — top up in AI Studio"`.
>
> **P4 kickoff updates (26 Oct, still v1.1):**
> - **§4.1 / §4.3 (YouTube):** Expanded channels from 5 to 10 verified handles (`Groww`, `CARachanaRanade`, `LabourLawAdvisor`, `warikoo`, `pranjalkamra`, `ETMONEY`, `VarsitybyZerodha`, `AkshatZayn`, `nehanagar`, `shankarnath`; `@shashankudupa` dropped with 0 in-window videos). Uses 1-unit uploads playlist scans (`playlistItems.list`) + 6 targeted `search.list` calls, strict SIP/MF anchor video filtering (`title` or `description[:250]`), `commentThreads.list(part="snippet,replies")`, round-robin channel harvesting (`max_comments_per_channel: 200`), creator/promo spam filtering (retaining non-promo `"crypto"`/`"+91"` mentions), first-person + behaviour `thread_context` matching inside SIP/MF-anchored videos, 40-comment per-video cap, 1,500 total cap, and an 8,000-unit/day Pacific-Time quota guard in `collect_youtube.json`.
> - **§4.3 (`AUTHOR_HASH_SALT`):** `require_author_salt()` refuses to start collection when `AUTHOR_HASH_SALT` is missing or a placeholder.
>
> **P5 kickoff updates (26 Oct, still v1.1):**
> - **§3 / §4.1 / §4.3 (TradingQ&A Discourse collector):** Added `collectors/tradingqna.py` writing to `data/raw/tradingqna.jsonl` (resumable via `data/checkpoints/collect_tradingqna.json`). Uses public Discourse `/search.json` + `/t/{id}.json` (no login) with 12 single-phrase SIP queries (`request_delay_sec: 6.0`, `rate_limit_cooldown_sec: 60.0`), strips `<aside class="quote">...</aside>` from replies, applies the shared `match_anchored_reply()` (including first-person + behaviour `thread_context` matching) inside SIP/MF-anchored topics, caps replies at `max_replies_per_topic: 20` (`like_count DESC, created_at ASC`) and `max_matched_total: 400`, and sets `raw_meta.platform_tag = "zerodha"`.
> - **§3 / §4.3 (Context Data — `data/context/`, `--stage context`):** Added `collectors/context_data.py` (isolated from `data/raw/raw_posts.jsonl` and never entering Clean/Enrich; 0 LLM calls). Generates:
>   1. `data/context/amfi_sip_monthly.csv`: last 24 months of SIP figures + 3 FY summary rows parsed from `https://www.amfiindia.com/mutual-fund` with `source_url` per row.
>   2. `data/context/factbook_demographics.csv`: investor demographic tables (age bands, gender, T30/B30 city tier, holding period, state-wise age allocation) extracted from the AMFI–Crisil Factbook 2026 PDF (`https://www.amfiindia.com/uploads/AMFI_Factbook2026_14b34a6447.pdf`) with `pdf_page` and `confidence` (`high` | `low`).
>   3. `data/context/mfsh_index.csv`: curated official-source fallback index discovered strictly from `https://www.mutualfundssahihai.com/sitemap.xml` (`sitemap-0.xml`), verified `http_status == 200`, with `summary` taken verbatim from each page's `<meta name="description">`, `og:description`, or lead paragraph.
>
> **P6 kickoff updates (26 Oct, still v1.1):**
> - **§4.4 (`pipeline/clean.py`):** Implemented rules-only Stage 2 (Clean) with zero LLM calls:
>   - Drops posts where `>50%` of alphabetic characters are in **any** non-Latin script (Devanagari, Tamil, Telugu, Kannada, Malayalam, Bengali, Gujarati, etc.) and logs drops per script and source in `corpus_stats.json`.
>   - Scrubs PII (`[EMAIL]`, `[UPI]`, `[PAN]`, `[PHONE]`, `[ACCOUNT_NUM]`) in-place and writes **only** the masked text to both `text` and `clean_text` in `data/clean/clean_posts.jsonl` so unmasked text never leaves `data/raw/`. `outputs/drop_log.csv` records dropped posts only; PII mask counts by type go to `outputs/corpus_stats.json`.
>   - Uses `rapidfuzz.fuzz.token_sort_ratio >= 90` (whole-text similarity with a `>=0.85` length-ratio guard) rather than `token_set_ratio`.
>   - Enforces the 40% per-source cap and clamps total corpus at `target_max = 4,000` using square-root damping ($\propto \sqrt{n_{m,s}}$) across `(YYYY-MM, subreddit)` strata, ranked by `quality_tier`. Stores `raw_meta.quality_tier` and `raw_meta.stratum` on every kept Reddit record, archives excess to `data/clean/source_cap_overflow.jsonl`, and logs before/after theme-keyword counts in `outputs/corpus_stats.json`.
>
> **P7 Enrich updates (27 Oct, still v1.1):**
> - **New modules:** `pipeline/enrich.py` (Stage 3), `pipeline/golden.py` (`--stage golden-sample`, 0 LLM calls), `scoring/segment_fit.py`, prompt `prompts/enrich_v1_20261027.txt`, `docs/labelling_guide.md` (definitions copied verbatim from the prompt).
> - **`core/llm_client.py`:** provider records `finish_reason`; a `MAX_TOKENS` reply returns status `max_tokens` (no repair retry) so Enrich splits the batch. `generate_json(parse_schema=...)` lets Enrich send the strict schema to Gemini but validate items one by one.
> - **`core/schemas.py`:** `kpi_branch`, `stop_scenario_ids` (1–17, max 3, out-of-range dropped), `month_12_plus`, `EnrichmentBatchResponse`, `propertyOrdering` on `EnrichmentFields`, `EnrichedPost.unverified_labels`.
> - **Config:** new `enrich:` block (prompt file, output floor 350 tokens/post, headroom 1.5, test size 20, golden sizes 12 + 8, `require_golden_labels_before_full_run: true`); `paths.enriched_dir`, `paths.seeds_dir`.
> - **Outputs:** `data/enriched/enriched_posts.jsonl` (gitignored), `data/checkpoints/enrich.json` + `enrich_calibration.json`, `outputs/corpus_stats.json["enrich"]` (`last_test` / `last_full`: schema-valid %, evidence %, kpi_branch and scenario distributions, tokens and ₹ per post).
> - **Tests:** `tests/test_enrich.py`, `tests/test_golden_sample.py`, `tests/test_segment_fit.py` (all mocked; 113 tests total).

> Note: Phase dates in the PRD are indicative only. Build sequence matters; dates are flexible up to the 9 Nov submission.

---

## 1. Architecture Principles
1. **Offline pipeline, online viewer.** All heavy work (collect → evaluate) runs locally. The deployed app only reads precomputed files plus one live single-post call.
2. **Stages are independent and file-based.** Each stage reads one file and writes one file. Any stage can be re-run without touching the others.
3. **Idempotent and resumable.** Every stage checkpoints. A crash or rate-limit stop resumes where it left off.
4. **One door to the LLM.** All model calls go through `llm_client`. Swapping Gemini for another model changes one file.
5. **Evidence over inference.** Every tag must point to verbatim text. Unknown is a valid answer.
6. **Budget-capped: cheapest path that meets quality gates.** Every Gemini call is estimated before it runs, logged after, and charged against a ₹1,000 hard project ceiling enforced in code.

## 2. System Overview

```
                ┌─────────────────────── LOCAL MACHINE (offline pipeline) ───────────────────────┐
                │                                                                                │
 Sources ──►  [1 Collect] ─► raw/ ─► [2 Clean] ─► clean/ ─► [3 Enrich] ─► enriched/              │
 (Play, App,     │                      │                       │  ▲                            │
  Reddit, YT,    │                      ▼                       │  └── llm_client ◄── Gemini    │
  CSV seeds)     │                  drop_log.csv                ▼                     Flash API  │
                │                                         [4 Analyse] ─► analysis/              │
                │                                               │                               │
                │                                               ▼                               │
                │                                         [5 Surface] ─► outputs/opportunities  │
                │                                               │                               │
                │                                               ▼                               │
                │                     human labels ──►  [6 Evaluate] ─► outputs/eval_report     │
                └───────────────────────────────────────────────┬────────────────────────────────┘
                                                                │ git push (outputs only)
                                                                ▼
                ┌──────────────── STREAMLIT CLOUD (public) ─────────────────┐
                │  app.py reads outputs/*.json|csv                           │
                │  Tabs: Overview · Opportunities · Insights · Evaluation ·  │
                │        Live Analysis · Method                              │
                │  Live Analysis ──► llm_client ──► Gemini Flash (1 post)    │
                └────────────────────────────────────────────────────────────┘
```

## 3. Repository Structure

```
groww-sip-discovery/
├── README.md
├── requirements.txt
├── config.yaml
├── .env.example
├── .gitignore
├── run.py
│
├── core/
│   ├── config.py
│   ├── llm_client.py
│   ├── schemas.py
│   ├── checkpoint.py
│   ├── budget.py           # token + cost tracker; enforces per-run (₹300) and project (₹1,000) caps
│   ├── logger.py
│   └── utils.py
│
├── collectors/
│   ├── base.py
│   ├── matcher.py
│   ├── runner.py
│   ├── play_store.py
│   ├── app_store.py
│   ├── reddit.py
│   ├── youtube.py
│   ├── tradingqna.py
│   ├── context_data.py
│   └── csv_seed.py
│
├── pipeline/
│   ├── clean.py
│   ├── enrich.py
│   ├── analyse.py
│   ├── surface.py
│   └── evaluate.py
│
├── scoring/
│   ├── segment_fit.py
│   └── opportunity_score.py
│
├── prompts/
│   ├── enrich_v1.txt
│   ├── cluster_v1.txt
│   ├── card_writer_v1.txt
│   ├── live_guard_v1.txt
│   └── archive/
│       └── offtopic_v1.txt
│
├── data/
│   ├── seeds/
│   │   └── golden_human.csv    # PM's hand labels for golden set
│   ├── context/                # Official context CSVs (AMFI monthly, Factbook demographics, MFSH index; never enters Clean/Enrich)
│   ├── raw/
│   ├── clean/
│   ├── enriched/
│   └── checkpoints/
│
├── outputs/
│   ├── corpus_stats.json
│   ├── drop_log.csv
│   ├── analysis.json
│   ├── opportunities.json
│   ├── opportunities.csv
│   ├── golden_set.csv
│   ├── golden_engine.csv       # engine labels (hidden from labeller until compare mode)
│   ├── eval_report.json
│   ├── enriched_sample.jsonl
│   └── spend_log.csv           # per-call log: tokens, cost, cumulative spend
│
├── app/
│   ├── app.py
│   ├── demo_limits.py          # 3/session + 50/day caps; file-backed counter via st.cache_resource
│   ├── pages/
│   └── components/
│
└── tests/
    ├── test_schemas.py
    ├── test_segment_fit.py
    ├── test_evidence_span.py
    ├── test_live_guard.py
    ├── test_budget_halt.py
    ├── test_approval_gate.py
    ├── test_thinking_tokens.py
    ├── test_spend_log_schema.py
    ├── test_cost_formula.py
    └── test_demo_caps.py
```

## 4. Component Design

### 4.1 Config (`config.yaml`)

```yaml
run:
  window_months: 24
  target_min: 3000
  target_max: 4000          # 5000 only if 20-post cost test confirms it fits under ₹1,000
  source_cap_pct: 40
  prompt_version: v1

keywords:
  core: ["SIP stopped","paused SIP","cancel SIP","first SIP","SIP missed","SIP returns",
         "Groww SIP","started investing","mutual fund beginner","SIP worth it",
         "SIP band","SIP ruk"]
  continuer: ["still going","1 year SIP","didn't stop SIP","continued SIP",
              "SIP anniversary","never stopped","kept my SIP","SIP chalu hai","band nahi kiya"]
  use_continuer_group: false

sources:
  play_store:  {enabled: true,  priority: 1, app_id: "com.nextbillion.groww"}
  reddit:      {enabled: true,  priority: 1, subs: [IndiaInvestments, personalfinanceindia, MutualfundsIndia, FIREIndia, IndianStreetBets, StockMarketIndia, IndianStockMarket, IndiaTax]}
  app_store:   {enabled: true,  priority: 2, app_id: "1404871703", cap_note: "RSS ~500"}   # ID verified 25 Oct
  youtube:     {enabled: true,  priority: 2, channels: [GrowwOfficial, CARachanaRanade, LabourLawAdvisor, warikoo, pranjalkamra]}
  csv_seed:    {enabled: true,  priority: 3, path: data/seeds/}
  twitter:     {enabled: false}

llm:
  provider: gemini
  model: "gemini-3.6-flash"    # verify exact ID via models.list in P0
  batch_size: 30               # tune on 20-post test; range 25–40
  max_retries: 3
  backoff_base_sec: 2
  inter_batch_pause_sec: 4
  thinking_budget_tagging: 0   # map to SDK param (thinking_level or thinking_budget) in llm_client.py

live:
  session_call_limit: 3
  daily_demo_cap: 50
  min_confidence: medium
  max_input_chars: 2000

budget:
  per_run_cap_inr: 300
  project_cap_inr: 1000
  usd_inr_rate: 96
  input_price_per_m_usd: 0.75          # confirm at ai.google.dev/gemini-api/docs/pricing in P0
  output_price_per_m_usd: 3.75         # confirm at build time
  cached_input_price_per_m_usd: 0.075  # confirm at build time
  thinking_budget_tagging: 0
  max_retries: 3
  require_approval: true

scoring:
  specificity_thresholds:
    high: 0.6    # share of cluster posts with verbatim evidence_span AND non-unknown checking_what → score 3
    medium: 0.3  # → score 2; below medium → score 1

paths:           # added in P1 (abridged; see config.yaml)
  raw_dir: data/raw
  clean_dir: data/clean
```

### 4.2 LLM Client (`core/llm_client.py`)
The only module that talks to a model.

| Responsibility | Behaviour |
|---|---|
| `generate_json(prompt, schema)` | Calls Gemini in JSON mode, validates against pydantic schema |
| Retry | Exponential backoff on 429/5xx: 2s, 4s, 8s, 16s, 32s |
| Prepay exhausted (HTTP 402) | Non-retryable clean stop; logs `error_402`, raises `LLMUnavailable` with `"Gemini prepay credits exhausted — top up in AI Studio"` so caller checkpoints and exits immediately |
| Invalid JSON | One repair retry with the error message appended; then mark `enrichment_failed` |
| Quota guard | Tracks calls per minute/day; sleeps or stops cleanly with a checkpoint before limits hit |
| Logging | Calls, tokens (if returned), failures, latency |
| `thinking_budget` | Maps `thinking_budget_tagging` from config to the SDK parameter (`thinking_level` or `thinking_budget` — confirm in P0 from google-genai SDK docs). Set to the lowest allowed value for all tagging calls. |
| `usage logging` | Logs `usage_metadata.thoughts_token_count` on every call. If thinking tokens exceed 10% of output tokens, logs a WARNING and reports to `budget.py`. |
| `budget integration` | After every call, reports `(input_tokens, output_tokens, thinking_tokens, cached_tokens, cost_inr)` to `core/budget.py`. If the per-run cap is reached, checkpoints and raises `BudgetExceeded`. |
| Swappable | `provider` in config; new providers implement the same interface and must also report token usage in the same format to `budget.py`. |

### 4.3 Collectors (Stage 1)
- All extend `BaseCollector` with `collect() -> Iterator[RawPost]`.
- Each collector runs in isolation; failures are logged and skipped (FR1.5).
- Usernames hashed with SHA-256 + salt at collection (FR1.4).
- Keyword filter + 24-month window applied at collection to save volume.
- Writes `data/raw/{source}.jsonl` and appends counts to `corpus_stats.json`.
- After the requested sources finish, all per-source files are collated (deduped by `id`) into `data/raw/raw_posts.jsonl`, the input to Clean.

| Collector | Library / method | Key notes |
|---|---|---|
| play_store | `google-play-scraper` | Sort by newest, paginate, filter by keywords + date |
| app_store | iTunes RSS customer reviews (JSON) | Accept ~500 cap; log shortfall |
| reddit | Public `.json` / `.rss` (Atom XML) via `requests` | No login/OAuth; search per sub × query × sort/time slice + thread comments (≤30 matched/thread); auto-falls back from `.json` to `.rss` on 403 |
| youtube | YouTube Data API v3 | Channel uploads playlist scan + 6 targeted searches; round-robin channel harvesting; pull comment threads (≤40/video, ≤200/channel, ≤1,500 total) |
| tradingqna | Public Discourse JSON API (`/search.json` + `/t/{id}.json`) via `requests` | 12 single-phrase SIP queries; strips `<aside class="quote">`; ≤20 replies/topic, ≤400 matched total; `raw_meta.platform_tag = "zerodha"` |
| context_data | `requests` + `pypdf` (`python run.py --stage context`) | Writes `amfi_sip_monthly.csv`, `factbook_demographics.csv`, and `mfsh_index.csv` under `data/context/` (isolated from `raw_posts.jsonl`) |
| csv_seed | `pandas` | Required columns: source, url, date, text |

**App-store specifics (P1):**
- **Matcher:** whole-word, case-insensitive regex. `keywords.anchors` (SIP/MF terms) match on their own. Phrases from `core`, `continuer` and `hinglish` that don't contain an anchor (e.g., "band kar diya", "paisa kat", "still going") count only if an anchor also appears in the same review. `keyword_group = "continuer"` when any continuer phrase counts. Matched terms stored in `raw_meta.matched_keywords`.
- **Play Store:** newest-first, stratified by star rating (1–5), pages fetched round-robin across stars; each star stops at `max_scan_per_star` (12,000; raised from 4,000 after the capped test run showed a ~4% match rate) or the 24-month cutoff (after 20 consecutive out-of-window reviews); collection stops at `max_matched_total` (2,500). Per-star scanned vs matched counts go to `corpus_stats.json` (stratification changes the natural rating mix; disclosed in the Evidence Log). `text` = review body only (developer replies excluded).
- **App Store:** RSS pages 1–10 (`sortby=mostrecent`); `text` = `"{title}. {content}"` unless the title is already in the body; title also kept in `raw_meta.title`. Shortfall vs the ~500 cap and the oldest review date are logged.
- **Resume:** per-source state in `data/checkpoints/collect_{source}.json` (Play continuation tokens, per-star counters; App Store pages done). Already-written IDs are read from the per-source JSONL, so re-runs never duplicate rows.
- **Retries:** timeouts, 429, 5xx and silent empty pages retry with backoff (2s, 4s, 8s); a source that still fails is logged and skipped (FR1.5).
- **Language:** not filtered here (beyond `lang=en`, `country=in`); Clean applies FR2.4 so every drop is in the drop log.

**Reddit specifics (P2):**
- **Transport:** Tries `.json` first; if Reddit's WAF returns HTTP 403 (`"You've been blocked by network security"`), switches automatically to `.rss` (Atom XML) for the remainder of the run. Uses a persistent `requests.Session()` with a polite delay (~10s per request) and exponential cooldown on HTTP 429 (`max(60s * 2^attempt, Retry-After, x-ratelimit-reset)`, never `<60s`), waiting the full reset window after a 429 before the next request.
- **Two-phase resumable state (`data/checkpoints/collect_reddit.json`):**
  1. *Discovery:* Paginates `(subreddit, sort, query, t)` streams (`after=t3_...`), prioritizing `sort=new` across all queries first and stopping any `(query, sort)` slice as soon as a page is `>80%` out of window (`out_of_window_page_ratio_stop: 0.80`). Writes matched posts (`reddit:t3_...`) and queues in-window thread IDs into `pending_threads` (deduped across queries/sorts). Removed/deleted post bodies are skipped, except if a removed post's title matched in-window, its thread is still queued for comment harvesting.
  2. *Thread comment harvesting:* Fetches `/r/{sub}/comments/{id}/.{json|rss}?limit=500` for each queued thread. Filters out `[deleted]`/`[removed]` bodies and bots/moderators (`AutoModerator`, `*bot`, `"I am a bot"`), while keeping intact comments from `[deleted]` authors.

**YouTube specifics (P4):**
- **Discovery & Quota Guard (`data/checkpoints/collect_youtube.json`):** Resolves the 10 verified channels via `channels.list` (1 unit each, cached), scans their uploads playlists via `playlistItems.list` (1 unit per 50 videos, up to 10 pages/channel, stopping after 20 consecutive out-of-window uploads), and runs 6 targeted `search.list` queries (100 units each). Tracks daily quota units by Pacific Time date and stops cleanly at `daily_quota_cap_units: 8000` or on HTTP `403 quotaExceeded` / `dailyLimitExceeded`.
- **Strict Video Anchor Filter:** Queues a video only if its title or lead description (`description[:250]`, before creator boilerplate link footers) matches an explicit SIP/MF anchor via `KeywordMatcher.has_anchor()`.
- **Comments, Replies & Caps:** Fetches `commentThreads.list(part="snippet,replies", maxResults=100, order="relevance")` (up to 3 pages/video, 1 unit/page), rotating **round-robin across channels** so no single channel starves the others. Drops creator self-comments/replies (`authorChannelId == channel_id`), timestamp-only `<3`-word noise, and promo/scam spam (retaining genuine `"crypto"` / `"+91"` mentions that have no solicitation cues). Inside a SIP/MF-anchored video, comments match via `matcher.match_with_context(..., thread_has_anchor=True)` OR when they contain both a whole-word first-person marker (`i`, `my`, `mine`, `me`, `maine`, `mera`, `meri`, `mujhe`) and a behaviour term (`stop/stopped`, `pause/paused`, `cancel`, `continue/continuing`, `started`, `withdraw`, `redeem`, `returns`, `loss`, `profit`, `salary`, `emergency`, `months`, `years`, `invest/investing`) — tagged `matched_via = "thread_context"`. Keeps at most 40 matched comments per video (`like_count DESC, publishedAt ASC`), 200 matched comments per channel (`max_comments_per_channel: 200`, logged in `corpus_stats.json`), and 1,500 total matched comments. Skips `403 commentsDisabled` / `404` cleanly.
- **Salt Enforcement (`AUTHOR_HASH_SALT`):** `require_author_salt()` refuses to start any collection run if `AUTHOR_HASH_SALT` is missing, `<8` chars, or set to a placeholder.
- **Comment matching & per-thread cap:** Comments match directly (`raw_meta.matched_via = "self"`) or, when the parent thread has a SIP/MF anchor, if the comment matches any `core`, `continuer`, or `hinglish` phrase (`raw_meta.matched_via = "thread_context"`). Capped at 30 matched comments per thread (highest score first when available, else earliest timestamp); excess logged as `thread_comment_cap`. Logs `comments_returned` vs `num_comments` (where known) in `corpus_stats.json`.

**TradingQ&A specifics (P5 Part 1 — `collectors/tradingqna.py`):**
- **Discovery & Harvesting (`data/checkpoints/collect_tradingqna.json`):** Queries `https://tradingqna.com/search.json?q={query}&page={page}` across 12 single-phrase SIP queries (up to 2 pages/query; `request_delay_sec: 6.0`, `rate_limit_cooldown_sec: 60.0` floor). Queues in-window topics into `topic_queue` and fetches `/t/{topic_id}.json`.
- **Quote Stripping & Reply Matching:** Strips `<aside class="quote">...</aside>` blocks from Discourse `cooked` HTML before matching so quoted OP text never triggers false matches. Inside SIP/MF-anchored topics, replies use `match_anchored_reply()` (combining `KeywordMatcher.match_with_context` and the first-person + behaviour rule, tagged `matched_via = "thread_context"`).
- **Caps & Metadata:** Caps kept replies at `max_replies_per_topic: 20` (`like_count DESC, created_at ASC`) and `max_matched_total: 400`. Every record sets `raw_meta.platform_tag = "zerodha"`.

**Context Data specifics (P5 Part 2 — `collectors/context_data.py`, `--stage context`):**
- **Isolation:** Writes exclusively to `data/context/*.csv` and logs under `stats["context"]` in `outputs/corpus_stats.json`. Never enters `data/raw/raw_posts.jsonl`, Stage 2 (Clean), or Stage 3 (Enrich). Zero LLM calls.
- **Outputs:**
  1. `data/context/amfi_sip_monthly.csv`: Last 24 months of monthly SIP figures + 3 FY summary rows parsed from `https://www.amfiindia.com/mutual-fund` (falls back to `verify=False` on macOS incomplete SSL chain or local cache `data/context/raw/amfi_mutual_fund.html`).
  2. `data/context/factbook_demographics.csv`: Investor demographic tables (age bands, gender, T30/B30 city tier, holding period, state-wise age allocation) extracted from `https://www.amfiindia.com/uploads/AMFI_Factbook2026_14b34a6447.pdf` (cached at `data/context/raw/amfi_crisil_factbook_2026.pdf`), with `pdf_page` and `confidence` (`high` | `low`).
  3. `data/context/mfsh_index.csv`: Official Mutual Funds Sahi Hai index discovered strictly from `https://www.mutualfundssahihai.com/sitemap.xml` (`sitemap-0.xml`), verified `http_status == 200`, with `summary` extracted verbatim from `<meta name="description">`, `og:description`, or first paragraph.

### 4.4 Clean (Stage 2)
Order of operations (each step logs drops by reason and source):
1. Normalise text (whitespace, unicode, emoji kept as-is) and drop `< 5` words / `< 20` chars or repetitive junk (`too_short_or_junk`).
2. Language filter: drop if `> 50%` of alphabetic characters are in **any** non-Latin script (Devanagari, Tamil, Telugu, Kannada, Malayalam, Bengali, Gujarati, etc.) and log drops per script and source; set `hinglish=true` if Roman-script Hindi markers are detected.
3. Spam/promo filter: referral codes, links-only, "use my code", repeated templates (`spam_promo`).
4. Off-topic filter: keyword rules only. Pure non-SIP noise is dropped (`off_topic`); borderline generic portfolio-review posts are KEPT (err toward inclusion) and flagged `clean_borderline=true`. No Gemini call is made in this stage. `offtopic_v1.txt` has been moved to `prompts/archive/`.
5. PII strip: regex mask in-place for email (`[EMAIL]`), UPI (`[UPI]`), PAN (`[PAN]`), phone (`[PHONE]`), and 9–16 digit account/folio/Aadhaar numbers (`[ACCOUNT_NUM]`). Both `text` and `clean_text` in `clean_posts.jsonl` store **only** the masked string so unmasked text never leaves `data/raw/`. PII mask counts by type are logged in `corpus_stats.json`.
6. Carry/infer `raw_meta.platform_tag` (`tradingqna` = `zerodha`, store reviews = `groww`, etc.).
7. Exact dedupe (SHA-256 of canonicalised text) and whole-text near-dupe (`rapidfuzz.fuzz.token_sort_ratio ≥ 90` with a `≥ 0.85` length-ratio guard; never `token_set_ratio`), within and across sources (`duplicate`).
8. Source cap & `target_max` clamp: clamp total corpus at `target_max = 4,000` with no single source `> 40%`. Downsample Reddit using square-root damping ($\propto \sqrt{n_{m,s}}$) across `(YYYY-MM, subreddit)` strata, ranked by `quality_tier`. Store `raw_meta.quality_tier` and `raw_meta.stratum` on every kept Reddit record, archive excess to `data/clean/source_cap_overflow.jsonl`, and log before/after theme-keyword counts in `corpus_stats.json` (`source_cap`).
9. Volume check: warn if `< 3,000` with suggested expansions.

Output: `data/clean/clean_posts.jsonl`, `data/clean/source_cap_overflow.jsonl`, `outputs/drop_log.csv`, `outputs/corpus_stats.json`.

### 4.5 Enrich (Stage 3)
- Batches of 30 posts per Gemini call (tunable via `llm.batch_size`; range 25–40 as confirmed by the 20-post cost test).
- Prompt `enrich_v1` contains: field definitions, enums, the six seeds, the rule "unknown over guessing", the rule "evidence_span must be copied verbatim", and 3 few-shot examples (one continuer, one drop-off, one ambiguous).
- Response validated per post against `EnrichedPost`.
- **Evidence verifier:** checks `evidence_span` is a substring of the post (case/whitespace-insensitive). Failure → retry once → else downgrade outcome/trigger to `unknown` and log.
- Checkpoint after every batch (`data/checkpoints/enrich.json` stores done IDs).
- Post-step: compute `segment_fit` deterministically via `scoring/segment_fit.py` (no LLM).
- Continuer floor check (PRD FR4.7): if unmet, print the instruction to flip `use_continuer_group` and re-run collect → clean → enrich for new posts only.
- **`is_relevant` field:** Flash determines during enrichment (no extra call — it's one field in the same batch response) whether the post contains first-hand SIP continuation experience. Posts where `is_relevant=false` are written to `enriched_posts.jsonl` but excluded from Stage 4 and Stage 5 analysis. This handles borderline posts that passed the Clean stage.
- **`clean_borderline` pass-through:** `clean_borderline` is a pass-through field from `CleanPost` — it is included in the enrichment prompt context so Flash can use it as a signal, but it is not re-assessed.

### 4.6 Analyse (Stage 4)
All analysis runs on the `is_relevant=true` subset of `enriched_posts.jsonl`. Posts where `is_relevant=false` are counted and reported in `corpus_stats.json` but excluded from all distributions, comparisons and segment cuts. The continuer floor (FR4.7) is checked on the `is_relevant=true` subset only.

Mostly pandas, minimal LLM.

| Analysis | Method |
|---|---|
| Continuers vs drop-offs | Group by outcome; distributions of trigger, stage, checking_frequency, outside_app_behaviour, expectation themes |
| Journey-stage breakdown | Top triggers and unmet needs per stage |
| Segment cuts | Group by each segment signal; report n; flag n < 30 |
| Co-occurrence | Count trigger × stage × checking_frequency × outcome combos; top 15 |
| Free-text themes | Flash clusters `unmet_need`, `start_reason`, `expectation` into themes (`cluster_v1`), chunked to fit context |
| Seeded vs discovered | Counts; discovered with n < 15 → Emerging Signals |
| Groww UX weighting | `in_app_confusion` analysis restricted to / weighted toward `platform_tag = groww` |

Output: `outputs/analysis.json` with one block per research question (Q1–Q9).

### 4.7 Surface (Stage 5)
1. Form opportunity clusters from `seeded_category_primary` (or promoted discovered category) × dominant journey stage × unmet-need theme.
2. Score with `scoring/opportunity_score.py`.
3. Exclude `no_fund_stock_flag = true`.
4. Select 3–5 evidence quotes per card: verbatim, ≤ 30 words, diverse sources, highest `enrichment_confidence`.
5. Flash writes the title and one-line description (`card_writer_v1`) with a guardrail: no advice, no fear/FOMO language.
6. Cards with n < 15 → labelled emerging.

**Scoring details:**

**Behavioural Specificity (deterministic, no LLM):**
Computed per cluster as the share of posts where BOTH conditions are true: (a) `evidence_span` is verified verbatim in the post text, AND (b) `checking_what` is non-unknown. This share is mapped to 1–3 using thresholds defined in `config.yaml` under `scoring.specificity_thresholds` (defaults: ≥ 0.6 → 3, ≥ 0.3 → 2, < 0.3 → 1). Normalised to 0–1.

**Actionability (LLM-rated, Flash, one call per cluster):**
Flash rates each cluster 1–3 against fixed rubric anchors stored in `card_writer_v1.txt`:
- 1: Outside product control (e.g., job loss alone, medical emergency — Groww cannot meaningfully intervene).
- 2: Partly influenceable — Groww can reduce friction or improve information, but cannot eliminate the root cause.
- 3: Groww can directly address within constraints (no fund/stock recommendations, no fear/FOMO, no urgency nudges).

Flash also outputs a one-line rationale per cluster. The score and rationale are stored in the opportunity card and shown in the UI for spot-checking. Normalised to 0–1.

Actionability calls for all clusters are batched into the pre-run estimate printed before Stage 5 begins (see Approval Gate in §4.11).

Output: `outputs/opportunities.json` and `.csv`.

### 4.8 Evaluate (Stage 6)
**Mode A: propose**
- Picks 12 stratified items (by category, stage, source) + 8 ambiguous (low confidence or conflicting fields).
- Writes `golden_set.csv` (post text + empty label columns + bucket) and `outputs/golden_engine.csv` (engine labels, kept separate until compare mode).

**Mode B: compare** (after hand-labelling)
- Per-field agreement %, overall %, split by bucket.
- Confusion summary for the weakest field.
- Failure table: post, engine label, human label, likely reason (Flash drafts the reason; you confirm).
- Optional before/after re-run after a prompt fix (prompt_version bump).

Output: `outputs/eval_report.json`.

### 4.9 Streamlit App
- Reads only `outputs/` (cached with `st.cache_data`).
- Filters live in the sidebar and apply across tabs.
- Evidence quotes link to source URLs.
- Colour-blind-safe palette (e.g., Okabe-Ito); labels and patterns, never colour alone.
- Footer on every page: "Research tool built on public data. Not investment advice."

### 4.10 Live Analysis: Safe Fallback Flow

```
User pastes post
      │
      ▼
[Input checks] empty / > 2,000 chars / session cap (3) / daily cap (50) hit? ──yes──► friendly message, no call
      │no
      ▼
[PII strip]
      │
      ▼
[ONE Flash call: live_guard_v1 + enrich_v1 combined]
  returns {classification: on_topic | off_topic | advice_request, enrichment fields (only if on_topic)}
      │
      ├── advice_request ──► Decline: "I can't recommend funds or stocks." → AMFI / Mutual Funds Sahi Hai
      ├── off_topic ───────► "This doesn't look like a SIP experience. Try another post."
      │
      ▼ on_topic
[Evidence verifier + confidence check]  (local, no call)
      │
      ├── confidence low or evidence fails ──► "Not enough signal to classify confidently"
      ▼
Show fields + confidence badge + evidence span + disclaimer
```

**Demo limits (`app/demo_limits.py`):**
- Session cap: 3 analyses, tracked in `st.session_state`.
- Daily cap: 50 analyses across all users, tracked in a small JSON file (`date`, `count`) held behind a lock in an object returned by `st.cache_resource`. The count resets when the date changes. It also resets if Streamlit Cloud restarts the app, which is acceptable for a demo; the project budget cap is the final backstop.
- Streamlit Secrets hold only `GEMINI_API_KEY` (read-only). The demo uses the main key; there is no separate demo key.
- Every live call goes through `llm_client` → `budget.py`, so it is logged and counts against the project cap.

### 4.11 Approval Gate (run.py)
Before any stage that calls Gemini, `run.py` computes and prints a pre-run estimate in the following format, then blocks on `input()`:

```
========================================
Pre-run Estimate — Stage: Enrich
Posts to process : 3,850  (already done: 150)
Batch size       : 30  →  129 calls
Est. input tokens: ~1,485,000   Est. output tokens: ~258,000
Thinking tokens  : ~0 (budget set to minimum)
Est. cost        : ₹84.23
Cumulative spend : ₹12.50
Budget remaining : ₹287.50 (per-run) / ₹987.50 (project)
========================================
Proceed? Type 'yes' to continue, anything else to exit:
```

Any input other than `yes` (case-insensitive) exits cleanly with a checkpoint. There is no flag to bypass this gate. The gate fires once per stage invocation; if `--stage all` is used, it fires before each individual stage that calls Gemini.

### 4.12 Budget (`core/budget.py`)
**Cost formula (per call):**
```
cost_usd = ( input_tokens                     × input_price_per_m_usd
           + (output_tokens + thinking_tokens) × output_price_per_m_usd
           + cached_tokens                     × cached_input_price_per_m_usd ) / 1,000,000
cost_inr = cost_usd × usd_inr_rate
```
- Thinking tokens bill at the output price. `input_tokens` excludes cached tokens (cached tokens are billed separately at the cached price), so the client subtracts `cached_content_token_count` from `prompt_token_count`.
- **Billing & Caps:** Gemini runs on AI Studio Prepay (₹1,000 credits in `"My First Project"`, paid tier Tier 1, auto-reload OFF → hard HTTP 402 stop at ₹0; Cloud ₹1,000 manual credit refunded). Note: the first P0 `test-call` ran on the free tier (₹0 actual charge, logged as ₹0.0247 in `spend_log.csv`). In-code caps remain mandatory: per-run ₹300 (raise `BudgetExceeded`, checkpoint, exit); project ₹1,000 cumulative from `spend_log.csv` (refuse to start); per-call sanity check (> 3× estimated average → stop and report).
- **Pre-flight check:** before each call, `budget.py` checks that cumulative spend + the worst-case cost of the next call (max input + `max_output_tokens`) stays under both caps; if not, it stops *before* calling.
- **Spend log:** appends one row per call to `outputs/spend_log.csv` (schema in §9).
- **Estimates:** `estimate(stage, n_posts, batch_size, avg_input_tokens_per_post, avg_output_tokens_per_post)` powers the approval gate. Defaults are replaced by measured per-post averages from `spend_log.csv` after the 20-post test.

## 5. Data Contracts (core schemas)

**RawPost:** `id, source, url, date, author_hash, text, collection_method, keyword_group, raw_meta`

**CleanPost:** RawPost + `hinglish, pii_stripped, clean_text, clean_borderline: bool`

**EnrichedPost:** CleanPost + all enrichment fields + `is_relevant: bool` + `clean_borderline: bool` (pass-through)

**Opportunity:** `id, rank, title, description, score, factors, rubric_notes, n_posts, pct_corpus, stages, continuer_dropoff_ratio, quotes, source_distribution, category_type, emerging, segment_coverage, actionability_rationale: str` (one-line LLM rationale, shown in UI)

**EvalItem:** `post_id, bucket, field, engine_label, human_label, agree, failure_reason`

## 6. Rate-Limit and Throughput Plan
Every clean post is enriched, because `is_relevant` is decided inside the enrichment call itself. Calls per enrichment run ≈ clean post count ÷ batch size. Example: 4,000 clean posts ÷ 30 ≈ **134 enrichment calls**. Add ~20–40 calls overhead (Analyse clustering, Surface Actionability rating + card titles, Evaluate failure reasons) → **~155–175 calls** per full pipeline run. Output tokens (~200 per post at \$3.75/M) dominate cost. Estimated ₹300–450; actual cost per post measured on the 20-post test and used for all further estimates. Live Analysis: 3 calls/session, 50 calls/day global.

Quota guard reads limits from config; when the per-minute limit nears, it sleeps; when the daily limit nears, it checkpoints and exits cleanly before `BudgetExceeded` fires. Run enrichment in chunks across sessions if needed; checkpoints make this seamless.

## 7. Security and Privacy
- Secrets in `.env` locally and Streamlit Secrets in production (Secrets hold only `GEMINI_API_KEY`); `.env` gitignored.
- Raw and clean data never committed; only aggregated outputs and an anonymised enriched sample.
- Usernames hashed; PII regex applied before any LLM call.
- Quotes ≤ 30 words, linked to source.

## 8. Error Handling

| Failure | Handling |
|---|---|
| Source blocked / API error | Log, skip source, continue others |
| 429 rate limit | Exponential backoff (2s, 4s, 8s, 16s, 32s); if daily limit nears, checkpoint and exit before BudgetExceeded fires. |
| Invalid JSON | One repair retry → `enrichment_failed` |
| Evidence span not found | Retry once → downgrade to unknown |
| Volume < 3,000 | Warning + suggested source/keyword expansion |
| Continuer floor unmet | Warning + flip-flag instruction |
| Missing outputs in app | App shows "data not yet generated" instead of crashing |
| BudgetExceeded (per-run cap) | Checkpoint current progress, print spend summary, exit cleanly. Resume manually with same command. |
| 402 prepay credits exhausted | No retry; log `error_402`, checkpoint current progress, print `"Gemini prepay credits exhausted — top up in AI Studio"`, exit cleanly. |

## 9. Observability
Each stage prints and saves a summary: records in, out, dropped (by reason), failed, LLM calls, time taken. Saved to `outputs/corpus_stats.json` and shown in the Overview tab. `outputs/spend_log.csv` records every Gemini call with columns: `timestamp, stage, batch_id, model, prompt_version, input_tokens, output_tokens, thinking_tokens, cached_tokens, cost_inr, cumulative_cost_inr, retry_count, call_status`.

## 10. Testing
- `test_schemas`: enums and required fields validate.
- `test_segment_fit`: known inputs give expected scores.
- `test_evidence_span`: verifier catches paraphrased spans.
- `test_live_guard`: 5 fixed inputs (off-topic, advice request, empty, low-signal, valid) give the correct fallback (PRD AC10).
- `test_budget_halt` (AC13): Mock Gemini calls with costs that cumulatively exceed the per-run cap (₹300). Assert pipeline checkpoints and exits cleanly. Assert `spend_log.csv` contains all calls up to the halt.
- `test_approval_gate` (AC14): Simulate `input()` returning `"no"`. Assert no Gemini call is made. Simulate `"yes"`. Assert the call proceeds.
- `test_thinking_tokens` (AC15): Mock a Gemini response with `usage_metadata.thoughts_token_count = 0`. Assert spend_log row records `thinking_tokens = 0`. Mock a response with `thoughts_token_count = 500` and `output_tokens = 100`. Assert a WARNING is logged.
- `test_spend_log_schema` (AC16): After a mock enrichment run, assert `spend_log.csv` has all required columns: `timestamp, stage, batch_id, model, prompt_version, input_tokens, output_tokens, thinking_tokens, cached_tokens, cost_inr, cumulative_cost_inr, retry_count, call_status`.
- `test_cost_formula` (PRD AC18): known token counts (input, output, thinking, cached) give the expected ₹ value under `cost = input × input_price + (output + thinking) × output_price + cached × cached_price`, × `usd_inr_rate`. Also checks that thinking tokens are billed at the output price and cached tokens at the cached price.
- `test_demo_caps_session` (AC17a): Simulate 4 live analysis requests in a session. Assert the 4th is blocked with the session-limit message.
- `test_demo_caps_daily` (AC17b): Set the file-backed daily counter to 50. Assert the next request is blocked with the daily-limit message. Assert the counter resets when the date changes.
- Manual: 20-record spot check after each stage.

## 11. Deployment
1. Push the repo to GitHub (outputs committed, data gitignored).
2. Create the app on Streamlit Community Cloud, pointing to `app/app.py`.
3. Add only `GEMINI_API_KEY` to Streamlit Secrets (read-only). The daily demo counter is a file-backed store via `st.cache_resource` (see §4.10); it resets on app restart, which is acceptable for a demo. The demo uses the main key, protected by the 3/session + 50/day caps and the project budget cap.
4. Test the public link in an incognito window (PRD AC11).
5. Re-runs: run the pipeline locally → commit new outputs → the app redeploys automatically.

## 12. Build Sequence (dates flexible, deadline 9 Nov)

| Phase | Build |
|---|---|
| P0 | Repo, config, `.env`, `llm_client`, schemas, logger, checkpoint. Call `models.list` with the Gemini API key to confirm the exact ID string for `gemini-3.6-flash`. Confirm current Gemini pricing at `ai.google.dev/gemini-api/docs/pricing`. Confirm the thinking-budget SDK parameter name (`thinking_level` or `thinking_budget`) from google-genai SDK docs. Implement `core/budget.py` and run a mock spend test (AC13). Implement the approval gate in `run.py`. |
| P1 | Play Store + App Store collectors |
| P2 | Reddit collector |
| P3 | CSV seed loader (forums) |
| P4 | YouTube collector |
| P5 | TradingQ&A Discourse collector (`collectors/tradingqna.py`) + official context data (`collectors/context_data.py` → `data/context/`) |
| P6 | Collate + Clean |
| P7 | Enrich + evidence verifier + segment_fit |
| P8 | Analyse + Surface |
| P9 | Evaluate (propose → hand-label → compare) |
| P10 | Streamlit app + Live Analysis guard + deploy |

Each phase ends with: stage summary printed, 20-record spot check, commit.
