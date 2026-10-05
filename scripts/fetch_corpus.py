"""Discover and download the document collection (federal_reserve/ + metadata.csv).

The original set is 93 Board PDFs. ``--discover`` crawls federalreserve.gov indexes (press releases,
speeches, testimony, FEDS Notes, SR letters, SLOOS, the FOMC calendar) plus a curated list of data
files from the Board and two Reserve Banks, and appends one metadata row per new document:

    html  FOMC statements, Summaries of Economic Projections, speeches, testimony, FEDS Notes,
          SR letters, Senior Loan Officer surveys, press releases
    pdf   FOMC press conference transcripts, the SCF Bulletin, the SHED report, SPF documentation
    docx  Survey of Consumer Finances instrument and table guide
    xlsx  SCF historical tables, stress-test market shocks, NY Fed household debt and consumer
          expectations, Philadelphia Fed SPF median forecasts
    csv   stress-test results for every bank since 2013, scenario variables, nine-quarter paths

Without ``--discover`` it downloads every file listed in metadata.csv that is missing locally and checks
its sha256, so a fresh clone can rebuild the collection.

    python scripts/fetch_corpus.py --discover     # find new documents, update metadata.csv, download
    python scripts/fetch_corpus.py                # download whatever metadata.csv lists but is missing
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "federal_reserve"
META = DOCS / "metadata.csv"
FED = "https://www.federalreserve.gov"
NYFED = "https://www.newyorkfed.org"
PHIL = "https://www.philadelphiafed.org"
UA = "fedrag-corpus/0.2 (research collection builder; python-httpx)"
COLUMNS = ["filename", "id", "doc_type", "title", "date", "pages", "size_mb", "has_text_layer", "sha256", "url",
           "retrieved", "format", "publisher", "speaker", "description"]
BOARD = "Board of Governors of the Federal Reserve System"

# Data files and reports that have no crawlable index: (filename, doc_type, title, date, url, publisher)
STATIC = [
    # Survey of Consumer Finances 2022: Bulletin article, historical tables, interview program, table guide
    ("fed_scf_bulletin_2023-10.pdf", "household_survey",
     "Changes in U.S. Family Finances from 2019 to 2022: Evidence from the Survey of Consumer Finances "
     "(Federal Reserve Bulletin)", "2023-10", f"{FED}/publications/files/scf23.pdf", BOARD),
    ("fed_scf_2022_historical_tables_real.xlsx", "household_survey",
     "Survey of Consumer Finances 1989-2022: Bulletin tables, historical, in 2022 dollars (Excel)", "2023-10",
     f"{FED}/econres/files/scf2022_tables_public_real_historical.xlsx", BOARD),
    ("fed_scf_2022_interview_instrument.docx", "household_survey",
     "2022 Survey of Consumer Finances: simulation of the MR interview program (questionnaire, Word)", "2023-10",
     f"{FED}/econres/files/SCF2022.docx", BOARD),
    ("fed_scf_bulletin_tables_sda_guide.docx", "household_survey",
     "Reproducing and Extending Published SCF Federal Reserve Bulletin Tables Using SDA (Word)", "2023-10",
     f"{FED}/econres/files/Creating%20the%20SCF%20Bulletin%20Tables%20in%20SDA.docx", BOARD),
    # Survey of Household Economics and Decisionmaking
    ("fed_shed_report_2026-05.pdf", "household_survey",
     "Economic Well-Being of U.S. Households in 2025 (SHED report)", "2026-05-13",
     f"{FED}/publications/files/2025-report-economic-well-being-us-households-202605.pdf", BOARD),
    # Stress tests: bank-level results for every exercise since 2013, scenarios, paths, market shocks
    ("fed_dfast_results_2013-2026.csv", "stress_test",
     "Stress test public results by bank and scenario, DFAST 2013-2026 (CSV)", "2026-06-24",
     f"{FED}/supervisionreg/files/public_results_DFAST_2026.csv", BOARD),
    ("fed_stress_test_paths_2026.csv", "stress_test",
     "2026 stress test: detailed nine-quarter paths of projected capital, losses and revenues (CSV)", "2026-06-24",
     f"{FED}/supervisionreg/files/2026_Detailed_Nine_Quarter_Paths.csv", BOARD),
    ("fed_stress_test_paths_2025.csv", "stress_test",
     "2025 stress test: detailed nine-quarter paths of projected capital, losses and revenues (CSV)", "2025-06-27",
     f"{FED}/supervisionreg/files/2025_Detailed_Nine_Quarter_Paths.csv", BOARD),
    ("fed_stress_test_market_shocks_2026.xlsx", "stress_test",
     "2026 stress test: global market shock, severely adverse scenario, simplified shocks (Excel)", "2026-02-04",
     f"{FED}/supervisionreg/files/2026_Final_Severely_Adverse_Market_Shock_simplified-shocks.xlsx", BOARD),
    ("fed_large_bank_capital_requirements_2026.pdf", "stress_test",
     "Large Bank Capital Requirements, June 2026 (preliminary stress capital buffers)", "2026-06-24",
     f"{FED}/publications/files/large-bank-capital-requirements-20260624.pdf", BOARD),
    # Reserve Banks
    ("nyfed_household_debt_credit_2026q2.xlsx", "household_survey",
     "Quarterly Report on Household Debt and Credit, 2026:Q2, underlying data (New York Fed, Excel)",
     "2026-08-11", f"{NYFED}/medialibrary/interactives/householdcredit/data/xls/HHD_C_Report_2026Q2.xlsx",
     "Federal Reserve Bank of New York"),
    ("nyfed_consumer_expectations_2026-08.xlsx", "household_survey",
     "Survey of Consumer Expectations: chart data through August 2026 (New York Fed, Excel)", "2026-09-08",
     f"{NYFED}/medialibrary/interactives/sce/sce/downloads/data/frbny-sce-data.xlsx",
     "Federal Reserve Bank of New York"),
    ("philfed_spf_median_level_2026q3.xlsx", "forecaster_survey",
     "Survey of Professional Forecasters: median forecasts for the levels of variables, 1968:Q4-2026:Q3 "
     "(Philadelphia Fed, Excel)", "2026-08-14",
     f"{PHIL}/-/media/FRBP/Assets/Surveys-And-Data/survey-of-professional-forecasters/historical-data/"
     "medianLevel.xlsx", "Federal Reserve Bank of Philadelphia"),
    ("philfed_spf_documentation_2026-07.pdf", "forecaster_survey",
     "Survey of Professional Forecasters: Documentation (variables, data files, codes)", "2026-07-28",
     f"{PHIL}/-/media/frbp/assets/surveys-and-data/survey-of-professional-forecasters/spf-documentation.pdf",
     "Federal Reserve Bank of Philadelphia"),
]
# Data dictionary for files whose column names or sheet codes do not explain themselves; the table cards
# that retrieval and the agents see include it.
DESCRIPTIONS = {
    "fed_dfast_results_2013-2026": (
        "One row per bank, stress test exercise and scenario: exercise_name (DFAST 2013 ... DFAST 2021, December "
        "2020 Stress Test, 2022 Stress Test ... 2026 Stress Test), disclosure_legal_name (bank holding company), "
        "scenario_name (Supervisory Severely Adverse, Supervisory Adverse, Supervisory Alternative Severe). "
        "Capital ratios in percent for tier 1 common, common equity tier 1 (CET1), tier 1, total capital, tier 1 "
        "leverage and supplementary leverage: *_actual_rat = starting (actual) ratio, *_end_rat = ratio at the end "
        "of the projection horizon, *_min_rat = projected minimum ratio. Dollar amounts (*_amt) in billions: "
        "risk-weighted assets, loan losses by loan type (first-lien mortgages, junior liens and HELOCs, commercial "
        "and industrial, commercial real estate, credit cards, other consumer, other loans) with loss rates in "
        "percent (*_rate), pre-provision net revenue, net interest income, noninterest income and expense, "
        "provisions, securities losses, trading and counterparty losses, pre-tax net income, AOCI. Rows whose "
        "disclosure_legal_name reads 'N participating banks' (or '... bank holding companies', '... firms') are "
        "the official aggregate for all banks in that exercise: use them for aggregate starting, ending and "
        "minimum ratios rather than averaging the banks."),
    "fed_stress_test_paths_2026": (
        "Aggregate of all banks in the 2026 stress test, severely adverse scenario: one row per item (capital, "
        "risk-weighted assets, capital ratios in percent, losses, revenues, net income, loan losses; dollars in "
        "billions), columns pq1 ... pq9 = projection quarters 1 to 9 (2026:Q1 to 2028:Q1). pq1 is already a stressed "
        "projection quarter, not the starting point: the actual starting ratios are in the aggregate rows of the "
        "DFAST results file (common_equity_tier1_actual_rat) and in the results report."),
    "fed_stress_test_paths_2025": (
        "Aggregate of all banks in the 2025 stress test, severely adverse scenario: one row per item (capital, "
        "risk-weighted assets, capital ratios in percent, losses, revenues, net income, loan losses; dollars in "
        "billions), columns pq1 ... pq9 = projection quarters 1 to 9 (2025:Q1 to 2027:Q1). pq1 is already a stressed "
        "projection quarter, not the starting point: the actual starting ratios are in the aggregate rows of the "
        "DFAST results file (common_equity_tier1_actual_rat) and in the results report."),
    "fed_stress_test_market_shocks_2026": (
        "Instantaneous shocks to trading and fair-value positions in the 2026 global market shock (severely "
        "adverse, simplified shocks): one sheet per asset class (equities by geography, dividends, FX spot and "
        "volatility, rates, credit spreads, securitized products, commodities, private equity)."),
    "fed_scf_2022_historical_tables_real": (
        "Survey of Consumer Finances Bulletin tables for every survey 1989-2022, in thousands of 2022 dollars "
        "unless noted: table 1 income and saving, table 2 income sources, table 3 reasons for saving, table 4 net "
        "worth, tables 5-6 financial assets (participation, medians, means), tables 7-9 nonfinancial assets, "
        "tables 10-13 debt (holdings, payments, leverage, debt-payment ratios), tables 14-17 credit and "
        "borrowing; rows are family characteristics (income and net worth percentiles, age, education, race, "
        "family structure, housing, work status, region)."),
    "nyfed_household_debt_credit_2026q2": (
        "New York Fed Consumer Credit Panel/Equifax data behind the Household Debt and Credit report, quarterly "
        "from 2003 (sheets named after report pages): balances by loan type in trillions of dollars, number of "
        "accounts, originations by credit score, new foreclosures and bankruptcies, delinquency rates and "
        "transitions into delinquency by loan type, age and region."),
    "nyfed_consumer_expectations_2026-08": (
        "New York Fed Survey of Consumer Expectations, monthly from June 2013 to August 2026: median one- and "
        "three-year-ahead inflation expectations and uncertainty, home price, earnings, income and spending "
        "expectations, job loss/finding probabilities, unemployment, interest rate, stock price and delinquency "
        "expectations; '... Demo' sheets break the series down by age, education, income and region."),
    "philfed_spf_median_level_2026q3": (
        "Philadelphia Fed Survey of Professional Forecasters median forecasts, one sheet per variable code (NGDP "
        "nominal GDP, PGDP GDP price index, RGDP real GDP, UNEMP unemployment rate, CPI/CORECPI/PCE/COREPCE "
        "inflation, TBILL/TBOND/BOND/BAABOND rates, HOUSING starts, EMP payrolls, INDPROD, CPROF profits, CPI10/"
        "PCE10 ten-year inflation, ...). Columns year/quarter = survey date; suffix 1 = previous quarter "
        "(historical), 2 = current quarter, 3-6 = next four quarters, A = current year, B = next year (C, D = "
        "later years where available). Definitions in the SPF documentation."),
}
SCENARIO_FILES = {  # stress-test scenario variables, one CSV per scenario and region
    2026: [("Final_Historic_Domestic", "historical data, domestic variables"),
           ("Final_Historic_International", "historical data, international variables"),
           ("Final_Supervisory_Baseline_Domestic", "supervisory baseline scenario, domestic variables"),
           ("Final_Supervisory_Baseline_International", "supervisory baseline scenario, international variables"),
           ("Final_Supervisory_Severely_Adverse_Domestic", "severely adverse scenario, domestic variables"),
           ("Final_Supervisory_Severely_Adverse_International", "severely adverse scenario, international variables")],
    2025: [("Table_2A_Supervisory_Baseline_Domestic", "supervisory baseline scenario, domestic variables"),
           ("Table_2B_Supervisory_Baseline_International", "supervisory baseline scenario, international variables"),
           ("Table_3A_Supervisory_Severely_Adverse_Domestic", "severely adverse scenario, domestic variables"),
           ("Table_3B_Supervisory_Severely_Adverse_International", "severely adverse scenario, international variables")],
    2024: [("Table_3A_Supervisory_Baseline_Domestic", "supervisory baseline scenario, domestic variables"),
           ("Table_3B_Supervisory_Baseline_International", "supervisory baseline scenario, international variables"),
           ("Table_4A_Supervisory_Severely_Adverse_Domestic", "severely adverse scenario, domestic variables"),
           ("Table_4B_Supervisory_Severely_Adverse_International", "severely adverse scenario, international variables")],
}
SCENARIO_DATES = {2026: "2026-02-04", 2025: "2025-02-05", 2024: "2024-02-15"}


class Fetcher:
    def __init__(self) -> None:
        self.http = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": UA})

    def get(self, url: str) -> httpx.Response:
        for attempt in range(4):
            try:
                r = self.http.get(url)
                if r.status_code in (403, 406):  # a few servers reject non-browser agents
                    r = self.http.get(url, headers={"User-Agent": "Mozilla/5.0 (fedrag corpus builder)"})
                if r.status_code < 500:
                    time.sleep(0.25)  # be polite
                    return r
            except httpx.TransportError:
                pass
            time.sleep(2 + 3 * attempt)
        raise RuntimeError(f"failed to fetch {url}")

    def text(self, url: str) -> str:
        r = self.get(url)
        r.raise_for_status()
        return r.text

    def json(self, url: str):
        return json.loads(self.get(url).content.decode("utf-8-sig"))


def _mdy(s: str | None) -> str:
    """'10/1/2026 1:30:00 PM' -> '2026-10-01' ('' when missing)."""
    try:
        return dt.datetime.strptime(s.split()[0], "%m/%d/%Y").strftime("%Y-%m-%d")
    except (AttributeError, IndexError, ValueError):
        return ""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _row(filename: str, doc_type: str, title: str, date: str, url: str, publisher: str = BOARD,
         speaker: str = "") -> dict:
    return {"filename": filename, "id": Path(filename).stem, "doc_type": doc_type, "title": _clean(title),
            "date": date, "url": url, "format": Path(filename).suffix.lstrip(".").lower(), "publisher": publisher,
            "speaker": speaker}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover(f: Fetcher) -> list[dict]:
    rows: list[dict] = []
    press = f.json(f"{FED}/json/ne-press.json")

    # FOMC statements (2023 on) and the Statement on Longer-Run Goals and Monetary Policy Strategy
    for p in press:
        link, title, date = p.get("l") or "", p.get("t") or "", _mdy(p.get("d"))
        if p.get("pt") != "Monetary Policy" or date < "2023-01-01" or not link:
            continue
        if title.strip() == "Federal Reserve issues FOMC statement":
            d = dt.date.fromisoformat(date).strftime("%B %-d, %Y")
            rows.append(_row(f"fed_fomc_statement_{date}.html", "fomc_statement", f"FOMC statement, {d}", date,
                             FED + link))
        elif "Longer-Run Goals" in title:
            rows.append(_row(f"fed_fomc_longer_run_goals_{date}.html", "fomc_statement", _clean(title), date,
                             FED + link))

    # Summaries of Economic Projections (accessible HTML version with all tables) and 2026 press conferences
    cal = f.text(f"{FED}/monetarypolicy/fomccalendars.htm")
    for d8 in sorted(set(re.findall(r"fomcprojtabl(\d{8})\.htm", cal))):
        date = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
        if date >= "2023-01-01":
            label = dt.date.fromisoformat(date).strftime("%B %-d, %Y")
            rows.append(_row(f"fed_sep_{date}.html", "economic_projections",
                             f"Summary of Economic Projections, FOMC meeting ending {label}", date,
                             f"{FED}/monetarypolicy/fomcprojtabl{d8}.htm"))
    for d8 in sorted(set(re.findall(r"monetary(2026\d{4})a\.htm", cal))):
        date = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
        label = dt.date.fromisoformat(date).strftime("%B %-d, %Y")
        rows.append(_row(f"fed_fomc_presconf_{date}.pdf", "press_conference",
                         f"Transcript of the Chair's FOMC press conference, {label}", date,
                         f"{FED}/mediacenter/files/FOMCpresconf{d8}.pdf"))

    # Other 2026 press releases: regulation, announcements, discount-rate minutes (not FOMC pointers)
    skip = re.compile(r"issues FOMC statement|Minutes of the Federal Open Market Committee|release economic "
                      r"projections|Longer-Run Goals", re.I)
    for p in press:
        link, title, date = p.get("l") or "", p.get("t") or "", _mdy(p.get("d"))
        if not link or not date.startswith("2026") or skip.search(title):
            continue
        if p.get("pt") in ("Banking and Consumer Regulatory Policy", "Other Announcements", "Monetary Policy"):
            slug = Path(link).stem
            rows.append(_row(f"fed_press_release_{slug}.html", "press_release", _clean(title), date, FED + link))

    # Speeches (2026) and testimony (2025-2026)
    for kind, url, year_from in (("speech", "/json/ne-speeches.json", "2026"),
                                 ("testimony", "/json/ne-testimony.json", "2025")):
        for s in f.json(FED + url):
            date, link = _mdy(s.get("d")), s.get("l") or ""
            if date < f"{year_from}-01-01" or not link.endswith(".htm"):
                continue
            speaker = _clean(s.get("s", ""))
            title = f"{'Speech' if kind == 'speech' else 'Testimony'} by {speaker}: {_clean(s['t'])}"
            if s.get("lo"):
                title += f" ({_clean(s['lo'])})"
            rows.append(_row(f"fed_{kind}_{Path(link).stem}.html", kind, title, date, FED + link, speaker=speaker))

    # FEDS Notes (2026)
    idx = f.text(f"{FED}/econres/notes/feds-notes/2026-index.htm")
    seen = set()
    for link, d8, title in re.findall(r'href="(/econres/notes/feds-notes/[^"]+-(2026\d{4})\.html?)"[^>]*>\s*'
                                      r'([^<]+?)\s*<', idx):
        if link in seen:
            continue
        seen.add(link)
        date = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
        slug = re.sub(r"-\d{8}$", "", Path(link).stem)[:48].rstrip("-")
        rows.append(_row(f"fed_feds_note_{date}_{slug}.html", "feds_note", f"FEDS Notes: {_clean(title)}", date,
                         FED + link))

    # SR letters (2025-2026); the date is in the letter itself
    for year in (2025, 2026):
        page = f.text(f"{FED}/supervisionreg/srletters/{year}.htm")
        for link, num, rest in re.findall(r'<a href="(/supervisionreg/srletters/SR\d+\.htm)"[^>]*>\s*(SR [\d-]+)\s*'
                                          r'</a>(.{0,600}?)</(?:tr|p|li)>', page, re.S):
            subject = _clean(re.sub(r"<[^>]+>", " ", rest))
            body = f.text(FED + link)
            m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)"
                          r"\s+\d{1,2},\s+(20\d\d)", re.sub(r"<[^>]+>", " ", body))
            date = dt.datetime.strptime(m.group(0), "%B %d, %Y").strftime("%Y-%m-%d") if m else f"{year}-01-01"
            rows.append(_row(f"fed_sr_letter_{num.split()[1]}.html", "supervisory_letter", f"{num}: {subject}", date,
                             FED + link))

    # Senior Loan Officer Opinion Surveys (2024-2026)
    sloos = f.text(f"{FED}/data/sloos.htm")
    for ym in sorted(set(re.findall(r"/data/sloos/sloos-(\d{6})\.htm", sloos))):
        if ym >= "202401":
            month = dt.date(int(ym[:4]), int(ym[4:]), 1).strftime("%B %Y")
            rows.append(_row(f"fed_sloos_{ym[:4]}-{ym[4:]}.html", "loan_officer_survey",
                             f"The {month} Senior Loan Officer Opinion Survey on Bank Lending Practices",
                             f"{ym[:4]}-{ym[4:]}", f"{FED}/data/sloos/sloos-{ym}.htm"))

    # Curated data files and reports
    for fn, doc_type, title, date, url, publisher in STATIC:
        rows.append(_row(fn, doc_type, title, date, url, publisher))
    for year, files in SCENARIO_FILES.items():
        for stem, label in files:
            region = "domestic" if stem.endswith("Domestic") else "international"
            kind = re.sub(r"^(Final_|Table_\d[AB]_)", "", stem).replace("_Domestic", "").replace("_International", "")
            fn = f"fed_stress_test_scenario_{year}_{kind.lower()}_{region}.csv"
            rows.append(_row(fn, "stress_test", f"{year} stress test scenarios: {label} (CSV)", SCENARIO_DATES[year],
                             f"{FED}/supervisionreg/files/{year}-{stem}.csv" if year != 2026 else
                             f"{FED}/supervisionreg/files/{year}_{stem}.csv"))
    return rows


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def _pdf_info(path: Path) -> tuple[str, str]:
    import pymupdf

    doc = pymupdf.open(path)
    has_text = any(page.get_text().strip() for page in doc)
    return str(len(doc)), str(has_text)


def download(f: Fetcher, row: dict, force: bool = False) -> bool:
    path = DOCS / row["filename"]
    if path.exists() and not force:
        if row.get("sha256") and hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            print(f"  WARNING: {row['filename']} differs from the recorded sha256 (source updated?)")
        return False
    r = f.get(row["url"])
    if r.status_code != 200 or not r.content:
        print(f"  FAILED {r.status_code} {row['url']}")
        return False
    path.write_bytes(r.content)
    row["sha256"] = hashlib.sha256(r.content).hexdigest()
    row["size_mb"] = f"{len(r.content) / 1e6:.2f}"
    row["retrieved"] = dt.date.today().isoformat()
    if row.get("format", "pdf") == "pdf":
        row["pages"], row["has_text_layer"] = _pdf_info(path)
    return True


def read_meta() -> list[dict]:
    with open(META, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:  # rows written before the format/publisher columns existed are Board PDFs
        r.setdefault("format", "pdf")
        r["format"] = r["format"] or "pdf"
        r["publisher"] = r.get("publisher") or BOARD
        r.setdefault("speaker", "")
        r["description"] = DESCRIPTIONS.get(r["id"], r.get("description") or "")
        if not r["description"] and r["id"].startswith("fed_stress_test_scenario_"):
            r["description"] = ("Scenario variables by quarter (scenario_name, date such as '2026 Q1'): real and "
                                "nominal GDP growth, disposable income growth, unemployment rate, CPI inflation, "
                                "Treasury yields, BBB and mortgage rates, prime rate, stock market index, house and "
                                "commercial real estate price indexes, market volatility index (domestic files); "
                                "GDP growth, inflation and exchange rates for the euro area, developing Asia, Japan "
                                "and the UK (international files). Growth rates are annualized percent changes.")
    return rows


def write_meta(rows: list[dict]) -> None:
    with open(META, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLUMNS})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--discover", action="store_true", help="crawl the indexes and add new documents")
    ap.add_argument("--dry-run", action="store_true", help="with --discover: list what would be added")
    args = ap.parse_args()

    f = Fetcher()
    rows = read_meta()
    known = {r["id"] for r in rows}
    new_ids: set[str] = set()
    if args.discover:
        new = [r for r in discover(f) if r["id"] not in known]
        uniq: dict[str, dict] = {}
        for r in new:
            uniq.setdefault(r["id"], r)
        new = list(uniq.values())
        by_type: dict[str, int] = {}
        for r in new:
            by_type[f"{r['doc_type']} ({r['format']})"] = by_type.get(f"{r['doc_type']} ({r['format']})", 0) + 1
        print(f"discovered {len(new)} new documents: " + ", ".join(f"{k} {v}" for k, v in sorted(by_type.items())))
        if args.dry_run:
            for r in new:
                print(f"  {r['filename']:<70} {r['date']:<10} {r['title'][:90]}")
            return
        rows += new
        new_ids = {r["id"] for r in new}

    added = 0
    for i, r in enumerate(rows):
        try:
            if download(f, r):
                added += 1
                print(f"  [{i + 1}/{len(rows)}] {r['filename']} ({r['size_mb']} MB)", flush=True)
        except Exception as e:  # keep going; the row stays without sha256 and is retried next time
            print(f"  ERROR {r['filename']}: {e}", file=sys.stderr)
        if added and added % 25 == 0:
            write_meta(rows)  # checkpoint
    missing = [r for r in rows if not (DOCS / r["filename"]).exists()]
    # newly discovered documents that could not be downloaded are not recorded
    rows = [r for r in rows if r["id"] not in new_ids or r not in missing]
    write_meta(rows)
    print(f"downloaded {added}; collection has {sum((DOCS / r['filename']).exists() for r in rows)} files" +
          (f"; missing: {[r['filename'] for r in missing][:10]}" if missing else ""))


if __name__ == "__main__":
    main()
