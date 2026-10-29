# Golden Set Labelling Guide

Label the 20 posts in `data/seeds/golden_human.csv` **before** looking at any engine output. The engine's
labels for these posts are kept separately (`outputs/golden_engine.csv`, written in P9) so this stays blind.

The definitions below are copied word for word from the enrichment prompt (`prompts/enrich_v3_20261027.txt`), so you
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

is_relevant (true / false)
- true: the writer describes their own (or a directly observed family/friend's) experience of starting, continuing, pausing, reducing, stopping, restarting or failing to pay a mutual fund SIP; an app/mandate/autopay/KYC problem affecting their SIP or mutual fund investing; or their own confusion or question about how their SIP works (these show what confuses investors, but their outcome stays "unknown"). A first-hand story of starting a first SIP, with a reason or an expectation, is relevant.
- false: generic fund or portfolio selection ("rate my portfolio", "which small cap fund"), general market opinion, advice or explanations written for others with no own experience, lump-sum or stock/F&O/IPO/loan/login issues that do not involve their SIP or mutual fund investing, praise with no experience in it, and any text not in English or Roman-script Hindi/Hinglish (for example Roman-script Tamil, Telugu, Kannada, Malayalam or Bengali).

outcome (what happened to the SIP, only as stated; questions and hypotheticals → unknown; redeeming or withdrawing money is not "stopped" unless the post says the SIP itself was stopped or cancelled)
- continued: still running the SIP (including through a dip or after considering stopping).
- paused: paused or skipped one or more instalments on purpose, not yet resumed.
- reduced: lowered the SIP amount but kept it running.
- stopped: cancelled or ended the SIP, or it stopped running because of failed payments.
- restarted: resumed after a pause, stop or failure.
- considering_stop: thinking about stopping, pausing or reducing, has not done it yet.
- unknown: not stated.

journey_stage (how far into their first SIP the key event happened)
- pre_start: has not started a SIP yet.
- month_0_1, month_2_3, month_4_6, month_7_12: months since the SIP started when the event happened.
- month_12_plus: more than 12 months in.
- post_stop: written after stopping, and the post is about life after stopping, with no timing given.
- unknown: no timing stated.

trigger_type (the main cause of the outcome or of considering a change)
- life_event_financial_pressure: job change or loss, salary delay, big expense, emergency, debt, marriage, moving.
- emotional: fear, anxiety, panic, guilt or loss of confidence not specifically about returns.
- returns_disappointment: returns lower than expected, negative or red numbers, low XIRR, comparing unfavourably with FD/stocks.
- ux_friction: the app or process got in the way: failed or double debits, mandate/autopay problems, KYC hold, confusing screens, poor support, redemption delays.
- financial_literacy_gap: misunderstanding how SIPs, NAV, units, XIRR, pause vs stop, lock-in or tax work.
- feature_gap: a capability is missing (e.g., cannot change debit date, no pause option, cannot move the mandate to a new bank, no reminder).
- social_peer_influence: family, friends, colleagues, finfluencers or social media told them to start, stop or switch.
- other: a clear cause not listed above.
- none: the post describes no change and no cause (e.g., a SIP simply running).
- unknown: there is a change but the cause is not stated.

kpi_branch (which route of the KPI tree the post shows)
- voluntary_stop (A): the person chose, or is considering choosing, to stop, pause or reduce.
- involuntary_payment_failure (B): instalments failed or stopped because money did not move (low balance on debit day, bank or mandate problem, KYC hold, fund-side stop), even if the person intended to keep investing. If a payment failure then made them decide to stop, still use B.
- lapse_and_return (C): a pause, skip or failure turned into a long lapse (inertia, restart hassle, avoidance), or a lapsed investor restarted.
- platform_switch: stopped the SIP on one app to move it to another app or platform (scenario 17).
- continuing: an active SIP with no stop, pause or reduction being considered.
- not_applicable: is_relevant is false, a pre-start post, or a relevant post with no stated SIP change (e.g., a question or a complaint).
Consistency table (always follow):
- outcome continued → continuing
- outcome considering_stop / paused / reduced / stopped → voluntary_stop, involuntary_payment_failure, lapse_and_return or platform_switch, whichever fits
- outcome restarted → lapse_and_return
- outcome unknown → involuntary_payment_failure only if a failed or blocked SIP instalment/mandate is described; otherwise not_applicable

stop_scenario_ids (0–3 ids, most important first; only when kpi_branch is voluntary_stop, involuntary_payment_failure, lapse_and_return or platform_switch; otherwise [])
1 money pressure or a life event (job switch, salary gap, expense, emergency, debt)
2 expectation gap or returns (small or negative returns, red numbers, "not working")
3 not understanding how a SIP works (NAV, XIRR, pause vs stop, options other than cancel)
4 lost the reason to invest (no goal, tax-season-only start, goal reached and withdrew)
5 lured into other investments (stocks, F&O, crypto, FD, gold, ULIP, real estate)
6 someone told them to stop or switch (friend, family, colleague, finfluencer)
7 stopped to "fix" or consolidate the portfolio and never finished
8 lost trust in the platform (redemption delay, support, outage, double debit)
9 low balance on debit day (debit before payday, salary late)
10 bank change; mandate still on the old account
11 mandate problems (setup incomplete, expired, revoked, rejected, UPI autopay limit)
12 KYC issues (KYC on hold, re-verification blocking purchases)
13 fund-side stop (fund stops taking SIPs, merges, closes)
14 a "temporary" pause became permanent (forgot, no reminder)
15 restarting is a hassle (redo mandate, pick funds again)
16 avoidance or guilt ("I'll restart later", waiting for the market to fall)
17 moved to another platform
If the real cause is not in this list, leave the ids empty (or include only those that fit) and name the cause in discovered_category. Never force a post into the nearest id.

seeded_category_primary / seeded_category_secondary (one of 6 seeds; primary is mandatory if any seed applies; secondary only if the post clearly spans two)
- ux_friction: the app or process makes keeping the SIP harder (payments, mandate, KYC, screens, support).
- emotional_triggers: feelings drive the decision (fear, anxiety, regret, disappointment at returns, guilt).
- financial_literacy_gaps: a misunderstanding of how SIPs or returns work.
- feature_gaps: a missing capability would have helped.
- life_event_financial_pressure: money or life circumstances force the change.
- social_peer_influence: other people's advice or behaviour drives it.
discovered_category: a short snake_case label ONLY when none of the 6 seeds fits; otherwise null.

checking_what (≤15 words): what they look at when they check (e.g., "returns percentage", "XIRR", "current value", "next debit date"). null if not stated.
checking_frequency: daily / weekly / monthly / rarely / unknown, only if stated or clearly described.
outside_app_behaviour: where they went when unsure: asked_family_friends / youtube / reddit_forums / advisor / news / did_nothing / other / unknown.
start_reason (≤20 words), expectation (≤20 words), trigger_detail (≤25 words), in_app_confusion (≤25 words), unmet_need (≤25 words): short summaries, null if not stated.

segment_signals (each "unknown" unless the text states it)
- age_band: under_22 / 22_28 / 29_35 / over_35 (only from a stated age or clear life stage like "college student").
- employment: salaried (mentions salary, job, company, office) / self_employed / student / other.
- years_of_work: 0_1 ("fresher", "first job") / 2_4 / 5_plus.
- city_tier: tier_1 only for Mumbai, Delhi NCR, Bengaluru, Hyderabad, Chennai, Pune, Kolkata, Ahmedabad; tier_2 for other named cities; tier_3 for small towns or villages.
- income_stability: stable_monthly (regular salary) / variable / unstable.
- source_of_money: salary / bonus / savings / family / other.
- influencer: who got them to start: family / friends_peers / colleagues / finfluencer_youtube / social_media / advisor / app_marketing / self / other.
segment_confidence: high if 3+ segment fields are known, medium if 1–2, low if none (omit when low).
is_first_time_investor: yes if the post says first SIP, first investment, beginner or new to investing; no if it says they invested before; else unknown.

platform_tag: groww / zerodha / indmoney / paytm_money / mfcentral / other / unspecified. Use the platform the experience happened on; default to platform_hint unless the text names another.
no_fund_stock_flag: true if the post is mainly about which specific funds or stocks to pick.
enrichment_confidence: high if the post is explicit, medium if partly implied, low if thin or ambiguous.
