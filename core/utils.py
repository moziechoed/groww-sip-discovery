"""Shared utilities: username hashing (FR1.4), verbatim evidence verification (FR3.5), text helpers."""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_SALT_PLACEHOLDER_PREFIXES = (
    "your-",
    "your_",
    "<",
    "changeme",
    "xxx",
    "placeholder",
    "default",
    "groww-sip-default-salt",
)


def is_valid_salt(value: str | None) -> bool:
    if not value:
        return False
    s = value.strip()
    if len(s) < 8:
        return False
    low = s.lower()
    if any(low.startswith(p) or low == p for p in _SALT_PLACEHOLDER_PREFIXES):
        return False
    return True


def require_author_salt(salt: str | None = None) -> str:
    """Return a validated AUTHOR_HASH_SALT or raise ValueError if missing/placeholder (FR1.4)."""
    if salt is None:
        try:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env", override=False)
        except ImportError:
            pass
        salt = os.environ.get("AUTHOR_HASH_SALT")
    if not is_valid_salt(salt):
        raise ValueError(
            "AUTHOR_HASH_SALT is missing or set to a placeholder in .env. "
            "Set a real random salt (>= 8 chars) in .env before running collection (FR1.4)."
        )
    return salt.strip()


def hash_author(username: str, salt: str | None = None) -> str:
    """Hash username with SHA-256 + validated salt so plain usernames are never stored (FR1.4)."""
    s = require_author_salt(salt)
    raw = f"{s}:{(username or 'anonymous').strip()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


_WS = re.compile(r"\s+")
# Typographic variants that are the same character to a reader (not a paraphrase): curly vs straight quotes,
# dash styles, the ellipsis character, non-breaking and zero-width spaces.
_TYPO = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'", "`": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u00a0": " ", "\u2009": " ", "\u202f": " ", "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",
})
_EDGE_PUNCT = " \t\n.,;:!?\"'()[]"


def _norm_for_evidence(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").translate(_TYPO).replace("\u2026", "...")
    return _WS.sub(" ", t).strip().lower()


def verify_evidence_span(span: str | None, post_text: str) -> bool:
    """Verbatim check (FR3.5): the span must appear in the post as one contiguous phrase. Ignores only case,
    whitespace, typographic variants (quote/dash styles, ellipsis character) and punctuation at the span's
    two ends. Wording, spelling and order must match exactly."""
    if not span or not span.strip():
        return False
    norm_span = _norm_for_evidence(span).strip(_EDGE_PUNCT)
    if len(norm_span) < 3:
        return False
    return norm_span in _norm_for_evidence(post_text)
