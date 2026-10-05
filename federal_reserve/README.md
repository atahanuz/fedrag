# Federal Reserve document collection

**391 documents in five formats, 199 MB, dated 2022 to October 2, 2026.** 387 are publications of the
Board of Governors of the Federal Reserve System; four data files come from the Federal Reserve Banks of
New York and Philadelphia. All are public domain and were downloaded from the publishers' websites.

| Format | Files | What they are |
| --- | ---: | --- |
| PDF | 103 | reports, minutes, Beige Books, working papers, press conference transcripts, survey reports (4,914 pages, all with a text layer) |
| HTML | 264 | web pages: FOMC statements, Summaries of Economic Projections (with all their tables), speeches, testimony, FEDS Notes, SR letters, Senior Loan Officer surveys, press releases |
| Word (.docx) | 2 | Survey of Consumer Finances interview program (questionnaire) and the guide to reproducing its Bulletin tables |
| Excel (.xlsx) | 5 | SCF historical tables (115 sheets), NY Fed household debt (73 sheets, incl. chart sheets), NY Fed consumer expectations (45 sheets), Philadelphia Fed SPF median forecasts (58 sheets), stress-test market shocks (19 sheets) |
| CSV | 17 | stress-test results for every bank in every exercise since 2013, scenario variables (2024-2026), nine-quarter projection paths |

The 93 PDFs of the original set (downloaded 2026-09-23) are unchanged. `scripts/fetch_corpus.py --discover`
found the other 298 on 2026-10-05; `scripts/fetch_corpus.py` re-downloads every file listed in
`metadata.csv` and checks its SHA-256.

## Documents by type

| `doc_type` | Docs | Format | Dates | What it is |
| --- | ---: | --- | --- | --- |
| `feds_note` | 73 | html | Jan - Oct 2026 | FEDS Notes: short research notes by Board staff (many with data tables) |
| `speech` | 69 | html | Jan - Oct 2026 | Speeches by the Chair, Vice Chairs and Governors (Bowman 20, Waller 13, Barr 12, Cook 10, Jefferson 8, Powell 3, Miran 2, Warsh 1) |
| `press_release` | 44 | html | Jan - Oct 2026 | Board press releases: regulation, stress tests, discount-rate minutes, announcements (e.g. the change of Chair) |
| `fomc_statement` | 34 | html | Feb 2023 - Sep 2026 | The FOMC statement of every meeting (decision and vote) and the Statement on Longer-Run Goals (2023, 2024, 2025 revision, 2026) |
| `meeting_minutes` | 29 | pdf | Feb 2023 - Jul 2026 | Minutes of every FOMC meeting |
| `stress_test` | 26 | pdf/csv/xlsx | 2023 - 2026 | Stress test results, scenarios, methodology, capital requirements; bank-level results 2013-2026, scenario variables and paths as data files |
| `working_paper` | 20 | pdf | May - Sep 2026 | Finance and Economics Discussion Series (FEDS) papers |
| `economic_projections` | 15 | html | Mar 2023 - Sep 2026 | Summary of Economic Projections: projection tables, dot plot, distributions, uncertainty and risks |
| `economic_conditions_report` | 14 | pdf | Jan 2025 - Sep 2026 | Beige Books (12 District reports + national summary) |
| `loan_officer_survey` | 11 | html | Jan 2024 - Jul 2026 | Senior Loan Officer Opinion Survey on Bank Lending Practices |
| `supervisory_letter` | 10 | html | Aug 2025 - Sep 2026 | SR letters (supervisory guidance) |
| `testimony` | 8 | html | Feb 2025 - Jul 2026 | Congressional testimony (semiannual monetary policy, supervision) |
| `financial_stability_report` | 7 | pdf | May 2023 - May 2026 | Financial Stability Reports |
| `monetary_policy_report` | 7 | pdf | Mar 2023 - Jul 2026 | Monetary Policy Reports to Congress |
| `household_survey` | 7 | pdf/docx/xlsx | 2023 - 2026 | Survey of Consumer Finances (Bulletin, tables, instrument, guide), SHED report on 2025, NY Fed household debt and consumer expectations data |
| `supervisory_report` | 6 | pdf | May 2023 - Jun 2026 | Supervision and Regulation Reports |
| `press_conference` | 6 | pdf | Jan - Sep 2026 | Transcripts of the Chair's press conferences after each 2026 FOMC meeting |
| `annual_report` | 3 | pdf | 2022 - 2024 | Board annual reports |
| `forecaster_survey` | 2 | pdf/xlsx | Jul - Aug 2026 | Philadelphia Fed Survey of Professional Forecasters: median forecasts and the documentation of its variable codes |

## Content characteristics

- **Structured data is spread over formats.** Bank-level stress-test results are a CSV; scenario
  variables are CSV files per year; the SEP's projection tables and dot plot are HTML tables; household
  finance tables are Excel sheets with multi-row merged headers, row groups and notes; some data are only
  interpretable with a separate document (the SPF variable codes are defined in its PDF documentation).
  `python -m fedrag.ingest.build_corpus` turns all of them into 846 SQL tables plus 20 views that stack a
  recurring table across releases (for example SEP Table 1 for every meeting since March 2023).
- **Recurring series** allow comparisons over time: FOMC statements and minutes, SEPs, Beige Books,
  SLOOS, stress tests, FSRs and MPRs.
- **2026 events** not in the original set: the September 2026 rate increase, the change of Chair (Kevin
  Warsh sworn in on May 22, 2026), the Chair's task forces, Alan Greenspan's death.

## `metadata.csv` columns

| Column | Description |
| --- | --- |
| `filename`, `id` | File name in this folder; the id is the name without the extension |
| `doc_type` | Document category, as in the table above |
| `title` | Document title (speeches and testimony include the speaker and venue) |
| `date` | `YYYY-MM-DD` for dated releases (meeting date for FOMC minutes, statements and SEPs), `YYYY-MM` for monthly releases, `YYYY` for annual reports (the year covered) |
| `pages` | PDF pages (other formats: blank) |
| `size_mb`, `sha256`, `retrieved` | File size, SHA-256 hash and download date |
| `has_text_layer` | `True` if a PDF's text can be extracted without OCR (true for every PDF) |
| `url` | Original download URL |
| `format` | `pdf`, `html`, `docx`, `xlsx` or `csv` |
| `publisher` | Board of Governors, or the Federal Reserve Bank that published the file |
| `speaker` | Speaker of a speech or testimony |
| `description` | Data dictionary for data files whose columns or sheet names are codes |

## File naming

`fed_<series>_<date or id>.<ext>` for Board documents (`fed_fomc_statement_2026-09-16.html`,
`fed_speech_warsh20260828a.html`, `fed_dfast_results_2013-2026.csv`), `nyfed_...` and `philfed_...` for the
Reserve Bank files.

## Usage rights

Publications of the Federal Reserve Board are in the public domain. The New York Fed's Survey of
Consumer Expectations data are free to use under its license (the workbook's License sheet), with
attribution to the Federal Reserve Bank of New York.
