"""Stage 2: Clean (PRD §9.2, Architecture §4.4).

Rules-only, deterministic, zero LLM calls.
Order of operations:
  1. Collate & normalise text; drop `< min_words` or `< min_chars` or junk/repeated-word templates (`too_short_or_junk`).
  2. Language & script filter (FR2.4): drop posts where `> 50%` of alphabetic characters are in ANY non-Latin script
     (Devanagari, Tamil, Telugu, Kannada, Malayalam, Bengali, Gujarati, Gurmukhi, Oriya, etc.). Log drops per script
     and source. Flag Roman-script Hinglish (`hinglish = True`).
  3. PII scrub in-place (FR2.5): mask emails (`[EMAIL]`), PAN (`[PAN]`), Indian phone numbers (`[PHONE]`),
     UPI IDs (`[UPI]`), and 9–16 digit account/folio/Aadhaar numbers (`[ACCOUNT_NUM]`).
     - Store ONLY the masked text in BOTH `text` and `clean_text` of `clean_posts.jsonl` so unmasked text never leaves
       `data/raw/`.
     - Log PII masked counts by type and source in `corpus_stats.json` (never log raw values).
  4. Spam & promo filter (FR2.2, `spam_promo`): drop referral codes, promo links, solicitation templates.
  5. Exact & near-duplicate removal (FR2.1, `duplicate`): within and across sources using canonical SHA-256 hash and
     `rapidfuzz.fuzz.token_sort_ratio >= 90` (whole-text similarity with length-ratio guard, never `token_set_ratio`).
  6. Rules-only off-topic filter & `clean_borderline` flag (FR2.3, FR2.8): drop pure non-SIP noise (`off_topic`);
     keep borderline generic portfolio/fund-selection posts with `clean_borderline = True`.
  7. Carry/populate `raw_meta.platform_tag` (`tradingqna` = `"zerodha"`, store listings = `"groww"`, etc.).
  8. 40% per-source cap & target_max clamp (FR1.7, FR2.7, `source_cap`):
     - Downsample any source exceeding 40% (and clamp total corpus at `run.target_max = 4000`).
     - For Reddit, stratify across `(YYYY-MM, subreddit)` cells with square-root damping (`sqrt(n)`), ranking within
       each cell by `(quality_tier DESC, matched_keywords_count DESC, sha256(id) ASC)`.
     - Store `raw_meta.quality_tier` and `raw_meta.stratum` on every kept Reddit record, archive excess to
       `data/clean/source_cap_overflow.jsonl`, and log before/after theme-keyword counts in `corpus_stats.json`.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from rapidfuzz import fuzz

from collectors.base import atomic_write_text, iso_z, read_json
from collectors.matcher import KeywordMatcher, match_anchored_reply
from collectors.runner import COLLATED_NAME, collate
from core.config import Settings
from core.logger import StageSummary, get_logger
from core.schemas import CleanPost, RawPost

log = get_logger("clean")

CLEAN_JSONL_NAME = "clean_posts.jsonl"
OVERFLOW_JSONL_NAME = "source_cap_overflow.jsonl"

DROP_REASONS_ORDER = (
    "too_short_or_junk",
    "language",
    "spam_promo",
    "duplicate",
    "off_topic",
    "source_cap",
)

# ----------------------------------------------------------------------------- 1. Normalisation & Junk Rules

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_TIMESTAMP_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_HORIZ_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_ALPHA_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def normalise_text(raw: str) -> str:
    """Normalise HTML entities, residual tags, and whitespace while keeping emojis and line breaks intact."""
    if not raw:
        return ""
    s = html.unescape(raw)
    s = _HTML_TAG_RE.sub(" ", s)
    # Normalise compatibility characters without decomposing Indic combining marks or emojis
    s = s.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    lines = [_HORIZ_SPACE_RE.sub(" ", ln).strip() for ln in s.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return _MULTI_NL_RE.sub("\n\n", text).strip()


def is_too_short_or_junk(text: str, *, min_words: int = 5, min_chars: int = 20) -> bool:
    """Return True if text has fewer than `min_words` alphabetic words, `< min_chars`, or is repetitive junk."""
    if not text or len(text.strip()) < min_chars:
        return True
    stripped = _URL_RE.sub(" ", text)
    stripped = _TIMESTAMP_RE.sub(" ", stripped)
    words = [w.lower() for w in _ALPHA_WORD_RE.findall(stripped) if len(w) >= 1]
    if len(words) < min_words:
        return True
    # Check for pure repetition like "good app good app good app" or "nice nice nice nice nice"
    unique_words = set(words)
    if len(words) >= 5 and len(unique_words) <= 2:
        return True
    return False


# ----------------------------------------------------------------------------- 2. Language & Non-Latin Script Detection (FR2.4)


def classify_char_script(ch: str) -> Optional[str]:
    """Classify an alphabetic character into `'latin'` or a specific non-Latin script name."""
    if not ch.isalpha():
        return None
    cp = ord(ch)
    # Basic Latin + Latin-1 Supplement + Latin Extended-A/B + Latin Extended Additional
    if (0x0041 <= cp <= 0x024F) or (0x1E00 <= cp <= 0x1EFF):
        return "latin"
    if (0x0900 <= cp <= 0x097F) or (0xA8E0 <= cp <= 0xA8FF):
        return "devanagari"
    if 0x0980 <= cp <= 0x09FF:
        return "bengali"
    if 0x0A00 <= cp <= 0x0A7F:
        return "gurmukhi"
    if 0x0A80 <= cp <= 0x0AFF:
        return "gujarati"
    if 0x0B00 <= cp <= 0x0B7F:
        return "oriya"
    if 0x0B80 <= cp <= 0x0BFF:
        return "tamil"
    if 0x0C00 <= cp <= 0x0C7F:
        return "telugu"
    if 0x0C80 <= cp <= 0x0CFF:
        return "kannada"
    if 0x0D00 <= cp <= 0x0D7F:
        return "malayalam"
    if 0x0600 <= cp <= 0x06FF:
        return "arabic_urdu"
    name = unicodedata.name(ch, "")
    if "LATIN" in name:
        return "latin"
    return "other_non_latin"


def detect_non_latin_script(text: str, max_ratio: float = 0.50) -> Tuple[bool, Optional[str], float]:
    """Check if `> max_ratio` of alphabetic characters are in ANY non-Latin script.

    Returns `(should_drop, dominant_non_latin_script, non_latin_ratio)`.
    """
    script_counts: Counter[str] = Counter()
    total_alpha = 0
    for ch in text:
        sc = classify_char_script(ch)
        if sc is not None:
            total_alpha += 1
            script_counts[sc] += 1
    if total_alpha == 0:
        return True, "no_letters", 1.0
    latin_cnt = script_counts.get("latin", 0)
    non_latin_cnt = total_alpha - latin_cnt
    ratio = non_latin_cnt / total_alpha
    if ratio > max_ratio:
        non_latin_scripts = {k: v for k, v in script_counts.items() if k != "latin"}
        dominant = max(non_latin_scripts.items(), key=lambda kv: kv[1])[0] if non_latin_scripts else "other_non_latin"
        return True, dominant, ratio
    return False, None, ratio


_HINGLISH_MARKERS = frozenset(
    {
        "hai",
        "hain",
        "nahi",
        "nhi",
        "naa",
        "kya",
        "kaise",
        "kar",
        "karo",
        "kare",
        "karein",
        "kiya",
        "karna",
        "karke",
        "mera",
        "meri",
        "mere",
        "maine",
        "mujhe",
        "mujhko",
        "apna",
        "apni",
        "apne",
        "paisa",
        "paise",
        "rupaye",
        "mahine",
        "mahina",
        "saal",
        "band",
        "bandh",
        "chalu",
        "ruk",
        "ruka",
        "chhod",
        "kat",
        "kata",
        "kate",
        "katne",
        "aur",
        "bhi",
        "abhi",
        "acha",
        "accha",
        "bhai",
        "yaar",
        "kyunki",
        "kyuki",
        "lekin",
        "magar",
        "sahi",
        "galat",
        "bahut",
        "bohot",
        "zyada",
        "jyada",
        "kam",
        "kab",
        "kaha",
        "kahan",
        "koi",
        "kuch",
        "sab",
        "wala",
        "wali",
        "wale",
        "raha",
        "rahi",
        "rahe",
        "hota",
        "hoti",
        "hoga",
        "hogaya",
        "gaya",
        "gayi",
        "gaye",
        "mila",
        "milega",
        "lagta",
        "dikha",
    }
)
_HINGLISH_PHRASES_RE = re.compile(
    r"\b(?:sip\s+band|sip\s+bandh|sip\s+chalu|sip\s+ruk|sip\s+chhod|band\s+kar|band\s+nahi|paisa\s+kat|paise\s+kat)\b",
    re.IGNORECASE,
)


def is_hinglish_text(text: str) -> bool:
    """Return True if Roman-script text contains Hinglish phrases or >=2 distinct Roman-Hindi markers."""
    if not text:
        return False
    if _HINGLISH_PHRASES_RE.search(text):
        return True
    words = {w.lower() for w in _ALPHA_WORD_RE.findall(text)}
    hits = words & _HINGLISH_MARKERS
    return len(hits) >= 2


# ----------------------------------------------------------------------------- 3. PII Scrubber (FR2.5)

# Order matters: Email & UPI first (so digits inside an email/UPI aren't partially eaten by phone/account),
# then PAN, then Phone, then Account/Folio/Aadhaar numbers.
PII_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PII_UPI_RE = re.compile(
    r"\b[A-Za-z0-9._\-]{2,}@(?:okaxis|okhdfcbank|okicici|oksbi|ybl|ibl|axl|paytm|apl|upi|kotak|pingpay|fbl|indus)\b",
    re.IGNORECASE,
)
PII_PAN_RE = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
# Indian mobile numbers: optional +91 / 91 / 0 prefix, followed by 10 digits starting with 6-9 (allowing 1 space/hyphen)
PII_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+91[\s\-]?|91[\s\-]|0)?[6-9]\d{4}[\s\-]?\d{5}(?!\d)"
)
# 12-digit spaced Aadhaar ("1234 5678 9012") or 9-16 contiguous digits (account, folio, order/ticket ID)
PII_ACCOUNT_RE = re.compile(
    r"(?<!\d)(?:\d{4}[\s\-]\d{4}[\s\-]\d{4}|\d{9,16})(?!\d)"
)


def scrub_pii(text: str) -> Tuple[str, Dict[str, int]]:
    """Mask PII patterns in `text` and return `(masked_text, counts_by_type)`. Never logs or returns raw matches."""
    counts: Dict[str, int] = {}
    out = text

    out, n_email = PII_EMAIL_RE.subn("[EMAIL]", out)
    if n_email:
        counts["email"] = n_email

    out, n_upi = PII_UPI_RE.subn("[UPI]", out)
    if n_upi:
        counts["upi"] = n_upi

    out, n_pan = PII_PAN_RE.subn("[PAN]", out)
    if n_pan:
        counts["pan"] = n_pan

    out, n_phone = PII_PHONE_RE.subn("[PHONE]", out)
    if n_phone:
        counts["phone"] = n_phone

    out, n_acct = PII_ACCOUNT_RE.subn("[ACCOUNT_NUM]", out)
    if n_acct:
        counts["account_num"] = n_acct

    return out, counts


def contains_unmasked_pii(text: str) -> bool:
    """Audit helper for tests: return True if any unmasked email, UPI, PAN, phone, or 9-16 digit account number appears."""
    if not text:
        return False
    # Strip URLs and ISO dates/timestamps first so numeric IDs in URLs don't false-trigger in metadata checks
    stripped = _URL_RE.sub(" ", text)
    return bool(
        PII_EMAIL_RE.search(stripped)
        or PII_UPI_RE.search(stripped)
        or PII_PAN_RE.search(stripped)
        or PII_PHONE_RE.search(stripped)
        or PII_ACCOUNT_RE.search(stripped)
    )


# ----------------------------------------------------------------------------- 4. Spam & Promo Filter (FR2.2)

_SPAM_PROMO_RE = re.compile(
    r"(?:"
    r"\buse\s+my\s+(?:referral\s+)?code\b"
    r"|\breferral\s+(?:code|link|bonus)\b"
    r"|\bsign\s*up\s+using\s+my\b"
    r"|\bjoin\s+(?:my|our)\s+(?:telegram|whatsapp)\b"
    r"|\b(?:whatsapp|telegram)\s+(?:me|us|group|channel)\b"
    r"|\bdm\s+me\s+for\b"
    r"|\bguaranteed\s+(?:returns?|profit)\b"
    r"|\b100%\s+(?:sure|guaranteed)\s+(?:profit|return)\b"
    r"|\bearn\s+(?:rs\.?|₹|\$)\s*\d+[\d,]*\s*(?:per\s+day|daily|weekly)\b"
    r"|\bforex\s+(?:trading|signal|mentor)\b"
    r")",
    re.IGNORECASE,
)


def is_spam_or_promo(text: str) -> bool:
    if not text:
        return False
    if _SPAM_PROMO_RE.search(text):
        return True
    # Links-only post: if removing URLs leaves fewer than 3 alphabetic words
    without_urls = _URL_RE.sub(" ", text)
    if len(_ALPHA_WORD_RE.findall(without_urls)) < 3:
        return True
    return False


# ----------------------------------------------------------------------------- 5. Exact & Near-Duplicate Removal (FR2.1)

_CANON_RE = re.compile(r"[^a-z0-9]+")


def canonical_text_for_dedupe(text: str) -> str:
    """Lowercase alphanumeric-only single-spaced string for exact hash and whole-text `token_sort_ratio`."""
    return _CANON_RE.sub(" ", (text or "").lower()).strip()


def deduplicate_records(
    records: List[Dict[str, Any]],
    *,
    near_dupe_threshold: float = 90.0,
    near_dupe_min_chars: int = 40,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Deduplicate records within and across all sources using exact canonical hash and
    `rapidfuzz.fuzz.token_sort_ratio >= near_dupe_threshold` (with a length-ratio guard).

    Returns `(kept_records, dropped_duplicate_records)`.
    """
    seen_hashes: Set[str] = set()
    # Bucket kept canonical texts by length band (bins of 15 chars) for fast whole-text comparisons across sources
    len_buckets: Dict[int, List[str]] = defaultdict(list)

    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []

    for rec in records:
        canon = canonical_text_for_dedupe(rec["clean_text"])
        if not canon:
            dropped.append(rec)
            continue
        h = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        if h in seen_hashes:
            dropped.append(rec)
            continue

        c_len = len(canon)
        is_near_dupe = False
        if c_len >= near_dupe_min_chars:
            bucket_id = c_len // 15
            # Check adjacent length buckets so any candidate within +/-15% length is compared
            radius = max(1, int(math.ceil((c_len * 0.15) / 15.0)))
            for b in range(bucket_id - radius, bucket_id + radius + 1):
                for existing_canon in len_buckets.get(b, ()):
                    e_len = len(existing_canon)
                    # Whole-text length guard: shorter must be >= 85% of longer
                    if min(c_len, e_len) / max(c_len, e_len) < 0.85:
                        continue
                    score = fuzz.token_sort_ratio(canon, existing_canon)
                    if score >= near_dupe_threshold:
                        is_near_dupe = True
                        break
                if is_near_dupe:
                    break

        if is_near_dupe:
            dropped.append(rec)
            continue

        seen_hashes.add(h)
        if c_len >= near_dupe_min_chars:
            len_buckets[c_len // 15].append(canon)
        kept.append(rec)

    return kept, dropped


# ----------------------------------------------------------------------------- 6. Rules-Only Off-Topic & clean_borderline (FR2.3, FR2.8)

_EXPERIENCE_OR_BEHAVIOUR_RE = re.compile(
    r"\b(?:"
    r"stop|stopped|stopping|pause|paused|pausing|cancel|cancelled|cancelling"
    r"|continue|continued|continuing|restarted|restart|resume|resumed"
    r"|miss|missed|missing|fail|failed|failing|deduct|deducted|debit|debited"
    r"|autopay|auto[\s\-]?pay|mandate|e[\s\-]?mandate|nach|otm|instalment|installment"
    r"|redeem|redeemed|withdraw|withdrew|withdrawn|exit\s+load|lock[\s\-]?in"
    r"|loss|losses|negative|crash|down|fall|falling|panic|worried|scared|confused|confusion|afraid"
    r"|salary|rent|emi|emergency|job|layoff|married|medical|expense|expenses|tight|broke"
    r"|started\s+my|my\s+first\s+sip|1\s+year|one\s+year|anniversary|kept\s+my|still\s+going|never\s+stopped"
    r"|band|bandh|chalu|ruk|chhod|kat|kata"
    r"|bug|glitch|issue|problem|error|stuck|pending|processing|support|ticket|ui|app|charge|charges"
    r")\b",
    re.IGNORECASE,
)

_GENERIC_PORTFOLIO_QUERY_RE = re.compile(
    r"(?:"
    r"\brate\s+my\s+portfolio\b"
    r"|\breview\s+my\s+(?:portfolio|sip|mutual\s+fund)\b"
    r"|\bwhich\s+(?:mutual\s+)?fund\s+(?:is\s+best|should\s+i|to\s+invest|to\s+buy)\b"
    r"|\bsuggest\s+(?:some\s+|good\s+|best\s+)?(?:mutual\s+)?funds?\b"
    r"|\bportfolio\s+review\b"
    r"|\bplease\s+review\s+my\b"
    r")",
    re.IGNORECASE,
)

_PURE_STOCK_FO_NOISE_RE = re.compile(
    r"\b(?:intraday|options?\s+trading|futures?\s+and\s+options?|f&o|call\s+option|put\s+option|ipo\s+allotment|btst|scalping)\b",
    re.IGNORECASE,
)


def classify_topic_and_borderline(
    text: str,
    raw_meta: Dict[str, Any],
    matcher: KeywordMatcher,
) -> Tuple[bool, bool]:
    """Return `(is_off_topic_drop, clean_borderline)`.

    - `is_off_topic_drop = True`: post has no SIP/MF anchor, no thread_context match, and no behaviour match.
    - `clean_borderline = True`: post is kept (err toward inclusion per FR2.3), but either:
      (a) matches generic portfolio/fund-selection query patterns without continuation/drop-off/UX friction cues, OR
      (b) has no strong continuation/drop-off/UX experience cue (only a bare anchor mention).
    """
    has_anchor = matcher.has_anchor(text)
    matched_via = raw_meta.get("matched_via", "self")
    m_context = match_anchored_reply(text, matcher) if matched_via == "thread_context" else matcher.match(text)

    if not has_anchor and not m_context.matched:
        return True, False

    # If the post is purely about F&O / intraday / IPO without an explicit "sip" or "mutual fund" term, drop as off-topic
    if _PURE_STOCK_FO_NOISE_RE.search(text) and not re.search(r"\b(?:sip|sips|mutual\s+funds?)\b", text, re.I):
        return True, False

    has_exp = bool(_EXPERIENCE_OR_BEHAVIOUR_RE.search(text))
    is_generic_query = bool(_GENERIC_PORTFOLIO_QUERY_RE.search(text))

    # If it's a generic "review my portfolio / which fund should I buy" post and lacks any stop/pause/fail/continue cue
    if is_generic_query and not re.search(
        r"\b(?:stop|stopped|pause|paused|cancel|cancelled|missed|failed|band|ruk|chhod|continued|never\s+stopped)\b",
        text,
        re.I,
    ):
        return False, True

    if not has_exp:
        return False, True

    return False, False


# ----------------------------------------------------------------------------- 7. Platform Tag Inference (Item 6)

_PLATFORM_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("groww", re.compile(r"\bgroww\b", re.IGNORECASE)),
    ("zerodha", re.compile(r"\b(?:zerodha|coin\s+by\s+zerodha|zerodha\s+coin|\bcoin\s+app\b|\bcoin\s+sip\b)\b", re.IGNORECASE)),
    ("indmoney", re.compile(r"\bind\s*money\b", re.IGNORECASE)),
    ("paytm_money", re.compile(r"\bpaytm(?:\s+money)?\b", re.IGNORECASE)),
    ("mfcentral", re.compile(r"\bmf\s*central\b", re.IGNORECASE)),
    ("other", re.compile(r"\b(?:kuvera|et\s*money|upstox|angel\s*one|angel\s*broking|nj\s*wealth|scripbox|cams|kfintech)\b", re.IGNORECASE)),
)


def infer_platform_tag(source: str, text: str, raw_meta: Dict[str, Any]) -> str:
    """Carry through or deterministically infer `platform_tag` (`groww`, `zerodha`, `indmoney`, `paytm_money`,
    `mfcentral`, `other`, `unspecified`)."""
    existing = (raw_meta.get("platform_tag") or "").strip().lower()
    if existing in {"groww", "zerodha", "indmoney", "paytm_money", "mfcentral", "other"}:
        # For TradingQ&A, if the post explicitly discusses Groww, tag as groww; otherwise keep zerodha
        if source == "tradingqna" and re.search(r"\bgroww\b", text, re.I):
            return "groww"
        return existing

    if source in ("play_store", "app_store"):
        return "groww"
    if source == "tradingqna":
        return "zerodha"

    combined = f"{raw_meta.get('thread_title', '')} {raw_meta.get('video_title', '')} {raw_meta.get('channel_handle', '')} {text}"
    for tag, pat in _PLATFORM_PATTERNS:
        if pat.search(combined):
            return tag
    return "unspecified"


# ----------------------------------------------------------------------------- 8. Reddit Quality Tier, Theme Buckets & Stratified Sampling (FR1.7)

REDDIT_THEME_BUCKETS: Dict[str, re.Pattern[str]] = {
    "stop_pause_cancel": re.compile(
        r"\b(?:stop|stopped|stopping|pause|paused|pausing|cancel|cancelled|cancelling|redeem|redeemed|withdraw|withdrew|band|bandh|ruk|chhod)\b",
        re.IGNORECASE,
    ),
    "mandate_autopay_failed": re.compile(
        r"\b(?:autopay|auto[\s\-]?pay|mandate|e[\s\-]?mandate|nach|otm|instalment|installment|missed|failed|deducted|debited|pending)\b",
        re.IGNORECASE,
    ),
    "returns_loss_market": re.compile(
        r"\b(?:return|returns|xirr|cagr|nav|loss|losses|negative|crash|down|fall|falling|market|volatility)\b",
        re.IGNORECASE,
    ),
    "continuer_1yr": re.compile(
        r"\b(?:continue|continued|continuing|1\s+year|one\s+year|anniversary|never\s+stopped|kept\s+my\s+sip|still\s+going|chalu|band\s+nahi)\b",
        re.IGNORECASE,
    ),
    "groww_mention": re.compile(r"\bgroww\b", re.IGNORECASE),
    "generic_portfolio_question": _GENERIC_PORTFOLIO_QUERY_RE,
}


def compute_reddit_quality_tier(rec: Dict[str, Any]) -> int:
    """Compute a deterministic 0–4 quality tier for Reddit records to prefer behaviour-rich posts over generic fund picks."""
    text = rec["clean_text"]
    score = 0
    if (
        REDDIT_THEME_BUCKETS["stop_pause_cancel"].search(text)
        or REDDIT_THEME_BUCKETS["mandate_autopay_failed"].search(text)
        or REDDIT_THEME_BUCKETS["continuer_1yr"].search(text)
    ):
        score += 2
    if REDDIT_THEME_BUCKETS["groww_mention"].search(text) or rec["raw_meta"].get("platform_tag") == "groww":
        score += 1
    if not rec.get("clean_borderline", False):
        score += 1
    if REDDIT_THEME_BUCKETS["generic_portfolio_question"].search(text) and not REDDIT_THEME_BUCKETS["stop_pause_cancel"].search(text):
        score -= 1
    return max(0, min(4, score))


def count_reddit_theme_buckets(records: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts = {k: 0 for k in REDDIT_THEME_BUCKETS}
    for rec in records:
        txt = rec["clean_text"]
        for name, pat in REDDIT_THEME_BUCKETS.items():
            if pat.search(txt):
                counts[name] += 1
    return counts


def stratify_downsample_reddit(
    reddit_records: List[Dict[str, Any]],
    target_keep: int,
    *,
    damping: str = "sqrt",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """Deterministically downsample `reddit_records` to `target_keep` across `(YYYY-MM, subreddit)` strata.

    - Annotates every record's `raw_meta` with `quality_tier` and `stratum = "{yyyy_mm}:{subreddit}"`.
    - Allocates stratum quotas proportional to `sqrt(n_stratum)` (or `n_stratum` if `damping == "proportional"`),
      redistributing any surplus/deficit across non-exhausted strata.
    - Within each stratum, ranks by `(-quality_tier, -matched_keywords_count, sha256(id) ASC)`.
    - Returns `(kept, overflow, audit_summary)`.
    """
    strata_bins: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in reddit_records:
        ym = (rec.get("date") or "")[:7] or "unknown"
        sub = rec.get("raw_meta", {}).get("subreddit") or "unknown"
        stratum = f"{ym}:{sub}"
        qt = compute_reddit_quality_tier(rec)
        rec["raw_meta"]["quality_tier"] = qt
        rec["raw_meta"]["stratum"] = stratum
        strata_bins[stratum].append(rec)

    # Sort each stratum deterministically by (-quality_tier, -len(matched_keywords), sha256(id))
    for st_key, items in strata_bins.items():
        items.sort(
            key=lambda r: (
                -int(r["raw_meta"]["quality_tier"]),
                -len(r["raw_meta"].get("matched_keywords") or ()),
                hashlib.sha256(r["id"].encode("utf-8")).hexdigest(),
            )
        )

    total_in = len(reddit_records)
    if target_keep >= total_in:
        audit = _build_reddit_audit(reddit_records, reddit_records)
        return list(reddit_records), [], audit

    # Compute damped weights per stratum
    weights: Dict[str, float] = {}
    for st_key, items in strata_bins.items():
        n = len(items)
        weights[st_key] = math.sqrt(n) if damping == "sqrt" else float(n)

    # Iterative water-filling quota allocation so no stratum gets more than len(items) and sum(quotas) == target_keep
    quotas: Dict[str, int] = {k: 0 for k in strata_bins}
    remaining_target = target_keep
    active_keys = sorted(strata_bins.keys())

    while remaining_target > 0 and active_keys:
        total_w = sum(weights[k] for k in active_keys)
        if total_w <= 0:
            break
        raw_alloc: Dict[str, float] = {
            k: (weights[k] / total_w) * remaining_target for k in active_keys
        }
        capped_any = False
        for k in list(active_keys):
            cap = len(strata_bins[k]) - quotas[k]
            if raw_alloc[k] >= cap:
                quotas[k] += cap
                remaining_target -= cap
                active_keys.remove(k)
                capped_any = True
        if not capped_any:
            # Assign integer parts, then distribute remainder by largest fractional part (tie-broken by stratum key)
            floors = {k: min(len(strata_bins[k]) - quotas[k], int(math.floor(raw_alloc[k]))) for k in active_keys}
            for k, fl in floors.items():
                quotas[k] += fl
                remaining_target -= fl
            if remaining_target > 0:
                frac_order = sorted(
                    active_keys,
                    key=lambda k: (-(raw_alloc[k] - floors[k]), -weights[k], k),
                )
                for k in frac_order:
                    if remaining_target <= 0:
                        break
                    if quotas[k] < len(strata_bins[k]):
                        quotas[k] += 1
                        remaining_target -= 1
            break

    kept: List[Dict[str, Any]] = []
    overflow: List[Dict[str, Any]] = []
    for st_key in sorted(strata_bins.keys()):
        q = quotas[st_key]
        items = strata_bins[st_key]
        kept.extend(items[:q])
        overflow.extend(items[q:])

    audit = _build_reddit_audit(reddit_records, kept)
    return kept, overflow, audit


def _build_reddit_audit(before: List[Dict[str, Any]], after: List[Dict[str, Any]]) -> Dict[str, Any]:
    before_months = Counter((r.get("date") or "")[:7] for r in before)
    after_months = Counter((r.get("date") or "")[:7] for r in after)
    before_subs = Counter(r.get("raw_meta", {}).get("subreddit", "unknown") for r in before)
    after_subs = Counter(r.get("raw_meta", {}).get("subreddit", "unknown") for r in after)
    before_tiers = Counter(int(r.get("raw_meta", {}).get("quality_tier", 0)) for r in before)
    after_tiers = Counter(int(r.get("raw_meta", {}).get("quality_tier", 0)) for r in after)
    before_themes = count_reddit_theme_buckets(before)
    after_themes = count_reddit_theme_buckets(after)

    theme_comparison = {}
    for k in REDDIT_THEME_BUCKETS:
        b_cnt = before_themes[k]
        a_cnt = after_themes[k]
        b_pct = round(100.0 * b_cnt / len(before), 1) if before else 0.0
        a_pct = round(100.0 * a_cnt / len(after), 1) if after else 0.0
        theme_comparison[k] = {
            "before_count": b_cnt,
            "before_pct": b_pct,
            "after_count": a_cnt,
            "after_pct": a_pct,
        }

    return {
        "before_total": len(before),
        "after_total": len(after),
        "overflow_archived": len(before) - len(after),
        "by_month": {
            m: {"before": before_months[m], "after": after_months.get(m, 0)}
            for m in sorted(before_months.keys())
        },
        "by_subreddit": {
            s: {"before": before_subs[s], "after": after_subs.get(s, 0)}
            for s in sorted(before_subs.keys())
        },
        "by_quality_tier": {
            str(t): {"before": before_tiers.get(t, 0), "after": after_tiers.get(t, 0)}
            for t in sorted(set(before_tiers.keys()) | set(after_tiers.keys()), reverse=True)
        },
        "by_theme_bucket": theme_comparison,
    }


# ----------------------------------------------------------------------------- Main Clean Pipeline Driver


def _source_priority(settings: Settings, source: str) -> int:
    return int(settings.sources.get(source, {}).get("priority", 9))


def run_clean(
    settings: Settings,
    *,
    raw_dir: Optional[Path] = None,
    clean_dir: Optional[Path] = None,
    drop_log_path: Optional[Path] = None,
    stats_path: Optional[Path] = None,
) -> int:
    """Execute Stage 2 (Clean) rules-only pipeline and write `clean_posts.jsonl`, `drop_log.csv`, and `corpus_stats.json`."""
    r_dir = Path(raw_dir) if raw_dir else settings.paths.resolve("raw_dir")
    c_dir = Path(clean_dir) if clean_dir else settings.paths.resolve("clean_dir")
    d_path = Path(drop_log_path) if drop_log_path else settings.paths.resolve("drop_log")
    s_path = Path(stats_path) if stats_path else settings.paths.resolve("corpus_stats")

    c_dir.mkdir(parents=True, exist_ok=True)
    d_path.parent.mkdir(parents=True, exist_ok=True)
    s_path.parent.mkdir(parents=True, exist_ok=True)

    # Ensure raw_posts.jsonl is freshly collated from data/raw/*.jsonl
    raw_posts_file = r_dir / COLLATED_NAME
    if any(p.name != COLLATED_NAME for p in r_dir.glob("*.jsonl")):
        collate(r_dir)

    if not raw_posts_file.exists():
        log.error("Raw corpus file not found at %s; run `--stage collect` first", raw_posts_file)
        return 1

    matcher = KeywordMatcher.from_settings(settings.keywords)
    clean_cfg = settings.clean
    summary = StageSummary("clean")

    raw_records: List[RawPost] = []
    for line in raw_posts_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw_records.append(RawPost.model_validate_json(line))

    summary.records_in = len(raw_records)
    per_source_in: Counter[str] = Counter(r.source for r in raw_records)
    drops_by_source_reason: Dict[str, Counter[str]] = defaultdict(Counter)
    lang_drops_by_script: Dict[str, Counter[str]] = defaultdict(Counter)
    pii_counts_by_source: Dict[str, Counter[str]] = defaultdict(Counter)
    pii_posts_by_source: Counter[str] = Counter()
    hinglish_by_source: Counter[str] = Counter()

    def record_drop(src: str, reason: str) -> None:
        drops_by_source_reason[src][reason] += 1
        summary.dropped[f"{src}:{reason}"] += 1

    # Steps 1, 2, 3, 4, 6, 7 per record before global dedupe
    pre_dedupe: List[Dict[str, Any]] = []
    for rp in raw_records:
        src = rp.source
        norm = normalise_text(rp.text)

        # Step 1: Minimum length & junk filter
        if is_too_short_or_junk(norm, min_words=clean_cfg.min_words, min_chars=clean_cfg.min_chars):
            record_drop(src, "too_short_or_junk")
            continue

        # Step 2: Non-Latin script filter (>50% in ANY non-Latin script)
        drop_lang, dom_script, _ = detect_non_latin_script(norm, max_ratio=clean_cfg.non_latin_max_ratio)
        if drop_lang:
            record_drop(src, "language")
            lang_drops_by_script[dom_script or "other_non_latin"][src] += 1
            continue

        # Step 4: Spam & promo filter (run before PII masking so promo phone/links are caught)
        if is_spam_or_promo(norm):
            record_drop(src, "spam_promo")
            continue

        # Step 6: Off-topic filter & clean_borderline flag (rules-only, no LLM)
        off_topic_drop, is_borderline = classify_topic_and_borderline(norm, rp.raw_meta, matcher)
        if off_topic_drop:
            record_drop(src, "off_topic")
            continue

        # Step 3: PII scrub in-place (Fix a: replace BOTH text and clean_text with masked text)
        masked_text, pii_hits = scrub_pii(norm)
        pii_stripped = bool(pii_hits)
        if pii_stripped:
            pii_posts_by_source[src] += 1
            for p_type, cnt in pii_hits.items():
                pii_counts_by_source[src][p_type] += cnt

        hinglish_flag = is_hinglish_text(masked_text)
        raw_meta = dict(rp.raw_meta)
        platform_tag = infer_platform_tag(src, masked_text, raw_meta)
        raw_meta["platform_tag"] = platform_tag

        pre_dedupe.append(
            {
                "id": rp.id,
                "source": src,
                "url": rp.url,
                "date": rp.date,
                "author_hash": rp.author_hash,
                "text": masked_text,          # Fix (a): never store unmasked text outside data/raw/
                "clean_text": masked_text,
                "collection_method": rp.collection_method,
                "keyword_group": rp.keyword_group,
                "raw_meta": raw_meta,
                "hinglish": hinglish_flag,
                "pii_stripped": pii_stripped,
                "clean_borderline": is_borderline,
            }
        )

    # Sort deterministically before dedupe so higher-priority / Groww-tagged / longer records survive
    pre_dedupe.sort(
        key=lambda r: (
            0 if r["raw_meta"].get("platform_tag") == "groww" else 1,
            _source_priority(settings, r["source"]),
            -len(r["clean_text"]),
            r["id"],
        )
    )

    # Step 5: Exact & whole-text near-duplicate removal (Fix b: token_sort_ratio >= 90)
    post_dedupe, dupes_dropped = deduplicate_records(
        pre_dedupe,
        near_dupe_threshold=clean_cfg.near_dupe_threshold,
        near_dupe_min_chars=clean_cfg.near_dupe_min_chars,
    )
    for d_rec in dupes_dropped:
        record_drop(d_rec["source"], "duplicate")

    # Step 8: 40% per-source cap & target_max (4,000) clamp (FR1.7, FR2.7)
    cap_pct = settings.run.source_cap_pct / 100.0
    target_max = settings.run.target_max

    by_source: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in post_dedupe:
        by_source[rec["source"]].append(rec)

    non_reddit_count = sum(len(v) for s, v in by_source.items() if s != "reddit")
    reddit_candidates = by_source.get("reddit", [])

    # Max Reddit records allowed by 40% share cap AND target_max total cap:
    # reddit <= (cap_pct / (1 - cap_pct)) * non_reddit_count AND reddit <= target_max - non_reddit_count
    if non_reddit_count > 0:
        max_reddit_by_share = int(math.floor((cap_pct / (1.0 - cap_pct)) * non_reddit_count))
    else:
        max_reddit_by_share = len(reddit_candidates)
    max_reddit_by_total = max(0, target_max - non_reddit_count)
    reddit_target = min(len(reddit_candidates), max_reddit_by_share, max_reddit_by_total)

    kept_reddit, overflow_reddit, reddit_audit = stratify_downsample_reddit(
        reddit_candidates,
        reddit_target,
        damping=clean_cfg.reddit_stratification_damping,
    )
    for _ in overflow_reddit:
        record_drop("reddit", "source_cap")

    by_source["reddit"] = kept_reddit

    # Also check if any non-Reddit source exceeds 40% of the final corpus (e.g. in tiny test runs)
    total_after_reddit = sum(len(v) for v in by_source.values())
    other_overflow: List[Dict[str, Any]] = []
    if total_after_reddit > 0:
        max_per_source = int(math.floor(cap_pct * max(total_after_reddit, 1)))
        # Only enforce on non-Reddit if there are at least 3 active sources so 1-source unit tests aren't zeroed out
        if len([s for s, v in by_source.items() if v]) >= 3 and max_per_source > 0:
            for s_name, items in list(by_source.items()):
                if s_name == "reddit" or len(items) <= max_per_source:
                    continue
                items.sort(key=lambda r: (r["date"], r["id"]), reverse=True)
                by_source[s_name] = items[:max_per_source]
                for ov in items[max_per_source:]:
                    other_overflow.append(ov)
                    record_drop(s_name, "source_cap")

    # Assemble final clean records sorted deterministically by (source, date DESC, id ASC)
    final_records: List[CleanPost] = []
    for s_name in sorted(by_source.keys()):
        for r_dict in sorted(by_source[s_name], key=lambda x: (x["date"], x["id"])):
            cp = CleanPost.model_validate(r_dict)
            final_records.append(cp)

    # Write clean_posts.jsonl and source_cap_overflow.jsonl
    clean_jsonl_path = c_dir / CLEAN_JSONL_NAME
    overflow_jsonl_path = c_dir / OVERFLOW_JSONL_NAME

    atomic_write_text(
        clean_jsonl_path,
        "".join(json.dumps(cp.model_dump(), ensure_ascii=False) + "\n" for cp in final_records),
    )
    all_overflow = overflow_reddit + other_overflow
    atomic_write_text(
        overflow_jsonl_path,
        "".join(json.dumps(CleanPost.model_validate(r).model_dump(), ensure_ascii=False) + "\n" for r in all_overflow),
    )

    summary.records_out = len(final_records)

    # Compute final distributions on retained CleanPosts
    per_source_out: Counter[str] = Counter(cp.source for cp in final_records)
    total_out = len(final_records)
    per_source_share_pct: Dict[str, float] = {
        s: round(100.0 * cnt / total_out, 2) if total_out else 0.0
        for s, cnt in per_source_out.items()
    }
    borderline_by_source: Counter[str] = Counter(cp.source for cp in final_records if cp.clean_borderline)
    for cp in final_records:
        if cp.hinglish:
            hinglish_by_source[cp.source] += 1
    platform_dist: Counter[str] = Counter(cp.raw_meta.get("platform_tag", "unspecified") for cp in final_records)

    # Write outputs/drop_log.csv (dropped posts only)
    drop_rows: List[Dict[str, Any]] = []
    for s_name in sorted(per_source_in.keys()):
        raw_n = per_source_in[s_name]
        for reason in DROP_REASONS_ORDER:
            cnt = drops_by_source_reason[s_name].get(reason, 0)
            if cnt > 0:
                drop_rows.append(
                    {
                        "source": s_name,
                        "reason": reason,
                        "count": cnt,
                        "pct_of_source_raw": round(100.0 * cnt / raw_n, 2) if raw_n else 0.0,
                    }
                )

    tmp_drop = d_path.with_name(d_path.name + ".tmp")
    with open(tmp_drop, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["source", "reason", "count", "pct_of_source_raw"])
        writer.writeheader()
        for dr in drop_rows:
            writer.writerow(dr)
    os.replace(tmp_drop, d_path)

    # Update outputs/corpus_stats.json
    stats = read_json(s_path, {})
    app_store_in = per_source_in.get("app_store", 0)
    app_store_out = per_source_out.get("app_store", 0)
    shortfall_note = (
        f"App Store RSS feed returned {app_store_in} SIP-matched reviews ({app_store_out} after cleaning) "
        f"vs the ~500 RSS feed ceiling (accepted per PRD v1.2 §9.1 / FR2.7)."
    )
    volume_warning = None
    if total_out < settings.run.target_min:
        volume_warning = (
            f"Cleaned corpus ({total_out}) is below target_min ({settings.run.target_min}). "
            f"Consider enabling continuer_keywords or adding manual CSV seeds."
        )

    clean_block = {
        "updated_at": iso_z(datetime.now(timezone.utc)),
        "records_in": summary.records_in,
        "records_out": total_out,
        "dropped_total": summary.records_in - total_out,
        "clean_posts_path": str(clean_jsonl_path),
        "overflow_archive_path": str(overflow_jsonl_path),
        "drop_log_path": str(d_path),
        "per_source_in": dict(per_source_in),
        "per_source_out": dict(per_source_out),
        "per_source_share_pct": per_source_share_pct,
        "dropped_by_source_and_reason": {s: dict(c) for s, c in sorted(drops_by_source_reason.items())},
        "language_drops_by_script": {sc: dict(c) for sc, c in sorted(lang_drops_by_script.items())},
        "hinglish_count_by_source": dict(hinglish_by_source),
        "hinglish_total": sum(hinglish_by_source.values()),
        "pii_posts_scrubbed_by_source": dict(pii_posts_by_source),
        "pii_scrubbed_by_source_and_type": {s: dict(c) for s, c in sorted(pii_counts_by_source.items())},
        "borderline_count_by_source": dict(borderline_by_source),
        "borderline_total": sum(borderline_by_source.values()),
        "platform_tag_distribution": dict(platform_dist),
        "reddit_sampling_audit": reddit_audit,
        "app_store_shortfall_note": shortfall_note,
        "volume_warning": volume_warning,
    }
    stats["clean"] = clean_block
    atomic_write_text(s_path, json.dumps(stats, indent=2, ensure_ascii=False))
    summary.finish(s_path)

    _print_clean_terminal_report(clean_block)
    return 0


def _print_clean_terminal_report(cb: Dict[str, Any]) -> None:
    print("\n========================================")
    print("Stage 2: Clean — Terminal Summary")
    print("========================================")
    print(f"Total records in : {cb['records_in']:,}")
    print(f"Total records out: {cb['records_out']:,}  (dropped: {cb['dropped_total']:,})")

    print("\n1. Per-Source In / Out / Final Share %:")
    for src in sorted(cb["per_source_in"].keys()):
        n_in = cb["per_source_in"].get(src, 0)
        n_out = cb["per_source_out"].get(src, 0)
        share = cb["per_source_share_pct"].get(src, 0.0)
        print(f"  {src:<12} in={n_in:5d}  ->  out={n_out:5d}  ({share:5.1f}%)")

    print("\n2. Drops by Reason and Source:")
    for src, reasons in sorted(cb["dropped_by_source_and_reason"].items()):
        parts = ", ".join(f"{r}={cnt}" for r, cnt in reasons.items())
        print(f"  {src:<12} {parts}")

    print("\n3. Language Drops by Non-Latin Script:")
    if cb["language_drops_by_script"]:
        for sc, by_src in sorted(cb["language_drops_by_script"].items()):
            tot = sum(by_src.values())
            src_str = ", ".join(f"{s}:{n}" for s, n in sorted(by_src.items()))
            print(f"  {sc:<16} total={tot:4d}  ({src_str})")
    else:
        print("  (none)")

    print(f"\n4. Hinglish Retained Posts: {cb['hinglish_total']:,}  {cb['hinglish_count_by_source']}")

    print("\n5. PII Masked Counts by Type (counts only, values scrubbed in-place):")
    if cb["pii_scrubbed_by_source_and_type"]:
        for src, types in sorted(cb["pii_scrubbed_by_source_and_type"].items()):
            posts_n = cb["pii_posts_scrubbed_by_source"].get(src, 0)
            t_str = ", ".join(f"{k}={v}" for k, v in sorted(types.items()))
            print(f"  {src:<12} posts_scrubbed={posts_n:4d}  ({t_str})")
    else:
        print("  (0 PII patterns matched)")

    print(f"\n6. clean_borderline Retained Posts: {cb['borderline_total']:,}  {cb['borderline_count_by_source']}")

    print("\n7. platform_tag Distribution:")
    for ptag, cnt in sorted(cb["platform_tag_distribution"].items(), key=lambda kv: -kv[1]):
        print(f"  {ptag:<14} {cnt:5d}")

    audit = cb.get("reddit_sampling_audit") or {}
    if audit:
        print(
            f"\n8. Reddit Stratified Sampling Audit "
            f"(before={audit.get('before_total', 0)}, after={audit.get('after_total', 0)}, "
            f"archived_overflow={audit.get('overflow_archived', 0)}):"
        )
        print("  By quality_tier (before -> after): " + ", ".join(
            f"tier {t}: {v['before']}->{v['after']}" for t, v in audit.get("by_quality_tier", {}).items()
        ))
        print("  By theme bucket (before -> after):")
        for b_name, b_vals in audit.get("by_theme_bucket", {}).items():
            print(
                f"    {b_name:<28} {b_vals['before_count']:4d} ({b_vals['before_pct']:4.1f}%)  ->  "
                f"{b_vals['after_count']:4d} ({b_vals['after_pct']:4.1f}%)"
            )

    if cb.get("app_store_shortfall_note"):
        print(f"\nNote: {cb['app_store_shortfall_note']}")
    if cb.get("volume_warning"):
        print(f"WARNING: {cb['volume_warning']}")
    print("========================================\n")
