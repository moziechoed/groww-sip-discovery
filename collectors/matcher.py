"""Whole-word, case-insensitive keyword matcher for app-store reviews (P1).

Rules (locked 24 Oct):
- Matching is whole-word: "nav" never matches "navigation", "sip" never matches "gossip".
- A space inside a term also matches a hyphen or no space ("auto pay" ~ "autopay" ~ "auto-pay").
- Apostrophes are ignored on both sides ("didn't" ~ "didn’t" ~ "didnt").
- Anchors (SIP/MF terms) match on their own.
- Phrases from keywords.core / keywords.continuer / keywords.hinglish that contain an anchor
  ("sip band", "kept my SIP") match on their own. Phrases without an anchor ("band kar diya",
  "paisa kat", "still going") only count when an anchor also appears in the same review.
- keyword_group = "continuer" when any continuer phrase counts, else "core".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Pattern, Tuple

from core.config import KeywordsCfg

_APOSTROPHES = re.compile(r"['’‘`´]")
_SEPARATORS = re.compile(r"[\s\-]+")


def normalise(text: str) -> str:
    return _APOSTROPHES.sub("", (text or "").lower())


def compile_term(term: str) -> Pattern[str]:
    words = [w for w in _SEPARATORS.split(normalise(term).strip()) if w]
    if not words:
        raise ValueError(f"Empty keyword: {term!r}")
    body = r"[\s\-]*".join(re.escape(w) for w in words)
    # (?<!\w) / (?!\w) = word boundaries that also work when a term starts or ends with a digit.
    return re.compile(rf"(?<!\w){body}(?!\w)")


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    matched_keywords: Tuple[str, ...]
    keyword_group: str  # "core" | "continuer"
    matched_via: str = "self"  # "self" | "thread_context"


@dataclass(frozen=True)
class _Phrase:
    term: str
    group: str
    pattern: Pattern[str]
    standalone: bool  # True if the phrase itself contains an anchor


class KeywordMatcher:
    def __init__(self, anchors: Iterable[str], core: Iterable[str], continuer: Iterable[str],
                 hinglish: Iterable[str] = ()):
        self.anchors: List[Tuple[str, Pattern[str]]] = []
        seen = set()
        for a in anchors:
            key = normalise(a).strip()
            if key and key not in seen:
                seen.add(key)
                self.anchors.append((key, compile_term(a)))
        if not self.anchors:
            raise ValueError("keywords.anchors is empty; the app-store matcher needs at least one SIP/MF anchor term")

        self.phrases: List[_Phrase] = []
        seen_phrases = set()
        for group, terms in (("core", core), ("core", hinglish), ("continuer", continuer)):
            for t in terms:
                key = normalise(t).strip()
                if not key or (key, group) in seen_phrases:
                    continue
                seen_phrases.add((key, group))
                standalone = any(p.search(key) for _, p in self.anchors)
                self.phrases.append(_Phrase(key, group, compile_term(t), standalone))

    @classmethod
    def from_settings(cls, kw: KeywordsCfg) -> "KeywordMatcher":
        return cls(kw.anchors, kw.core, kw.continuer, kw.hinglish)

    def has_anchor(self, text: str) -> bool:
        t = normalise(text)
        return any(p.search(t) for _, p in self.anchors)

    def match(self, text: str) -> MatchResult:
        t = normalise(text)
        hits: List[str] = [term for term, p in self.anchors if p.search(t)]
        has_anchor = bool(hits)
        continuer = False
        for ph in self.phrases:
            if (ph.standalone or has_anchor) and ph.pattern.search(t):
                if ph.term not in hits:
                    hits.append(ph.term)
                continuer = continuer or ph.group == "continuer"
        return MatchResult(bool(hits), tuple(hits), "continuer" if continuer else "core", "self")

    def match_with_context(self, text: str, *, thread_has_anchor: bool = False) -> MatchResult:
        """Match a comment directly ('self') or, if inside a SIP-anchored thread, via core/continuer/hinglish phrases ('thread_context')."""
        direct = self.match(text)
        if direct.matched or not thread_has_anchor:
            return direct
        t = normalise(text)
        hits: List[str] = []
        continuer = False
        for ph in self.phrases:
            if ph.pattern.search(t):
                if ph.term not in hits:
                    hits.append(ph.term)
                continuer = continuer or ph.group == "continuer"
        return MatchResult(bool(hits), tuple(hits), "continuer" if continuer else "core", "thread_context")


# ----------------------------------------------------------------------------- thread_context first-person + behaviour rule (P4/P5)

_THREAD_FIRST_PERSON_TERMS = (
    "i",
    "my",
    "mine",
    "me",
    "maine",
    "mera",
    "meri",
    "mujhe",
)
_THREAD_BEHAVIOUR_TERMS = (
    "stop",
    "stopped",
    "stopping",
    "pause",
    "paused",
    "pausing",
    "cancel",
    "cancelled",
    "cancelling",
    "continue",
    "continued",
    "continuing",
    "start",
    "started",
    "starting",
    "withdraw",
    "withdrew",
    "withdrawn",
    "redeem",
    "redeemed",
    "redeeming",
    "return",
    "returns",
    "loss",
    "losses",
    "profit",
    "profits",
    "salary",
    "emergency",
    "month",
    "months",
    "year",
    "years",
    "invest",
    "invested",
    "investing",
)
_THREAD_CONTINUER_BEHAVIOUR = {"continue", "continued", "continuing"}
_THREAD_DROPOFF_BEHAVIOUR = {
    "stop",
    "stopped",
    "stopping",
    "pause",
    "paused",
    "pausing",
    "cancel",
    "cancelled",
    "cancelling",
    "withdraw",
    "withdrew",
    "withdrawn",
    "redeem",
    "redeemed",
    "redeeming",
}

_THREAD_FIRST_PERSON_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _THREAD_FIRST_PERSON_TERMS) + r")\b",
    re.IGNORECASE,
)
_THREAD_BEHAVIOUR_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _THREAD_BEHAVIOUR_TERMS) + r")\b",
    re.IGNORECASE,
)


def match_anchored_reply(text: str, matcher: KeywordMatcher) -> MatchResult:
    """Match a comment/reply inside a SIP/MF-anchored thread (YouTube P4, TradingQ&A P5).

    1. Runs `matcher.match_with_context(text, thread_has_anchor=True)`.
    2. If not matched, checks the first-person + behaviour rule: whole-word first-person marker
       (`i`, `my`, `mine`, `me`, `maine`, `mera`, `meri`, `mujhe`) AND whole-word behaviour term
       (`stop/stopped`, `pause/paused`, `cancel`, `continue/continuing`, `started`, `withdraw`,
        `redeem`, `returns`, `loss`, `profit`, `salary`, `emergency`, `months`, `years`, `invest/investing`).
       When both appear, returns `matched=True, matched_via="thread_context"`.
    """
    m = matcher.match_with_context(text, thread_has_anchor=True)
    if m.matched or not text:
        return m

    fp_hits: List[str] = []
    for match in _THREAD_FIRST_PERSON_RE.finditer(text):
        w = match.group(1).lower()
        if w not in fp_hits:
            fp_hits.append(w)
    if not fp_hits:
        return m

    beh_hits: List[str] = []
    for match in _THREAD_BEHAVIOUR_RE.finditer(text):
        w = match.group(1).lower()
        if w not in beh_hits:
            beh_hits.append(w)
    if not beh_hits:
        return m

    beh_set = set(beh_hits)
    if (beh_set & _THREAD_CONTINUER_BEHAVIOUR) and not (beh_set & _THREAD_DROPOFF_BEHAVIOUR):
        group = "continuer"
    else:
        group = "core"

    return MatchResult(
        matched=True,
        keyword_group=group,
        matched_keywords=tuple(fp_hits + beh_hits),
        matched_via="thread_context",
    )

