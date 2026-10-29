"""Mocked unit tests for P5 Part 2: Context data (`collectors/context_data.py`).

Tests:
1. AMFI monthly SIP HTML table parser (`parse_amfi_sip_html` -> 24 monthly rows + FY summaries with `source_url`).
2. Factbook demographics extractor (`extract_factbook_demographics` -> `pdf_page`, `confidence` high/low, and
   verification against page text).
3. Mutual Funds Sahi Hai sitemap discovery (`build_mfsh_index` -> strictly sitemap-discovered URLs, HTTP 200
   requirement, and verbatim `<meta name="description">` / first paragraph extraction).
4. Isolation check: files under `data/context/` are never read or merged by `collectors.runner.collate()`.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List

import pytest

from collectors.context_data import (
    AMFI_CSV_COLUMNS,
    AMFI_SIP_URL,
    FACTBOOK_2026_URL,
    FACTBOOK_CSV_COLUMNS,
    MFSH_CSV_COLUMNS,
    _get_amfi_url,
    build_mfsh_index,
    extract_factbook_demographics,
    extract_mfsh_page_metadata,
    parse_amfi_sip_html,
)
from collectors.runner import collate


SAMPLE_AMFI_HTML = """
<html>
<head><style>.css-1{color:red;}</style></head>
<body>
  <table>
    <tr><th>Month</th><th>SIP Contribution ₹ crore</th></tr>
    <tr><th>FY 2026-27</th><th>FY 2025-26</th><th>FY 2024-25</th></tr>
    <tr><td>Total during FY</td><td>1,58,108</td><td>3,49,589</td><td>2,89,352</td></tr>
    <tr><td>September</td><td>32,490</td><td>29,361</td><td>24,509</td></tr>
    <tr><td>August</td><td>32,297</td><td>28,265</td><td>23,547</td></tr>
    <tr><td>March</td><td></td><td>32,087</td><td>25,926</td></tr>
  </table>
  <table>
    <tr>
      <th>Month</th>
      <th>Total No. of outstanding SIP Accounts</th>
      <th>No. of New SIPs registered</th>
      <th>No. of SIPs discontinued*/ tenure completed</th>
      <th>No. of Contributing SIP accounts</th>
      <th>SIP Contribution ₹ crore</th>
      <th>SIP AUM ₹ crore</th>
    </tr>
    <tr>
      <td>Apr 26- Sep 26</td><td>1,088.9</td><td>351.48</td><td>307.43</td><td>1,006.47</td><td>1,90,598</td><td>17,86,095</td>
    </tr>
    <tr>
      <td>Sep-26</td><td>1,088.9</td><td>63.26</td><td>49.68</td><td>1,006.47</td><td>32,490</td><td>17,86,095</td>
    </tr>
    <tr>
      <td>Aug-26</td><td>1,075.32</td><td>66.4</td><td>53.82</td><td>1,001.79</td><td>32,297</td><td>18,61,652</td>
    </tr>
    <tr>
      <td>Apr 25 – Mar 26</td><td>1,044.84</td><td>719.22</td><td>679.75</td><td>971.86</td><td>3,49,589</td><td>15,10,943</td>
    </tr>
  </table>
</body>
</html>
"""


def test_parse_amfi_sip_html_combines_monthly_and_fy_summaries():
    """1. AMFI parser extracts monthly rows (`YYYY-MM`) and FY summary rows with normalised numbers and `source_url`."""
    rows = parse_amfi_sip_html(SAMPLE_AMFI_HTML, source_url=AMFI_SIP_URL, max_months=24)
    assert len(rows) > 0
    for r in rows:
        assert tuple(r.keys()) == AMFI_CSV_COLUMNS
        assert r["source_url"] == AMFI_SIP_URL

    by_month = {r["month"]: r for r in rows}
    # Sep-26 has full breakdown from Table 1
    sep26 = by_month["2026-09"]
    assert sep26["period_type"] == "monthly"
    assert sep26["outstanding_sip_accounts_lakhs"] == "1088.9"
    assert sep26["new_sips_registered_lakhs"] == "63.26"
    assert sep26["sips_discontinued_or_matured_lakhs"] == "49.68"
    assert sep26["contributing_sip_accounts_lakhs"] == "1006.47"
    assert sep26["sip_contribution_inr_crore"] == "32490"
    assert sep26["sip_aum_inr_crore"] == "1786095"

    # March of FY 2025-26 -> calendar month 2026-03 from Table 0
    mar26 = by_month["2026-03"]
    assert mar26["period_type"] == "monthly"
    assert mar26["sip_contribution_inr_crore"] == "32087"

    # Annual summary row from Table 1
    fy2526 = by_month["Apr 25 - Mar 26"]
    assert fy2526["period_type"] == "annual_summary"
    assert fy2526["new_sips_registered_lakhs"] == "719.22"
    assert fy2526["sips_discontinued_or_matured_lakhs"] == "679.75"


def test_factbook_demographics_extractor_includes_page_numbers_and_confidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """2. Factbook demographics extractor outputs required schema (`pdf_page`, `confidence` in `{'high', 'low'}`)
    and marks rows `high` when verified in selectable page text and `low` when unverified/chart-only."""

    class FakePage:
        def __init__(self, text: str):
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class FakePdfReader:
        def __init__(self, path: str):
            self.pages = [FakePage("") for _ in range(76)]
            self.pages[21] = FakePage("The number of unique mutual fund investors increased from 2.3 crore in March 2021 to 6.1 crore in March 2026.")
            self.pages[28] = FakePage("B30 AUM rising from Rs 5.2 lakh crore to Rs 13.9 lakh crore, T30 assets from Rs 26.2 lakh crore to Rs 59.8 lakh crore.")
            self.pages[39] = FakePage("below 18 years from 37.3% to 46.0% and 35 to 58 years from 29.8% to 40.6%. 18 to 34 years to 32.7%. above 58 years from 11.4% to 20.6%.")
            self.pages[40] = FakePage("18-34 age group increased from 22.6% in March 2021 to 22.7% in March 2026, regular decreased from 14.4% to 11.9%.")
            self.pages[44] = FakePage("Women 32.6% 61.3% 34.5% Men Minor 1.2%")
            self.pages[45] = FakePage("women represented 26.4% of the MF investor base, approximately 1.61 crore women investors.")
            self.pages[71] = FakePage("Maharashtra 4.1% 26.0% 27.7% 35.3% 6.9% Karnataka 3.2% 27.3% 33.2% 29.1% 7.2%")

    import collectors.context_data as cd

    monkeypatch.setattr(cd, "PdfReader", FakePdfReader)
    dummy_pdf = tmp_path / "dummy.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4 dummy")

    rows = extract_factbook_demographics(dummy_pdf, source_url=FACTBOOK_2026_URL)
    assert len(rows) > 20
    for r in rows:
        assert tuple(r.keys()) == FACTBOOK_CSV_COLUMNS
        assert r["confidence"] in ("high", "low")
        assert int(r["pdf_page"]) >= 1
        assert r["source_url"] == FACTBOOK_2026_URL

    high_rows = [r for r in rows if r["confidence"] == "high"]
    low_rows = [r for r in rows if r["confidence"] == "low"]
    assert len(high_rows) > 10
    assert len(low_rows) >= 4
    # Verify Annexure 3 state age allocation parsed from page 72
    maha_u25 = [r for r in rows if r["segment"] == "Maharashtra — Below 25"]
    assert len(maha_u25) == 1
    assert maha_u25[0]["value"] == "4.1"
    assert maha_u25[0]["pdf_page"] == "72"
    assert maha_u25[0]["confidence"] == "high"


def test_mfsh_sitemap_discovery_requires_http_200_and_extracts_verbatim_summary():
    """3. MFSH index builder:
    - Discovers URLs strictly from the sitemap XML.
    - Omits any URL that returns non-200 (e.g., HTTP 404).
    - Extracts `summary` verbatim from `<meta name="description">` or first `<p>` (stripping `<script>` JSON-LD).
    - Reports missing topics (e.g., XIRR when not in sitemap)."""

    sitemap_index_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex><sitemap><loc>https://www.mutualfundssahihai.com/sitemap-0.xml</loc></sitemap></sitemapindex>
    """
    sitemap_0_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset>
      <url><loc>https://www.mutualfundssahihai.com/en/what-is-sip</loc></url>
      <url><loc>https://www.mutualfundssahihai.com/en/how-do-i-startstop-sip-what-happens-if-i-miss-instalment</loc></url>
      <url><loc>https://www.mutualfundssahihai.com/en/elss-fund-tax-saving-mutual-fund</loc></url>
    </urlset>
    """
    page_what_is_sip = """
    <html>
      <head>
        <title>What is SIP Investment?</title>
        <meta name="description" content="What is Systematic Investment Plan? How SIP works, benefits &amp; more." />
      </head>
      <body><h1>What is Systematic Investment Plan (SIP)?</h1></body>
    </html>
    """
    page_start_stop = """
    <html>
      <head><title>How to Start or Stop SIP</title></head>
      <body>
        <h1>How do I start/stop a SIP? What happens if I miss an instalment?</h1>
        <p><script type="application/ld+json">{"@context":"https://schema.org"}</script>
           Before you make any Mutual Fund investment, you need to complete your KYC and bank mandate.</p>
      </body>
    </html>
    """

    class FakeMFSHSession:
        def get(self, url: str, timeout: float = 20.0):
            class R:
                def __init__(self, code: int, text: str):
                    self.status_code = code
                    self.text = text

            if url.endswith("/sitemap.xml"):
                return R(200, sitemap_index_xml)
            if url.endswith("/sitemap-0.xml"):
                return R(200, sitemap_0_xml)
            if url.endswith("/en/what-is-sip"):
                return R(200, page_what_is_sip)
            if url.endswith("/en/how-do-i-startstop-sip-what-happens-if-i-miss-instalment"):
                return R(200, page_start_stop)
            if url.endswith("/en/elss-fund-tax-saving-mutual-fund"):
                return R(404, "Not Found")
            return R(404, "Not Found")

    rows, missing = build_mfsh_index(FakeMFSHSession())  # type: ignore[arg-type]
    assert len(rows) == 2
    for r in rows:
        assert tuple(r.keys()) == MFSH_CSV_COLUMNS
        assert r["http_status"] == "200"
        assert r["discovered_from"] == "https://www.mutualfundssahihai.com/sitemap-0.xml"

    assert rows[0]["url"] == "https://www.mutualfundssahihai.com/en/what-is-sip"
    assert rows[0]["summary"] == "What is Systematic Investment Plan? How SIP works, benefits & more."
    assert rows[0]["summary_source"] == "meta_description"

    assert rows[1]["url"] == "https://www.mutualfundssahihai.com/en/how-do-i-startstop-sip-what-happens-if-i-miss-instalment"
    assert rows[1]["summary"] == "Before you make any Mutual Fund investment, you need to complete your KYC and bank mandate."
    assert rows[1]["summary_source"] == "first_paragraph"
    assert "schema.org" not in rows[1]["summary"]

    # ELSS returned 404 and XIRR was absent from sitemap -> both reported in missing
    assert any("HTTP 404" in m for m in missing)
    assert any("xirr" in m.lower() for m in missing)


def test_context_files_never_enter_collate_corpus(tmp_path: Path):
    """4. Context isolation: `collate(raw_dir)` only merges `data/raw/*.jsonl` and never touches `data/context/*.csv`."""
    raw_dir = tmp_path / "data" / "raw"
    context_dir = tmp_path / "data" / "context"
    raw_dir.mkdir(parents=True)
    context_dir.mkdir(parents=True)

    (raw_dir / "tradingqna.jsonl").write_text(
        '{"id": "tradingqna:t_1", "source": "tradingqna", "text": "Stopped my SIP"}\n',
        encoding="utf-8",
    )
    (context_dir / "amfi_sip_monthly.csv").write_text("month,sip_contribution_inr_crore\n2026-09,32490\n", encoding="utf-8")
    (context_dir / "factbook_demographics.csv").write_text("dimension,value\nage_band,32.7\n", encoding="utf-8")
    (context_dir / "mfsh_index.csv").write_text("topic,url\nsip_basics,https://www.mutualfundssahihai.com/en/what-is-sip\n", encoding="utf-8")

    total = collate(raw_dir)
    assert total == 1
    collated_text = (raw_dir / "raw_posts.jsonl").read_text(encoding="utf-8")
    assert "tradingqna:t_1" in collated_text
    assert "32490" not in collated_text
    assert "mutualfundssahihai" not in collated_text


def test_get_amfi_url_scopes_verify_false_strictly_to_amfiindia_and_logs_warning(caplog: pytest.LogCaptureFixture):
    """5. `_get_amfi_url` tries `verify=True` first; on `SSLError`, retries with `verify=False` and logs a WARNING
    ONLY if the hostname is `amfiindia.com` (raises `SSLError` for any other host)."""
    import logging
    import requests as req

    calls = []

    class FakeSession:
        def get(self, url: str, timeout: float = 25.0, verify: bool = True):
            calls.append((url, verify))
            if verify:
                raise req.exceptions.SSLError("certificate verify failed: unable to get local issuer certificate")

            class Resp:
                status_code = 200
                text = "ok"

            return Resp()

    sess = FakeSession()
    with caplog.at_level(logging.WARNING, logger="groww.context"):
        resp = _get_amfi_url(sess, "https://www.amfiindia.com/mutual-fund")  # type: ignore[arg-type]
    assert resp.status_code == 200
    assert calls == [
        ("https://www.amfiindia.com/mutual-fund", True),
        ("https://www.amfiindia.com/mutual-fund", False),
    ]
    assert any("retrying https://www.amfiindia.com/mutual-fund with verify=False" in r.message for r in caplog.records)

    # Non-AMFI host must NEVER fall back to verify=False
    with pytest.raises(req.exceptions.SSLError):
        _get_amfi_url(sess, "https://generativelanguage.googleapis.com/v1beta/models")  # type: ignore[arg-type]

