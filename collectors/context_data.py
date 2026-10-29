"""Stage 1 Part 2: Context data builder (`data/context/` — PRD §9.1, Architecture §4.3).

Context data is NOT part of the user-conversation corpus (`data/raw/raw_posts.jsonl`) and never
enters Stage 2 (Clean) or Stage 3 (Enrich). Zero LLM calls.

Outputs under `data/context/`:
  1. `amfi_sip_monthly.csv`:
     - Last 24 months of AMFI SIP monthly figures (`month`, `period_type`, `outstanding_sip_accounts_lakhs`,
       `new_sips_registered_lakhs`, `sips_discontinued_or_matured_lakhs`, `contributing_sip_accounts_lakhs`,
       `sip_contribution_inr_crore`, `sip_aum_inr_crore`, `source_url`) plus the 3 fiscal-year summary rows
       (`FY 2023-24`, `FY 2024-25`, `FY 2025-26`) published on `https://www.amfiindia.com/mutual-fund`.
     - Also supports parsing a locally downloaded HTML snapshot (`data/context/raw/amfi_mutual_fund.html`)
       if offline or if AMFI blocks live requests.
  2. `factbook_demographics.csv`:
     - Extracts investor demographic tables (age bands, gender, city tier T30 vs B30, holding period,
       plan mix by ticket size, and state-wise age allocation) from the official AMFI–Crisil Factbook 2026 PDF
       (`https://www.amfiindia.com/uploads/AMFI_Factbook2026_14b34a6447.pdf`, cached once at
       `data/context/raw/amfi_crisil_factbook_2026.pdf`).
     - Every row includes `pdf_page`, `confidence` (`high` | `low`), `source_url`, and `notes`.
     - Any `low`-confidence rows (where a chart's data labels in the PDF are rendered as vector graphics
       rather than selectable table cells) are printed with their `pdf_page` number for manual check.
  3. `mfsh_index.csv`:
     - Discovers real URLs strictly from `https://www.mutualfundssahihai.com/sitemap.xml` ->
       `sitemap-0.xml` (never constructs or guesses URLs).
     - Fetches each candidate `/en/` page, verifies `http_status == 200`, and extracts `title` (`<h1>` or
       `<title>`) and `summary` verbatim from the page's own `<meta name="description">`, `og:description`,
       or first body paragraph (never written by the agent).
     - Reports any sub-topics (e.g. a dedicated standalone XIRR page) that have no real page on the site.
"""
from __future__ import annotations

import csv
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse
import warnings

import requests
import urllib3
from pypdf import PdfReader

from collectors.base import atomic_write_text, iso_z, read_json
from core.config import Settings
from core.logger import get_logger

log = get_logger("context")

_AMFI_ALLOWED_SSL_FALLBACK_HOSTS = frozenset({"amfiindia.com", "www.amfiindia.com", "portal.amfiindia.com"})


def _get_amfi_url(session: requests.Session, url: str, timeout: float = 25.0) -> requests.Response:
    """Fetch an `amfiindia.com` URL with standard TLS verification first; if macOS OpenSSL fails on AMFI's
    incomplete certificate chain, log a warning and retry `verify=False` ONLY when the hostname is `amfiindia.com`."""
    try:
        return session.get(url, timeout=timeout, verify=True)
    except requests.exceptions.SSLError:
        host = (urlparse(url).hostname or "").lower()
        if host not in _AMFI_ALLOWED_SSL_FALLBACK_HOSTS:
            raise
        log.warning("AMFI SSL chain incomplete on macOS OpenSSL; retrying %s with verify=False (scoped to %s)", url, host)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            return session.get(url, timeout=timeout, verify=False)

AMFI_SIP_URL = "https://www.amfiindia.com/mutual-fund"
FACTBOOK_2026_URL = "https://www.amfiindia.com/uploads/AMFI_Factbook2026_14b34a6447.pdf"
MFSH_SITEMAP_INDEX_URL = "https://www.mutualfundssahihai.com/sitemap.xml"
MFSH_SITEMAP_FALLBACK_URL = "https://www.mutualfundssahihai.com/sitemap-0.xml"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

AMFI_CSV_COLUMNS = (
    "month",
    "period_type",
    "outstanding_sip_accounts_lakhs",
    "new_sips_registered_lakhs",
    "sips_discontinued_or_matured_lakhs",
    "contributing_sip_accounts_lakhs",
    "sip_contribution_inr_crore",
    "sip_aum_inr_crore",
    "source_url",
)

FACTBOOK_CSV_COLUMNS = (
    "dimension",
    "segment",
    "metric",
    "period",
    "value",
    "unit",
    "pdf_page",
    "confidence",
    "source_url",
    "notes",
)

MFSH_CSV_COLUMNS = (
    "topic",
    "title",
    "url",
    "summary",
    "summary_source",
    "http_status",
    "discovered_from",
)

# Month names in Table 0 of amfiindia.com/mutual-fund mapped to (month_num, abbrev)
_MONTH_MAP: Dict[str, Tuple[int, str]] = {
    "april": (4, "Apr"),
    "apr": (4, "Apr"),
    "may": (5, "May"),
    "june": (6, "Jun"),
    "jun": (6, "Jun"),
    "july": (7, "Jul"),
    "jul": (7, "Jul"),
    "august": (8, "Aug"),
    "aug": (8, "Aug"),
    "september": (9, "Sep"),
    "sep": (9, "Sep"),
    "october": (10, "Oct"),
    "oct": (10, "Oct"),
    "november": (11, "Nov"),
    "nov": (11, "Nov"),
    "december": (12, "Dec"),
    "dec": (12, "Dec"),
    "january": (1, "Jan"),
    "jan": (1, "Jan"),
    "february": (2, "Feb"),
    "feb": (2, "Feb"),
    "march": (3, "Mar"),
    "mar": (3, "Mar"),
}


def _clean_num(val: Any) -> str:
    """Normalise numeric string ('1,90,598' -> '190598', '' -> '')."""
    if val is None:
        return ""
    s = str(val).strip().replace(",", "")
    if not s or s in ("-", "—", "N/A", "null", "None"):
        return ""
    return s


def _strip_tags(raw_html: str) -> str:
    no_script = re.sub(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", " ", raw_html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", no_script)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


# ----------------------------------------------------------------------------- 1. AMFI Monthly SIP Data


def parse_amfi_sip_html(html_text: str, source_url: str = AMFI_SIP_URL, max_months: int = 24) -> List[Dict[str, str]]:
    """Parse AMFI's `/mutual-fund` HTML (Table 0: multi-year monthly SIP contribution matrix;
    Table 1: monthly/annual SIP accounts, registrations, discontinuations, contributing accounts, and AUM).

    Returns up to `max_months` monthly rows (newest-first, YYYY-MM) followed by the fiscal-year summary rows.
    """
    no_style = re.sub(r"<style[^>]*>.*?</style>", "", html_text, flags=re.I | re.S)
    tables = re.findall(r"<table[^>]*>.*?</table>", no_style, flags=re.I | re.S)

    # Parse Table 0: Month x FY matrix of SIP Contribution (₹ crore)
    monthly_contrib: Dict[str, str] = {}  # "YYYY-MM" -> contribution_crore
    if len(tables) >= 1:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tables[0], flags=re.I | re.S)
        fy_headers: List[Tuple[int, int]] = []  # (start_year, end_year)
        for r in rows:
            cells = [_strip_tags(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, flags=re.I | re.S)]
            if not cells:
                continue
            # Detect FY header row: e.g. ['FY 2026-27', 'FY 2025-26', ...]
            fy_matches = [re.search(r"FY\s*(\d{4})\s*[-–]\s*(\d{2,4})", c, flags=re.I) for c in cells]
            if any(fy_matches):
                fy_headers = []
                for m in fy_matches:
                    if m:
                        y1 = int(m.group(1))
                        y2_raw = int(m.group(2))
                        y2 = (y1 // 100) * 100 + y2_raw if y2_raw < 100 else y2_raw
                        fy_headers.append((y1, y2))
                continue

            first = cells[0].strip().lower()
            if first in _MONTH_MAP and fy_headers:
                m_num, _ = _MONTH_MAP[first]
                for idx, (y_start, y_end) in enumerate(fy_headers):
                    col_idx = idx + 1
                    if col_idx >= len(cells):
                        break
                    val = _clean_num(cells[col_idx])
                    if not val:
                        continue
                    cal_year = y_start if m_num >= 4 else y_end
                    ym = f"{cal_year:04d}-{m_num:02d}"
                    monthly_contrib[ym] = val

    # Parse Table 1: Detailed SIP Accounts, Registrations, Discontinuations, Contributing Accounts, Contribution, AUM
    detailed_by_ym: Dict[str, Dict[str, str]] = {}
    fy_summary_rows: List[Dict[str, str]] = []

    if len(tables) >= 2:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tables[1], flags=re.I | re.S)
        for r in rows:
            cells = [_strip_tags(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, flags=re.I | re.S)]
            if len(cells) < 7:
                continue
            label = cells[0].strip()
            if label.lower() == "month":
                continue

            # Check if label is a single month like "Sep-26", "July -26", "Apr -26"
            m_single = re.match(r"^([A-Za-z]+)\s*[-–]\s*(\d{2,4})$", label)
            if m_single and m_single.group(1).lower() in _MONTH_MAP:
                m_num, _ = _MONTH_MAP[m_single.group(1).lower()]
                yr_raw = int(m_single.group(2))
                yr = 2000 + yr_raw if yr_raw < 100 else yr_raw
                ym = f"{yr:04d}-{m_num:02d}"
                detailed_by_ym[ym] = {
                    "outstanding_sip_accounts_lakhs": _clean_num(cells[1]),
                    "new_sips_registered_lakhs": _clean_num(cells[2]),
                    "sips_discontinued_or_matured_lakhs": _clean_num(cells[3]),
                    "contributing_sip_accounts_lakhs": _clean_num(cells[4]),
                    "sip_contribution_inr_crore": _clean_num(cells[5]),
                    "sip_aum_inr_crore": _clean_num(cells[6]),
                }
            else:
                # Annual / multi-month summary row (e.g., "Apr 25 – Mar 26", "Apr 24 – Mar 25", "Apr 23 – Mar 24")
                norm_label = re.sub(r"\s*[–-]\s*", " - ", label)
                if norm_label.lower().startswith("apr 26"):
                    # Skip partial-year subtotal since individual Apr-26..Sep-26 months are present
                    continue
                fy_summary_rows.append(
                    {
                        "month": norm_label,
                        "period_type": "annual_summary",
                        "outstanding_sip_accounts_lakhs": _clean_num(cells[1]),
                        "new_sips_registered_lakhs": _clean_num(cells[2]),
                        "sips_discontinued_or_matured_lakhs": _clean_num(cells[3]),
                        "contributing_sip_accounts_lakhs": _clean_num(cells[4]),
                        "sip_contribution_inr_crore": _clean_num(cells[5]),
                        "sip_aum_inr_crore": _clean_num(cells[6]),
                        "source_url": source_url,
                    }
                )

    all_yms = sorted(set(monthly_contrib.keys()) | set(detailed_by_ym.keys()), reverse=True)
    selected_yms = all_yms[:max_months]

    out: List[Dict[str, str]] = []
    for ym in selected_yms:
        det = detailed_by_ym.get(ym, {})
        out.append(
            {
                "month": ym,
                "period_type": "monthly",
                "outstanding_sip_accounts_lakhs": det.get("outstanding_sip_accounts_lakhs", ""),
                "new_sips_registered_lakhs": det.get("new_sips_registered_lakhs", ""),
                "sips_discontinued_or_matured_lakhs": det.get("sips_discontinued_or_matured_lakhs", ""),
                "contributing_sip_accounts_lakhs": det.get("contributing_sip_accounts_lakhs", ""),
                "sip_contribution_inr_crore": det.get("sip_contribution_inr_crore") or monthly_contrib.get(ym, ""),
                "sip_aum_inr_crore": det.get("sip_aum_inr_crore", ""),
                "source_url": source_url,
            }
        )

    out.extend(fy_summary_rows)
    return out


# ----------------------------------------------------------------------------- 2. AMFI-Crisil Factbook 2026 PDF


def extract_factbook_demographics(
    pdf_path: Path,
    source_url: str = FACTBOOK_2026_URL,
) -> List[Dict[str, str]]:
    """Extract investor demographic tables & metrics from the AMFI–Crisil Factbook 2026 PDF.

    Every row includes `pdf_page`, `confidence` (`high` | `low`), `source_url`, and `notes`.
    - `confidence = "high"`: value is explicitly printed in the selectable text or Annexure table of `pdf_page`
      and verified present in the extracted page text.
    - `confidence = "low"`: value is from a chart whose data labels are rendered as vector graphics or
      where the exact breakdown is partially unlabelled in selectable text, flagged for manual verification.
    """
    reader = PdfReader(str(pdf_path))
    num_pages = len(reader.pages)
    page_texts: Dict[int, str] = {}
    for pno in range(1, num_pages + 1):
        raw = reader.pages[pno - 1].extract_text() or ""
        page_texts[pno] = re.sub(r"\s+", " ", raw).strip()

    rows: List[Dict[str, str]] = []

    def add_row(
        *,
        dimension: str,
        segment: str,
        metric: str,
        period: str,
        value: str,
        unit: str,
        pdf_page: int,
        verify_token: Optional[str] = None,
        force_low_confidence: bool = False,
        notes: str = "",
    ) -> None:
        txt = page_texts.get(pdf_page, "")
        token_ok = True
        if verify_token:
            token_ok = verify_token.lower() in txt.lower()
        conf = "low" if (force_low_confidence or not token_ok) else "high"
        rows.append(
            {
                "dimension": dimension,
                "segment": segment,
                "metric": metric,
                "period": period,
                "value": str(value),
                "unit": unit,
                "pdf_page": str(pdf_page),
                "confidence": conf,
                "source_url": source_url,
                "notes": notes,
            }
        )

    # 1. Unique Investor Base (Page 22 / Report p. 12)
    add_row(
        dimension="investor_base",
        segment="Unique Individual Investors",
        metric="unique_investors_count",
        period="Mar-2021",
        value="2.3",
        unit="crore",
        pdf_page=22,
        verify_token="2.3 crore",
        notes="Unique mutual fund investors rose from 2.3 crore in Mar 2021 to 6.1 crore in Mar 2026",
    )
    add_row(
        dimension="investor_base",
        segment="Unique Individual Investors",
        metric="unique_investors_count",
        period="Mar-2026",
        value="6.1",
        unit="crore",
        pdf_page=22,
        verify_token="6.1 crore",
        notes="Nearly 2.7x growth in unique individual investors between Mar 2021 and Mar 2026",
    )

    # 2. Age Bands — SIP AUM as % of Industry AUM across Age Groups (Page 40-41 / Report p. 30-31)
    age_sip_rows = [
        ("Below 18 years", "Mar-2021", "37.3", "37.3%"),
        ("Below 18 years", "Mar-2026", "46.0", "46.0%"),
        ("18 to 34 years", "Mar-2026", "32.7", "32.7%"),
        ("35 to 58 years", "Mar-2021", "29.8", "29.8%"),
        ("35 to 58 years", "Mar-2026", "40.6", "40.6%"),
        ("Above 58 years", "Mar-2021", "11.4", "11.4%"),
        ("Above 58 years", "Mar-2026", "20.6", "20.6%"),
    ]
    for seg, per, val, tok in age_sip_rows:
        add_row(
            dimension="age_band",
            segment=seg,
            metric="sip_aum_share_of_age_group_aum",
            period=per,
            value=val,
            unit="percent",
            pdf_page=40,
            verify_token=tok,
            notes="Evolution of SIP AUM as a percentage of industry AUM by age group",
        )
    # 18-34 Mar-2021 exact bar label is inside the vector chart on p. 41, while text on p. 40 says "maintained a relatively stable presence, with a modest adjustment in its proportion to 32.7%"
    add_row(
        dimension="age_band",
        segment="18 to 34 years",
        metric="sip_aum_share_of_age_group_aum",
        period="Mar-2021",
        value="37.0",
        unit="percent",
        pdf_page=41,
        force_low_confidence=True,
        notes="Chart bar on PDF page 41 (22.6% direct + 14.4% regular = 37.0%); prose on page 40 states 32.7% for Mar-2026 without printing the Mar-2021 sum inline",
    )
    add_row(
        dimension="age_band",
        segment="18 to 34 years (Direct SIP)",
        metric="direct_sip_aum_share_of_age_group_aum",
        period="Mar-2021",
        value="22.6",
        unit="percent",
        pdf_page=41,
        verify_token="22.6%",
        notes="Younger populace prefers direct route for SIP investments",
    )
    add_row(
        dimension="age_band",
        segment="18 to 34 years (Direct SIP)",
        metric="direct_sip_aum_share_of_age_group_aum",
        period="Mar-2026",
        value="22.7",
        unit="percent",
        pdf_page=41,
        verify_token="22.7%",
        notes="Direct SIP AUM share in 18-34 age group",
    )
    add_row(
        dimension="age_band",
        segment="18 to 34 years (Regular SIP)",
        metric="regular_sip_aum_share_of_age_group_aum",
        period="Mar-2021",
        value="14.4",
        unit="percent",
        pdf_page=41,
        verify_token="14.4%",
        notes="Regular SIP AUM share in 18-34 age group",
    )
    add_row(
        dimension="age_band",
        segment="18 to 34 years (Regular SIP)",
        metric="regular_sip_aum_share_of_age_group_aum",
        period="Mar-2026",
        value="11.9",
        unit="percent",
        pdf_page=41,
        verify_token="11.9%",
        notes="Regular SIP AUM share in 18-34 age group declined from 14.4% to 11.9%",
    )

    # 3. City Tier — T30 vs B30 AUM (Page 29 / Report p. 19, and Page 49 / Report p. 39)
    city_rows = [
        ("T30 (Top 30 cities)", "industry_aum", "Mar-2021", "26.2", "inr_lakh_crore", 29, "26.2 lakh crore", False, "T30 mutual fund AUM"),
        ("T30 (Top 30 cities)", "industry_aum", "Mar-2026", "59.8", "inr_lakh_crore", 29, "59.8 lakh crore", False, "T30 mutual fund AUM"),
        ("B30 (Beyond top 30 cities)", "industry_aum", "Mar-2021", "5.2", "inr_lakh_crore", 29, "5.2 lakh crore", False, "B30 mutual fund AUM"),
        ("B30 (Beyond top 30 cities)", "industry_aum", "Mar-2026", "13.9", "inr_lakh_crore", 29, "13.9 lakh crore", False, "B30 mutual fund AUM"),
        ("T30 (Top 30 cities)", "industry_aum_share", "Mar-2026", "81.1", "percent", 29, None, True, "Computed from 59.8 / (59.8 + 13.9) lakh crore; pie chart percentage label on PDF page 29 is vector-rendered"),
        ("B30 (Beyond top 30 cities)", "industry_aum_share", "Mar-2026", "18.9", "percent", 29, None, True, "Computed from 13.9 / (59.8 + 13.9) lakh crore; pie chart percentage label on PDF page 29 is vector-rendered"),
        ("T30 — Women Investors", "gender_city_aum_share", "Mar-2026", "74.0", "percent", 49, "74%", False, "Share of women investors' AUM in T30 cities"),
        ("B30 — Women Investors", "gender_city_aum_share", "Mar-2021", "23.0", "percent", 49, "23%", False, "Share of women investors' AUM in B30 cities"),
        ("B30 — Women Investors", "gender_city_aum_share", "Mar-2026", "26.0", "percent", 49, "26%", False, "Share of women investors' AUM in B30 cities"),
        ("T30 — Men Investors", "gender_city_aum_share", "Mar-2026", "73.0", "percent", 49, "73%", False, "Share of men investors' AUM in T30 cities"),
        ("B30 — Men Investors", "gender_city_aum_share", "Mar-2021", "26.0", "percent", 49, "26%", False, "Share of men investors' AUM in B30 cities"),
        ("B30 — Men Investors", "gender_city_aum_share", "Mar-2026", "27.0", "percent", 49, "27%", False, "Share of men investors' AUM in B30 cities"),
    ]
    for seg, met, per, val, unit, pg, tok, low_flag, note in city_rows:
        add_row(
            dimension="city_tier",
            segment=seg,
            metric=met,
            period=per,
            value=val,
            unit=unit,
            pdf_page=pg,
            verify_token=tok,
            force_low_confidence=low_flag,
            notes=note,
        )

    # 4. Gender Demographics (Pages 45, 46, 50, 51, 53, 55)
    gender_rows = [
        ("Women", "share_of_individual_aum", "Mar-2021", "32.6", "percent", 45, "32.6%", "Participation of women investors in MF AUM"),
        ("Women", "share_of_individual_aum", "Mar-2026", "34.5", "percent", 45, "34.5%", "Participation of women investors in MF AUM"),
        ("Men", "share_of_individual_aum", "Mar-2021", "61.3", "percent", 45, "61.3%", "Participation of men investors in MF AUM"),
        ("Women", "share_of_unique_investors", "Mar-2026", "26.4", "percent", 46, "26.4%", "1.61 crore women out of 6.09 crore individual investors"),
        ("Women", "unique_investors_count", "Mar-2026", "1.61", "crore", 46, "1.61 crore", "Total women mutual fund investors"),
        ("Women", "sip_aum", "Mar-2021", "1.26", "inr_lakh_crore", 53, "1.26 lakh crore", "Women investors' SIP AUM"),
        ("Women", "sip_aum", "Mar-2025", "4.19", "inr_lakh_crore", 53, "4.19 lakh crore", "Women investors' SIP AUM"),
        ("Women", "sip_aum", "Mar-2026", "4.73", "inr_lakh_crore", 53, "4.73 lakh crore", "Women investors' SIP AUM grew nearly 4x from Mar 2021"),
        ("Women (Below 25 years)", "equity_allocation_share", "Mar-2026", "88.3", "percent", 50, "88.3%", "Age-wise equity allocation among women investors"),
        ("Women (25 to 44 years)", "equity_allocation_share", "Mar-2026", "76.2", "percent", 50, "76.2%", "Age-wise equity allocation among women investors"),
        ("Women (45 to 58 years)", "equity_allocation_share", "Mar-2026", "64.8", "percent", 50, "64.8%", "Age-wise equity allocation among women investors"),
        ("Women (Above 58 years)", "equity_allocation_share", "Mar-2026", "51.2", "percent", 50, "51.2%", "Age-wise equity allocation among women investors"),
        ("Women (Below 25 years)", "direct_plan_aum_share", "Mar-2021", "18.0", "percent", 51, "18%", "Direct plan share among women below 25 years"),
        ("Women (Below 25 years)", "direct_plan_aum_share", "Mar-2026", "27.0", "percent", 51, "27%", "Direct plan share among women below 25 years"),
        ("Women (25 to 44 years)", "direct_plan_aum_share", "Mar-2026", "36.0", "percent", 51, "36%", "Direct plan share among women aged 25-44 years"),
        ("Women (SIP <Rs 500)", "direct_plan_account_share", "Mar-2026", "60.0", "percent", 55, "60%", "For smaller SIPs (<Rs 500), direct plans dominate at 60%"),
        ("Women (SIP Rs 500-1,000)", "regular_plan_account_share", "Mar-2026", "62.0", "percent", 55, "62%", "Regular plan share for Rs 500-1,000 SIPs"),
        ("Women (SIP Rs 1,001-5,000)", "regular_plan_account_share", "Mar-2026", "64.0", "percent", 55, "64%", "Regular plan share for Rs 1,001-5,000 SIPs"),
    ]
    for seg, met, per, val, unit, pg, tok, note in gender_rows:
        add_row(
            dimension="gender",
            segment=seg,
            metric=met,
            period=per,
            value=val,
            unit=unit,
            pdf_page=pg,
            verify_token=tok,
            notes=note,
        )
    # Men Mar-2026 share in donut chart on p. 45 is partially merged in PDF text extraction (100 - 34.5 - 1.2 - 5.2 = 59.1%)
    add_row(
        dimension="gender",
        segment="Men",
        metric="share_of_individual_aum",
        period="Mar-2026",
        value="59.1",
        unit="percent",
        pdf_page=45,
        force_low_confidence=True,
        notes="Donut chart on PDF page 45 lists Women 34.5%, Minor 1.2%, Others 5.2% in selectable text; Men Mar-2026 label (59.1%) is inside the graphic",
    )

    # 5. Holding Period & Channel Mix (Pages 26, 27, 42, 43, 52)
    holding_rows = [
        ("Industry AUM (<1 year)", "aum_holding_period_share", "Mar-2021", "40.7", "percent", 26, "40.7%", "Industry AUM holding period <1 year"),
        ("Industry AUM (1 to 5 years)", "aum_holding_period_share", "Mar-2021", "51.6", "percent", 26, "51.6%", "Industry AUM holding period 1-5 years"),
        ("Industry AUM (>5 years)", "aum_holding_period_share", "Mar-2021", "7.7", "percent", 26, "7.7%", "Industry AUM holding period >5 years"),
        ("Industry AUM (<1 year)", "aum_holding_period_share", "Mar-2026", "34.0", "percent", 26, "34.0%", "Industry AUM holding period <1 year"),
        ("Industry AUM (1 to 5 years)", "aum_holding_period_share", "Mar-2026", "46.8", "percent", 26, "46.8%", "Industry AUM holding period 1-5 years"),
        ("Industry AUM (>5 years)", "aum_holding_period_share", "Mar-2026", "19.2", "percent", 26, "19.2%", "Industry AUM holding period >5 years"),
        ("Retail Investors", "direct_plan_aum_share", "Mar-2021", "21.4", "percent", 27, "21.4%", "Retail investors direct plan share"),
        ("Retail Investors", "direct_plan_aum_share", "Mar-2026", "36.7", "percent", 27, "36.7%", "Retail investors direct plan share"),
        ("Retail Investors", "sip_share_of_segment_aum", "Mar-2021", "33.5", "percent", 43, "33.5%", "SIP AUM as % of total retail AUM"),
        ("Retail Investors", "sip_share_of_segment_aum", "Mar-2026", "45.0", "percent", 43, "45.0%", "SIP AUM as % of total retail AUM"),
        ("Regular Plans (>5 years holding)", "holding_over_5yr_share", "Mar-2026", "23.5", "percent", 42, "23.5%", "Share of regular investments held >5 years"),
        ("Direct Plans (>5 years holding)", "holding_over_5yr_share", "Mar-2026", "14.0", "percent", 42, "14%", "Share of direct investments held >5 years"),
        ("Women Investors (<1 year holding)", "aum_holding_period_share", "Mar-2021", "11.0", "percent", 52, "11%", "Women investors AUM held <1 year"),
        ("Women Investors (<1 year holding)", "aum_holding_period_share", "Mar-2026", "24.0", "percent", 52, "24%", "Women investors AUM held <1 year"),
        ("Women Investors (1 to 2 years holding)", "aum_holding_period_share", "Mar-2021", "35.0", "percent", 52, "35%", "Women investors AUM held 1-2 years"),
        ("Women Investors (1 to 2 years holding)", "aum_holding_period_share", "Mar-2026", "19.0", "percent", 52, "19%", "Women investors AUM held 1-2 years"),
        ("Women Investors (>5 years holding)", "aum_holding_period_share", "Mar-2021", "5.0", "percent", 52, "5%", "Women investors AUM held >5 years"),
        ("Women Investors (>5 years holding)", "aum_holding_period_share", "Mar-2026", "24.0", "percent", 52, "24%", "Women investors AUM held >5 years"),
    ]
    for seg, met, per, val, unit, pg, tok, note in holding_rows:
        add_row(
            dimension="holding_period_and_channel",
            segment=seg,
            metric=met,
            period=per,
            value=val,
            unit=unit,
            pdf_page=pg,
            verify_token=tok,
            notes=note,
        )

    # 6. Parse Annexure 3 (Page 72 / Report p. 62) dynamically for top states by AUM across age bands
    p72 = page_texts.get(72, "")
    state_pattern = re.compile(
        r"(Maharashtra|Gujarat|New Delhi|Karnataka|Uttar Pradesh|West Bengal|T\s*amil Nadu|T\s*elangana)\s+"
        r"(\d+\.\d+)%\s+(\d+\.\d+)%\s+(\d+\.\d+)%\s+(\d+\.\d+)%\s+(\d+\.\d+)%"
    )
    age_cols = ["Below 25", "25 to 44", "45 to 58", "Above 58", "Not specified"]
    for match in state_pattern.finditer(p72):
        st_name = re.sub(r"\s+", " ", match.group(1)).replace("T amil", "Tamil").replace("T elangana", "Telangana")
        for idx, age_label in enumerate(age_cols, start=2):
            val = match.group(idx)
            add_row(
                dimension="state_age_allocation",
                segment=f"{st_name} — {age_label}",
                metric="individual_aum_age_share",
                period="Mar-2026",
                value=val,
                unit="percent",
                pdf_page=72,
                verify_token=f"{val}%",
                notes="Annexure 3: Allocation of individuals' AUM across age groups and states as of March 31, 2026",
            )

    return rows


# ----------------------------------------------------------------------------- 3. Mutual Funds Sahi Hai Index


# Topic classification rules for URLs discovered from mutualfundssahihai.com/sitemap-0.xml
_MFSH_TOPIC_SLUGS: Tuple[Tuple[str, str], ...] = (
    # (url_slug_suffix, topic_category)
    ("/en/what-is-sip", "sip_basics"),
    ("/en/sip-and-how-it-works", "sip_basics"),
    ("/en/systematic-investment-plan-sip-and-how-it-works", "sip_basics"),
    ("/en/what-are-benefits-sip", "sip_basics"),
    ("/en/what-rupee-cost-averaging", "sip_basics"),
    ("/en/what-power-compounding", "sip_basics"),
    ("/en/what-difference-between-sip-and-mutual-fund", "sip_basics"),
    ("/en/what-step-sip", "sip_basics"),
    ("/en/choose-right-sip-amount-reach-your-goal", "sip_basics"),
    ("/en/how-should-i-choose-whether-go-sip-or-lumpsum", "sip_basics"),
    ("/en/how-do-i-startstop-sip-what-happens-if-i-miss-instalment", "pausing_stopping_missed_sip"),
    ("/en/what-happens-when-you-miss-sip-payments-between", "pausing_stopping_missed_sip"),
    ("/en/what-will-mutual-funds-do-if-two-or-more-installments-are-missed", "pausing_stopping_missed_sip"),
    ("/en/it-possible-change-sip-amount-every-month", "pausing_stopping_missed_sip"),
    ("/en/why-continue-investing-through-sips-volatile-market", "pausing_stopping_missed_sip"),
    ("/en/what-happens-when-market-falls-midway-while-you-have-invested-longer-term", "pausing_stopping_missed_sip"),
    ("/en/why-should-one-not-be-bothered-volatility-mutual-funds", "pausing_stopping_missed_sip"),
    ("/en/what-net-asset-value-nav", "returns_nav_cagr"),
    ("/en/how-nav-calculated", "returns_nav_cagr"),
    ("/en/should-high-or-low-nav-scheme-impact-your-investment-decision", "returns_nav_cagr"),
    ("/en/what-is-cagr-or-annualised-return", "returns_nav_cagr"),
    ("/en/what-absolute-return", "returns_nav_cagr"),
    ("/en/what-trailing-and-rolling-returns-mutual-funds", "returns_nav_cagr"),
    ("/en/what-total-return-index-tri-mutual-fund", "returns_nav_cagr"),
    ("/en/how-do-i-get-my-returns-mutual-funds", "returns_nav_cagr"),
    ("/en/what-kind-returns-should-one-expect-mutual-funds", "returns_nav_cagr"),
    ("/en/why-dont-mutual-funds-give-fixed-rate-return-saving-account-or-fd", "returns_nav_cagr"),
    ("/en/elss-fund-tax-saving-mutual-fund", "tax_and_elss"),
    ("/en/advantages-of-elss-funds", "tax_and_elss"),
    ("/en/should-i-invest-elss-through-sip-or-lumpsum", "tax_and_elss"),
    ("/en/who-should-invest-in-tax-saving-mutual-funds", "tax_and_elss"),
    ("/en/what-are-the-taxation-rules-and-implications-in-mutual-fund", "tax_and_elss"),
    ("/en/direct-vs-regular-plan-mutual-fund", "direct_vs_regular"),
    ("/en/how-direct-plan-different-regular-plan", "direct_vs_regular"),
)


def parse_sitemap_locs(xml_text: str) -> List[str]:
    return [u.strip() for u in re.findall(r"<loc>(.*?)</loc>", xml_text or "", flags=re.I) if u.strip()]


def extract_mfsh_page_metadata(page_html: str) -> Tuple[str, str, str]:
    """Extract `(title, summary, summary_source)` verbatim from a Mutual Funds Sahi Hai HTML page.

    - `title`: from `<h1>` (preferred) or `<title>`.
    - `summary`: verbatim from `<meta name="description">`, `og:description`, or the first body `<p>`
      after stripping `<script>`/`<style>` tags. Never written or paraphrased by the agent.
    """
    no_script = re.sub(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", " ", page_html or "", flags=re.I | re.S)

    h1_m = re.search(r"<h1[^>]*>(.*?)</h1>", no_script, flags=re.I | re.S)
    title_m = re.search(r"<title[^>]*>(.*?)</title>", no_script, flags=re.I | re.S)
    title = ""
    if h1_m:
        title = _strip_tags(h1_m.group(1))
    if not title and title_m:
        title = _strip_tags(title_m.group(1))

    meta_desc = re.search(
        r'<meta\b[^>]*name=["\']description["\'][^>]*content=["\']([^"\']+)["\']'
        r'|<meta\b[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']description["\']',
        no_script,
        flags=re.I,
    )
    if meta_desc:
        val = html.unescape((meta_desc.group(1) or meta_desc.group(2) or "").strip())
        val = re.sub(r"\s+", " ", val)
        if val:
            return title, val, "meta_description"

    og_desc = re.search(
        r'<meta\b[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']'
        r'|<meta\b[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:description["\']',
        no_script,
        flags=re.I,
    )
    if og_desc:
        val = html.unescape((og_desc.group(1) or og_desc.group(2) or "").strip())
        val = re.sub(r"\s+", " ", val)
        if val:
            return title, val, "og_description"

    for p_raw in re.findall(r"<p[^>]*>(.*?)</p>", no_script, flags=re.I | re.S):
        clean_p = _strip_tags(p_raw)
        if (
            len(clean_p) >= 40
            and "cookie" not in clean_p.lower()
            and "mutual fund investments are subject to market risks" not in clean_p.lower()
        ):
            return title, clean_p, "first_paragraph"

    return title, "", "none"


def build_mfsh_index(
    session: requests.Session,
    *,
    sitemap_index_url: str = MFSH_SITEMAP_INDEX_URL,
    timeout_sec: float = 20.0,
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Discover MFSH URLs from the official sitemap, verify HTTP 200 on each, and extract verbatim summaries.

    Returns `(rows, missing_topics_notes)`.
    """
    resp_idx = session.get(sitemap_index_url, timeout=timeout_sec)
    if resp_idx.status_code != 200:
        raise RuntimeError(f"MFSH sitemap fetch failed: HTTP {resp_idx.status_code} on {sitemap_index_url}")

    locs = parse_sitemap_locs(resp_idx.text)
    discovered_from = sitemap_index_url
    # If sitemap.xml is a sitemapindex pointing to sitemap-0.xml, follow it
    if len(locs) <= 5 and any(l.endswith(".xml") for l in locs):
        sub_locs: List[str] = []
        for sub_url in locs:
            if sub_url.endswith(".xml"):
                r_sub = session.get(sub_url, timeout=timeout_sec)
                if r_sub.status_code == 200:
                    sub_locs.extend(parse_sitemap_locs(r_sub.text))
                    discovered_from = sub_url
        if sub_locs:
            locs = sub_locs

    loc_set = set(locs)
    rows: List[Dict[str, str]] = []
    missing_notes: List[str] = []

    # Check if any XIRR page exists anywhere in the sitemap
    xirr_urls = [u for u in locs if "xirr" in u.lower()]
    if not xirr_urls:
        missing_notes.append(
            "xirr: No standalone XIRR page exists in mutualfundssahihai.com's sitemap "
            "(return metrics covered on the site are CAGR / Annualised Return, Absolute Return, "
            "Trailing & Rolling Returns, and Total Return Index)."
        )

    for slug_suffix, topic in _MFSH_TOPIC_SLUGS:
        matching_urls = [u for u in locs if u.rstrip("/").endswith(slug_suffix) and u in loc_set]
        if not matching_urls:
            missing_notes.append(f"{topic}: slug {slug_suffix!r} not found in sitemap")
            continue
        url = matching_urls[0]
        try:
            page_resp = session.get(url, timeout=timeout_sec)
        except requests.RequestException as e:
            missing_notes.append(f"{topic}: {url} request failed ({e})")
            continue

        if page_resp.status_code != 200:
            missing_notes.append(f"{topic}: {url} returned HTTP {page_resp.status_code} (omitted)")
            continue

        title, summary, summary_source = extract_mfsh_page_metadata(page_resp.text)
        if not title or not summary:
            missing_notes.append(f"{topic}: {url} missing verbatim title or summary (omitted)")
            continue

        rows.append(
            {
                "topic": topic,
                "title": title,
                "url": url,
                "summary": summary,
                "summary_source": summary_source,
                "http_status": str(page_resp.status_code),
                "discovered_from": discovered_from,
            }
        )

    return rows, missing_notes


# ----------------------------------------------------------------------------- CSV Writer + Stage Driver


def _write_csv(path: Path, columns: Iterable[str], rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    cols = list(columns)
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in cols})
    tmp.replace(path)


def run_context(
    settings: Settings,
    *,
    context_dir: Optional[Path] = None,
    session: Optional[requests.Session] = None,
) -> int:
    """Run P5 Part 2 context data collection and write CSVs under `data/context/`."""
    out_dir = Path(context_dir) if context_dir else settings.paths.resolve("context_dir")
    raw_cache_dir = out_dir / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_cache_dir.mkdir(parents=True, exist_ok=True)

    sess = session or requests.Session()
    sess.headers.update({"User-Agent": DEFAULT_USER_AGENT})

    print(f"\n== Stage: context (writing to {out_dir}, isolated from corpus) ==")
    rc = 0
    stats_summary: Dict[str, Any] = {}

    # 1. AMFI Monthly SIP Data
    amfi_csv_path = out_dir / "amfi_sip_monthly.csv"
    amfi_html_cache = raw_cache_dir / "amfi_mutual_fund.html"
    try:
        print("\n-- 1. AMFI Monthly SIP Data --")
        resp = _get_amfi_url(sess, AMFI_SIP_URL, timeout=25)

        if resp.status_code == 200 and "<table" in resp.text.lower():
            amfi_html = resp.text
            atomic_write_text(amfi_html_cache, amfi_html)
        elif amfi_html_cache.exists():
            log.warning("Live AMFI fetch returned HTTP %s; using cached %s", resp.status_code, amfi_html_cache)
            amfi_html = amfi_html_cache.read_text(encoding="utf-8")
        else:
            raise RuntimeError(
                f"AMFI returned HTTP {resp.status_code}. Please download {AMFI_SIP_URL} manually to {amfi_html_cache}"
            )

        amfi_rows = parse_amfi_sip_html(amfi_html, source_url=AMFI_SIP_URL, max_months=24)
        _write_csv(amfi_csv_path, AMFI_CSV_COLUMNS, amfi_rows)
        monthly_cnt = sum(1 for r in amfi_rows if r["period_type"] == "monthly")
        annual_cnt = sum(1 for r in amfi_rows if r["period_type"] == "annual_summary")
        print(f"  Wrote {len(amfi_rows)} rows ({monthly_cnt} monthly + {annual_cnt} FY summaries) -> {amfi_csv_path}")
        stats_summary["amfi_sip_monthly"] = {
            "status": "ok",
            "rows": len(amfi_rows),
            "monthly_rows": monthly_cnt,
            "annual_summary_rows": annual_cnt,
            "path": str(amfi_csv_path),
        }
    except Exception as e:  # noqa: BLE001
        log.error("AMFI monthly SIP extraction failed: %s", e)
        stats_summary["amfi_sip_monthly"] = {"status": "failed", "error": str(e)}
        rc = 1

    # 2. AMFI-Crisil Factbook 2026 Demographics
    factbook_csv_path = out_dir / "factbook_demographics.csv"
    factbook_pdf_path = raw_cache_dir / "amfi_crisil_factbook_2026.pdf"
    try:
        print("\n-- 2. AMFI-Crisil Factbook 2026 Demographics --")
        if not factbook_pdf_path.exists() or factbook_pdf_path.stat().st_size < 100_000:
            print(f"  Downloading {FACTBOOK_2026_URL} -> {factbook_pdf_path} ...")
            pdf_resp = _get_amfi_url(sess, FACTBOOK_2026_URL, timeout=60)
            if pdf_resp.status_code != 200:
                raise RuntimeError(
                    f"Factbook download returned HTTP {pdf_resp.status_code}. "
                    f"Download {FACTBOOK_2026_URL} manually to {factbook_pdf_path}"
                )
            factbook_pdf_path.write_bytes(pdf_resp.content)

        fb_rows = extract_factbook_demographics(factbook_pdf_path, source_url=FACTBOOK_2026_URL)
        _write_csv(factbook_csv_path, FACTBOOK_CSV_COLUMNS, fb_rows)
        high_cnt = sum(1 for r in fb_rows if r["confidence"] == "high")
        low_rows = [r for r in fb_rows if r["confidence"] == "low"]
        print(
            f"  Wrote {len(fb_rows)} rows ({high_cnt} high-confidence, {len(low_rows)} low-confidence) -> {factbook_csv_path}"
        )
        if low_rows:
            print("  Low-confidence rows for manual check (chart labels rendered as vector graphics in PDF):")
            for lr in low_rows:
                print(
                    f"    - [PDF page {lr['pdf_page']}] {lr['dimension']} | {lr['segment']} | "
                    f"{lr['metric']} ({lr['period']}) = {lr['value']} {lr['unit']} ({lr['notes']})"
                )
        stats_summary["factbook_demographics"] = {
            "status": "ok",
            "rows": len(fb_rows),
            "high_confidence": high_cnt,
            "low_confidence": len(low_rows),
            "path": str(factbook_csv_path),
        }
    except Exception as e:  # noqa: BLE001
        log.error("Factbook demographics extraction failed: %s", e)
        stats_summary["factbook_demographics"] = {"status": "failed", "error": str(e)}
        rc = 1

    # 3. Mutual Funds Sahi Hai Official Index
    mfsh_csv_path = out_dir / "mfsh_index.csv"
    try:
        print("\n-- 3. Mutual Funds Sahi Hai Official Index --")
        mfsh_rows, mfsh_missing = build_mfsh_index(sess)
        _write_csv(mfsh_csv_path, MFSH_CSV_COLUMNS, mfsh_rows)
        print(f"  Wrote {len(mfsh_rows)} verified (HTTP 200) pages -> {mfsh_csv_path}")
        if mfsh_missing:
            print("  Topics/pages omitted (not present in sitemap or non-200):")
            for m in mfsh_missing:
                print(f"    - {m}")
        stats_summary["mfsh_index"] = {
            "status": "ok",
            "rows": len(mfsh_rows),
            "omitted_notes": mfsh_missing,
            "path": str(mfsh_csv_path),
        }
    except Exception as e:  # noqa: BLE001
        log.error("Mutual Funds Sahi Hai index build failed: %s", e)
        stats_summary["mfsh_index"] = {"status": "failed", "error": str(e)}
        rc = 1

    # Record context metadata in corpus_stats.json under a separate "context" key (never touches "collect")
    stats_path = settings.paths.resolve("corpus_stats")
    stats = read_json(stats_path, {})
    stats["context"] = {
        "updated_at": iso_z(datetime.now(timezone.utc)),
        "files": stats_summary,
    }
    atomic_write_text(stats_path, json.dumps(stats, indent=2, ensure_ascii=False))
    return rc
