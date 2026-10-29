"""Mocked unit tests for P6: Stage 2 Clean (`pipeline/clean.py`).

Covers all locked P6 requirements (zero LLM calls, zero network calls):
  1. Non-Latin script filter (>50% in ANY non-Latin script: Devanagari, Tamil, Telugu, Kannada, Malayalam,
     Bengali, Gujarati) + script-level drop logging + Roman Hinglish (`hinglish=True`).
  2. Strict PII isolation: masks email (`[EMAIL]`), UPI (`[UPI]`), PAN (`[PAN]`), phone (`[PHONE]`), and
     account numbers (`[ACCOUNT_NUM]`), stores ONLY the masked text in BOTH `text` and `clean_text`, and
     verifies that NO file written to `data/clean/` (`clean_posts.jsonl`) or `outputs/` (`drop_log.csv`,
     `corpus_stats.json`) contains any unmasked PII pattern.
  3. Whole-text near-dedupe (`rapidfuzz.fuzz.token_sort_ratio >= 90` with length-ratio guard): catches genuine
     near-duplicates across/within sources, while NEVER collapsing a short post that is a subset of a longer post.
  4. Rules-only off-topic drop (`off_topic`) & `clean_borderline=True` flagging on generic portfolio-review posts.
  5. 40% per-source cap + `target_max` clamp + square-root damped `(YYYY-MM, subreddit)` stratification +
     `quality_tier` preference + before/after theme-keyword audit in `corpus_stats.json`.
  6. Platform tag carry-through (`tradingqna` -> `zerodha`, store listings -> `groww`, etc.) and `CleanPost` validation.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from core.config import load_config
from core.schemas import CleanPost, RawPost
from pipeline.clean import (
    contains_unmasked_pii,
    deduplicate_records,
    detect_non_latin_script,
    is_hinglish_text,
    run_clean,
    scrub_pii,
    stratify_downsample_reddit,
)


def _raw_post(
    post_id: str,
    source: str,
    text: str,
    *,
    date: str = "2026-08-15T10:00:00Z",
    raw_meta: Dict[str, Any] | None = None,
) -> RawPost:
    return RawPost(
        id=post_id,
        source=source,
        url=f"https://example.com/{post_id}",
        date=date,
        author_hash="a1b2c3d4e5f60718",
        text=text,
        collection_method="scraped",
        keyword_group="core",
        raw_meta=raw_meta or {"matched_keywords": ["sip"]},
    )


def _write_raw_corpus(raw_dir: Path, posts: List[RawPost]) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / "raw_posts.jsonl"
    path.write_text("".join(json.dumps(p.model_dump(), ensure_ascii=False) + "\n" for p in posts), encoding="utf-8")


def test_non_latin_script_filter_drops_all_indic_scripts_and_keeps_roman_hinglish(tmp_path: Path):
    """1. Drops posts where >50% of letters are in ANY non-Latin script (Devanagari, Tamil, Telugu, Kannada,
    Malayalam, Bengali, Gujarati) and logs each under `language_drops_by_script`, while keeping Roman Hinglish
    with `hinglish=True`."""
    samples = [
        ("devanagari", "मेरा ग्रो एसआईपी ऑटोपे पिछले महीने से बंद हो गया है और पैसा कट रहा है SIP"),
        ("tamil", "எனது மியூச்சுவல் ஃபண்ட் எஸ்ஐபி முதலீடு நிறுத்தப்பட்டது ஏன் என்று தெரியவில்லை SIP"),
        ("telugu", "నా మ్యూచువల్ ఫండ్ ఎస్ఐపి ఆటోపే మండేట్ ఫెయిల్ అయింది ఎలా ఆపాలి SIP"),
        ("kannada", "ನನ್ನ ಮ್ಯೂಚುವಲ್ ಫಂಡ್ ಎಸ್ಐಪಿ ಹಣ ಕಟ್ ಆಗಿದೆ ಆದರೆ ಯೂನಿಟ್ ಬಂದಿಲ್ಲ SIP"),
        ("malayalam", "എന്റെ മ്യൂച്വൽ ഫണ്ട് എസ്ഐപി ഓട്ടോപേ പ്രവർത്തിക്കുന്നില്ല എങ്ങനെ നിർത്താം SIP"),
        ("bengali", "আমার মিউচুয়াল ফান্ড এসআইপি অটোপে বন্ধ হয়ে গেছে এবং টাকা কেটে নিয়েছে SIP"),
        ("gujarati", "મારું મ્યુચ્યુઅલ ફંડ એસઆઈપી ઓટોપે બંધ થઈ ગયું છે અને પૈસા કપાઈ ગયા SIP"),
    ]
    for expected_script, text in samples:
        should_drop, dom_script, ratio = detect_non_latin_script(text, max_ratio=0.50)
        assert should_drop is True, f"Expected {expected_script} sample to be dropped"
        assert dom_script == expected_script
        assert ratio > 0.50

    # Roman Hinglish must NOT be dropped and must set hinglish=True
    hinglish_text = "Maine apna Groww SIP band kar diya kyunki har mahine paisa kat raha tha aur returns nahi mile"
    should_drop_h, dom_h, _ = detect_non_latin_script(hinglish_text, max_ratio=0.50)
    assert should_drop_h is False
    assert dom_h is None
    assert is_hinglish_text(hinglish_text) is True

    # End-to-end check via run_clean
    settings = load_config()
    posts = [
        _raw_post(f"play_store:{sc}", "play_store", txt) for sc, txt in samples
    ] + [
        _raw_post("play_store:hinglish_1", "play_store", hinglish_text),
    ]
    raw_dir = tmp_path / "raw"
    clean_dir = tmp_path / "clean"
    drop_log = tmp_path / "outputs" / "drop_log.csv"
    stats_path = tmp_path / "outputs" / "corpus_stats.json"
    _write_raw_corpus(raw_dir, posts)

    rc = run_clean(settings, raw_dir=raw_dir, clean_dir=clean_dir, drop_log_path=drop_log, stats_path=stats_path)
    assert rc == 0

    clean_lines = (clean_dir / "clean_posts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(clean_lines) == 1
    kept = CleanPost.model_validate_json(clean_lines[0])
    assert kept.id == "play_store:hinglish_1"
    assert kept.hinglish is True

    stats = json.loads(stats_path.read_text(encoding="utf-8"))["clean"]
    for sc, _ in samples:
        assert stats["language_drops_by_script"][sc]["play_store"] == 1


def test_no_unmasked_pii_in_any_clean_or_output_file(tmp_path: Path):
    """2. Fix (a): PII (`[EMAIL]`, `[UPI]`, `[PAN]`, `[PHONE]`, `[ACCOUNT_NUM]`) is masked in BOTH `text` and
    `clean_text` of `clean_posts.jsonl`, and NO file in `data/clean/` or `outputs/` contains an unmasked phone,
    email, PAN, UPI, or account-number pattern."""
    raw_pii_post = (
        "My Groww SIP installment failed! Contact me at rahul.sharma99@gmail.com or +91 9876543210. "
        "My PAN is ABCDE1234F, UPI is rahul99@okhdfcbank, folio number 102938475612, and Aadhaar 4567 8901 2345."
    )
    masked, pii_counts = scrub_pii(raw_pii_post)
    assert pii_counts == {"email": 1, "upi": 1, "pan": 1, "phone": 1, "account_num": 2}
    assert "[EMAIL]" in masked
    assert "[UPI]" in masked
    assert "[PAN]" in masked
    assert "[PHONE]" in masked
    assert "[ACCOUNT_NUM]" in masked
    assert not contains_unmasked_pii(masked)

    settings = load_config()
    raw_dir = tmp_path / "raw"
    clean_dir = tmp_path / "clean"
    out_dir = tmp_path / "outputs"
    drop_log = out_dir / "drop_log.csv"
    stats_path = out_dir / "corpus_stats.json"

    posts = [
        _raw_post("play_store:pii_1", "play_store", raw_pii_post),
        _raw_post(
            "youtube:pii_2",
            "youtube",
            "My mutual fund SIP installment on Groww was debited from account 112233445566, email test.user@yahoo.in.",
        ),
        _raw_post(
            "reddit:pii_3",
            "reddit",
            "I paused my SIP on Groww after debit issue on bank account 998877665544, call 9123456789.",
            raw_meta={"subreddit": "IndiaInvestments", "matched_keywords": ["sip"]},
        ),
    ]
    _write_raw_corpus(raw_dir, posts)

    rc = run_clean(settings, raw_dir=raw_dir, clean_dir=clean_dir, drop_log_path=drop_log, stats_path=stats_path)
    assert rc == 0

    # Verify BOTH text and clean_text in clean_posts.jsonl are masked and identical
    clean_rows = [
        CleanPost.model_validate_json(line)
        for line in (clean_dir / "clean_posts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(clean_rows) == 3
    for cp in clean_rows:
        assert cp.pii_stripped is True
        assert cp.text == cp.clean_text
        assert not contains_unmasked_pii(cp.text)
        assert not contains_unmasked_pii(cp.clean_text)

    # Audit every file written under clean_dir and out_dir for any unmasked PII literal or regex match
    forbidden_literals = (
        "rahul.sharma99@gmail.com",
        "9876543210",
        "ABCDE1234F",
        "rahul99@okhdfcbank",
        "102938475612",
        "4567 8901 2345",
        "998877665544",
        "9123456789",
    )
    for folder in (clean_dir, out_dir):
        for file_path in folder.rglob("*"):
            if file_path.is_file():
                content = file_path.read_text(encoding="utf-8")
                for lit in forbidden_literals:
                    assert lit not in content, f"Unmasked PII literal {lit!r} leaked into {file_path}"
                assert not contains_unmasked_pii(content), f"Unmasked PII pattern matched in {file_path}"

    # Also verify drop_log.csv contains dropped posts only (0 rows here since neither post was dropped)
    with open(drop_log, "r", encoding="utf-8") as f:
        drop_rows = list(csv.DictReader(f))
    assert drop_rows == []


def test_whole_text_near_dedupe_catches_near_dupes_without_subset_false_positives():
    """3. Fix (b): `token_sort_ratio >= 90` with length-ratio guard catches genuine near-duplicates across sources,
    but does NOT treat a short post as a duplicate of a much longer post that contains the same words."""
    recs = [
        {
            "id": "play_store:1",
            "source": "play_store",
            "clean_text": "I stopped my Groww SIP after 4 months because autopay mandate failed twice.",
        },
        {
            # Near-duplicate (same words, minor punctuation/word swap, >=90 token_sort_ratio)
            "id": "reddit:2",
            "source": "reddit",
            "clean_text": "I stopped my Groww SIP after 4 months because the autopay mandate failed twice!",
        },
        {
            # Short post whose words are a subset of play_store:1 — token_set_ratio would score 100,
            # but whole-text token_sort_ratio + length guard must KEEP it as a distinct post!
            "id": "youtube:3",
            "source": "youtube",
            "clean_text": "I stopped my Groww SIP after 4 months.",
        },
    ]
    kept, dropped = deduplicate_records(recs, near_dupe_threshold=90.0, near_dupe_min_chars=30)
    assert [r["id"] for r in kept] == ["play_store:1", "youtube:3"]
    assert [r["id"] for r in dropped] == ["reddit:2"]


def test_off_topic_drop_and_clean_borderline_flagging(tmp_path: Path):
    """4. Pure non-SIP noise is dropped as `off_topic`; generic portfolio-review posts with an anchor are kept
    with `clean_borderline=True`; behaviour-rich posts have `clean_borderline=False`."""
    settings = load_config()
    posts = [
        # Pure intraday F&O noise without SIP/MF anchor -> dropped as off_topic
        _raw_post(
            "reddit:off_1",
            "reddit",
            "Lost 50k in Nifty intraday call options trading and scalping today on brokerage app.",
            raw_meta={"subreddit": "IndianStreetBets", "matched_keywords": []},
        ),
        # Generic portfolio-review query -> kept with clean_borderline=True
        _raw_post(
            "reddit:border_1",
            "reddit",
            "Please review my mutual fund portfolio and suggest which mutual fund is best for 10 years.",
            raw_meta={"subreddit": "IndiaInvestments", "matched_keywords": ["mutual fund"]},
        ),
        # First-hand continuation/drop-off behaviour -> kept with clean_borderline=False
        _raw_post(
            "reddit:good_1",
            "reddit",
            "I paused my first SIP on Groww after 3 months when my rent increased and salary was delayed.",
            raw_meta={"subreddit": "personalfinanceindia", "matched_keywords": ["paused sip", "first sip", "groww sip"]},
        ),
    ]
    raw_dir = tmp_path / "raw"
    clean_dir = tmp_path / "clean"
    drop_log = tmp_path / "outputs" / "drop_log.csv"
    stats_path = tmp_path / "outputs" / "corpus_stats.json"
    _write_raw_corpus(raw_dir, posts)

    run_clean(settings, raw_dir=raw_dir, clean_dir=clean_dir, drop_log_path=drop_log, stats_path=stats_path)
    clean_rows = {
        cp.id: cp
        for cp in (
            CleanPost.model_validate_json(line)
            for line in (clean_dir / "clean_posts.jsonl").read_text(encoding="utf-8").splitlines()
        )
    }
    assert "reddit:off_1" not in clean_rows
    assert clean_rows["reddit:border_1"].clean_borderline is True
    assert clean_rows["reddit:good_1"].clean_borderline is False


def test_reddit_40_pct_cap_sqrt_stratification_and_selection_bias_audit(tmp_path: Path):
    """5. Fix (c): Enforces the 40% per-source cap and `target_max` clamp, stratifies Reddit with square-root
    damping across `(YYYY-MM, subreddit)`, prefers high `quality_tier` behaviour posts over generic portfolio
    questions, stores `quality_tier` and `stratum` on every kept Reddit record, writes overflow to
    `source_cap_overflow.jsonl`, and logs before/after theme-keyword counts in `corpus_stats.json`."""
    settings = load_config()
    settings.run.target_max = 100
    settings.run.source_cap_pct = 40

    posts: List[RawPost] = []
    # 15 non-Reddit posts (10 YouTube, 4 Play Store, 1 TradingQ&A) -> 40% cap allows at most floor(15 * 0.4 / 0.6) = 10 Reddit posts
    yt_phrases = [
        "I continued my SIP for two years and never stopped even during market fall.",
        "Started my first SIP on Groww last summer and kept my SIP going through volatility.",
        "My mutual fund SIP anniversary was yesterday and XIRR is finally positive at fourteen percent.",
        "Paused my SIP when I changed jobs in Bangalore, then restarted once salary stabilised.",
        "Why does mutual fund NAV update so late at night after SIP installment deduction?",
        "Cancelled my ELSS SIP after lock-in confusion and switched to a plain index mutual fund.",
        "Step up SIP helped me increase my monthly investment automatically every April after appraisal.",
        "I stopped checking daily mutual fund returns and now check my SIP folio only once a quarter.",
        "Bank autopay mandate took six days to activate before my first SIP debit went through.",
        "Even when midcap mutual funds fell twelve percent, I did not stop my monthly SIP.",
    ]
    for i, txt in enumerate(yt_phrases):
        posts.append(_raw_post(f"youtube:yt_{i}", "youtube", txt))

    ps_phrases = [
        "My Groww SIP autopay mandate failed on the fifth and customer support ticket is still pending.",
        "Groww app shows SIP installment processing for four days even after money was debited from bank.",
        "Unable to pause my Groww SIP before the debit date because the pause button threw an error.",
        "Started my first SIP on Groww with five hundred rupees but folio creation took a whole week.",
    ]
    for i, txt in enumerate(ps_phrases):
        posts.append(_raw_post(f"play_store:ps_{i}", "play_store", txt))

    posts.append(
        _raw_post(
            "tradingqna:tq_1",
            "tradingqna",
            "My Coin SIP installment failed despite an active e-mandate in my savings bank account.",
            raw_meta={"platform_tag": "zerodha", "matched_keywords": ["sip", "mandate"]},
        )
    )

    # 30 distinct Reddit posts:
    # - 25 in skewed month 2026-10 (r/IndiaInvestments), 20 of which are generic portfolio questions and 5 are stop/pause behaviour
    # - 5 in earlier month 2025-05 (r/personalfinanceindia) with strong stop/pause behaviour
    generic_oct_texts = [
        "Please review my mutual fund portfolio from Mumbai investing in flexicap and suggest which mutual fund is best.",
        "Rate my portfolio of three mutual funds for retirement goal in twenty years with ten thousand monthly.",
        "Which mutual fund should I buy for my daughter education goal over a fifteen year horizon in India?",
        "Suggest some good mutual funds for a beginner software engineer starting with five thousand rupees.",
        "Portfolio review needed for my aggressive equity mutual fund allocation across largecap and midcap.",
        "Please review my SIP allocation between Parag Parikh flexicap and UTI nifty fifty index mutual fund.",
        "Which mutual fund is best for tax saving under section eighty C with lowest expense ratio this year?",
        "Rate my portfolio holding four index mutual funds and one gold fund for long term wealth creation.",
        "Suggest best mutual fund for lump sum investment of two lakh rupees from annual festival bonus.",
        "Please review my mutual fund selection for buying a house in Hyderabad after eight years of saving.",
        "Which mutual fund should I pick if I want moderate risk and steady compounding over twelve years?",
        "Portfolio review request for my mutual fund investments as a twenty five year old salaried fresher in Bengaluru.",
        "Suggest good mutual funds in smallcap category to complement my existing nifty fifty index fund.",
        "Rate my portfolio of five equity mutual funds totaling thirty thousand per month across categories.",
        "Please review my mutual fund basket and tell me if I have too much overlap between largecap schemes.",
        "Which mutual fund is best for parking emergency buffer money for eighteen months with low volatility?",
        "Suggest some mutual funds that invest in international US tech stocks alongside domestic equity.",
        "Portfolio review for my parents retirement corpus using balanced advantage and hybrid mutual funds.",
        "Please review my SIP portfolio of six funds and advise how to consolidate into three core schemes.",
        "Which mutual fund should I invest in for a seven year horizon with high risk appetite in Pune?",
    ]
    for i, txt in enumerate(generic_oct_texts):
        posts.append(
            _raw_post(
                f"reddit:oct_generic_{i}",
                "reddit",
                txt,
                date="2026-10-10T10:00:00Z",
                raw_meta={"subreddit": "IndiaInvestments", "matched_keywords": ["mutual fund"]},
            )
        )
    oct_beh_texts = [
        "I stopped my Groww SIP in October after bank autopay failed twice and I had a hospital emergency.",
        "Cancelled my Groww SIP after four months because I lost my job during the autumn layoff round.",
        "Paused my Groww SIP this October since my house rent and parents medical expenses jumped sharply.",
        "My Groww SIP installment missed its debit date despite balance and I stopped the mandate in frustration.",
        "Stopped my first SIP on Groww after seeing negative XIRR returns for six straight months in smallcap.",
    ]
    for i, txt in enumerate(oct_beh_texts):
        posts.append(
            _raw_post(
                f"reddit:oct_beh_{i}",
                "reddit",
                txt,
                date="2026-10-12T10:00:00Z",
                raw_meta={"subreddit": "IndiaInvestments", "matched_keywords": ["groww sip", "stopped"]},
            )
        )
    may_beh_texts = [
        "I paused my first SIP on Groww back in May when my monthly salary credit was delayed by three weeks.",
        "Stopped my Groww SIP in May because education loan EMI started and cashflow became very tight.",
        "Had to cancel my mutual fund SIP on Groww during May after wedding expenses wiped out my buffer.",
        "Paused my Groww SIP for two months in May when autopay mandate kept failing on HDFC bank.",
        "Reduced and paused my Groww SIP in May after switching cities and paying a huge rental deposit.",
    ]
    for i, txt in enumerate(may_beh_texts):
        posts.append(
            _raw_post(
                f"reddit:may_beh_{i}",
                "reddit",
                txt,
                date="2025-05-15T10:00:00Z",
                raw_meta={"subreddit": "personalfinanceindia", "matched_keywords": ["first sip", "paused"]},
            )
        )

    raw_dir = tmp_path / "raw"
    clean_dir = tmp_path / "clean"
    drop_log = tmp_path / "outputs" / "drop_log.csv"
    stats_path = tmp_path / "outputs" / "corpus_stats.json"
    _write_raw_corpus(raw_dir, posts)

    rc = run_clean(settings, raw_dir=raw_dir, clean_dir=clean_dir, drop_log_path=drop_log, stats_path=stats_path)
    assert rc == 0

    clean_rows = [
        CleanPost.model_validate_json(line)
        for line in (clean_dir / "clean_posts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    overflow_rows = [
        CleanPost.model_validate_json(line)
        for line in (clean_dir / "source_cap_overflow.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    # 15 non-Reddit + 10 Reddit = 25 total (Reddit is 10/25 = 40.0%)
    assert len(clean_rows) == 25
    reddit_kept = [r for r in clean_rows if r.source == "reddit"]
    assert len(reddit_kept) == 10
    assert len(overflow_rows) == 20

    # Every kept Reddit record carries quality_tier and stratum in raw_meta
    for r in reddit_kept:
        assert "quality_tier" in r.raw_meta
        assert "stratum" in r.raw_meta
        assert r.raw_meta["stratum"] in ("2026-10:IndiaInvestments", "2025-05:personalfinanceindia")

    # Within 2026-10:IndiaInvestments (7 kept out of 25 due to sqrt damping giving 3/5 to 2025-05),
    # all 5 high-tier behaviour posts MUST be selected before any generic portfolio questions
    oct_kept_ids = {r.id for r in reddit_kept if r.raw_meta["stratum"] == "2026-10:IndiaInvestments"}
    for i in range(5):
        assert f"reddit:oct_beh_{i}" in oct_kept_ids

    # Verify platform_tag carry-through on tradingqna and play_store
    tq_row = next(r for r in clean_rows if r.source == "tradingqna")
    assert tq_row.raw_meta["platform_tag"] == "zerodha"
    ps_row = next(r for r in clean_rows if r.source == "play_store")
    assert ps_row.raw_meta["platform_tag"] == "groww"

    # Verify audit block in corpus_stats.json shows strong enrichment of stop_pause_cancel and reduction of generic_portfolio_question
    stats = json.loads(stats_path.read_text(encoding="utf-8"))["clean"]
    audit = stats["reddit_sampling_audit"]
    assert audit["before_total"] == 30
    assert audit["after_total"] == 10
    assert audit["overflow_archived"] == 20
    assert audit["by_theme_bucket"]["stop_pause_cancel"]["after_pct"] > audit["by_theme_bucket"]["stop_pause_cancel"]["before_pct"]
    assert audit["by_theme_bucket"]["generic_portfolio_question"]["after_pct"] < audit["by_theme_bucket"]["generic_portfolio_question"]["before_pct"]
