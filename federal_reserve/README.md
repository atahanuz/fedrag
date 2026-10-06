# Federal Reserve document collection

**725 documents in five formats, 269 MB, dated January 2022 to October 5, 2026.** 721 are publications of
the Board of Governors of the Federal Reserve System; four data files come from the Federal Reserve Banks of
New York and Philadelphia. All are public domain and were downloaded from the publishers' websites.

| Format | Files | What they are |
| --- | ---: | --- |
| PDF | 151 | reports, minutes, Beige Books, press conference transcripts, working papers, survey reports (6,337 pages, all with a text layer) |
| HTML | 550 | web pages: FOMC statements, Summaries of Economic Projections (with all their tables), speeches, testimony, FEDS Notes, press releases, SR letters, Senior Loan Officer surveys |
| Word (.docx) | 2 | Survey of Consumer Finances interview program (questionnaire) and the guide to reproducing its Bulletin tables |
| Excel (.xlsx) | 5 | SCF historical tables (115 sheets), NY Fed household debt (73 sheets, incl. chart sheets), NY Fed consumer expectations (45 sheets), Philadelphia Fed SPF median forecasts (58 sheets), stress-test market shocks (19 sheets) |
| CSV | 17 | stress-test results for every bank in every exercise since 2013, scenario variables (2024-2026), nine-quarter projection paths |

The 93 PDFs of the original set (downloaded 2026-09-23) are unchanged. `scripts/fetch_corpus.py --discover`
found 298 more documents on 2026-10-05 and another 334 on 2026-10-06 (2025 speeches, FEDS Notes and press
releases; 2023-2025 press conferences; 2023-2024 Beige Books; 2022 minutes; 2024 SR letters).
`scripts/fetch_corpus.py` re-downloads every file listed in `metadata.csv` and checks its SHA-256.

## Documents by type

| `doc_type` | Docs | Format | Dates | What it is |
| --- | ---: | --- | --- | --- |
| `speech` | 186 | html | Jan 2025 - Oct 2026 | Speeches by the Chair, Vice Chairs and Governors (Bowman 39, Barr 36, Waller 33, Jefferson 21, Cook 21, Powell 15, Kugler 14, Miran 6, Warsh 1) |
| `feds_note` | 164 | html | Jan 2025 - Oct 2026 | FEDS Notes: short research notes by Board staff (many with data tables) |
| `press_release` | 118 | html | Jan 2025 - Oct 2026 | Board press releases: regulation, stress tests, discount-rate minutes, announcements (e.g. the change of Chair) |
| `meeting_minutes` | 37 | pdf | Jan 2022 - Jul 2026 | Minutes of every FOMC meeting |
| `fomc_statement` | 34 | html | Feb 2023 - Sep 2026 | The FOMC statement of every meeting (decision and vote) and the Statement on Longer-Run Goals (2023, 2024, 2025 revision, 2026) |
| `economic_conditions_report` | 30 | pdf | Jan 2023 - Sep 2026 | Beige Books (12 District reports + national summary) |
| `press_conference` | 30 | pdf | Feb 2023 - Sep 2026 | Transcripts of the Chair's press conferences after each FOMC meeting |
| `stress_test` | 26 | pdf/csv/xlsx | 2023 - 2026 | Stress test results, scenarios, methodology, capital requirements; bank-level results 2013-2026, scenario variables and paths as data files |
| `working_paper` | 20 | pdf | May - Sep 2026 | Finance and Economics Discussion Series (FEDS) papers |
| `economic_projections` | 15 | html | Mar 2023 - Sep 2026 | Summary of Economic Projections: projection tables, dot plot, distributions, uncertainty and risks |
| `supervisory_letter` | 14 | html | Jul 2024 - Oct 2026 | SR letters (supervisory guidance) |
| `loan_officer_survey` | 11 | html | Jan 2024 - Jul 2026 | Senior Loan Officer Opinion Survey on Bank Lending Practices |
| `testimony` | 8 | html | Feb 2025 - Jul 2026 | Congressional testimony (semiannual monetary policy, supervision) |
| `financial_stability_report` | 7 | pdf | May 2023 - May 2026 | Financial Stability Reports |
| `monetary_policy_report` | 7 | pdf | Mar 2023 - Jul 2026 | Monetary Policy Reports to Congress (there was no February 2026 report) |
| `household_survey` | 7 | pdf/docx/xlsx | 2023 - 2026 | Survey of Consumer Finances (Bulletin, tables, instrument, guide), SHED report on 2025, NY Fed household debt and consumer expectations data |
| `supervisory_report` | 6 | pdf | May 2023 - Jun 2026 | Supervision and Regulation Reports |
| `annual_report` | 3 | pdf | 2022 - 2024 | Board annual reports |
| `forecaster_survey` | 2 | pdf/xlsx | Jul - Aug 2026 | Philadelphia Fed Survey of Professional Forecasters: median forecasts and the documentation of its variable codes |

## Content characteristics

- **Structured data is spread over formats.** Bank-level stress-test results are a CSV; scenario
  variables are CSV files per year; the SEP's projection tables and dot plot are HTML tables; household
  finance tables are Excel sheets with multi-row merged headers, row groups and notes; some data are only
  interpretable with a separate document (the SPF variable codes are defined in its PDF documentation).
  `python -m fedrag.ingest.build_corpus` turns all of them into 978 SQL tables plus 20 views that stack a
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
