# Federal Reserve Board document set

93 PDFs published by the Board of Governors of the Federal Reserve System: **4,569 pages, 148.5 MB**, dated
2022 to September 2026. All are in English, and every file has an extractable text layer, so none need OCR.
All were downloaded from federalreserve.gov on 2026-09-23.

`metadata.csv` in this folder has one row per PDF. The same records also appear in `../../manifest.csv`,
filtered to `org_slug = federal_reserve`.

## Documents by type

| `doc_type` | Docs | Pages (each) | Dates | What it is |
| --- | ---: | ---: | --- | --- |
| `meeting_minutes` | 29 | 10–21 | Feb 2023 – Jul 2026 | Minutes of every FOMC meeting: staff economic and financial outlook, participants' views, the policy decision and votes |
| `economic_conditions_report` | 14 | 54–58 | Jan 2025 – Sep 2026 | Beige Books: anecdotal reports on business conditions from each of the 12 Federal Reserve Districts |
| `monetary_policy_report` | 7 | 71–83 | Mar 2023 – Jul 2026 | Semiannual Monetary Policy Reports to Congress on the economy, inflation, the labor market and policy |
| `financial_stability_report` | 7 | 69–85 | May 2023 – May 2026 | Semiannual reports on asset valuations, borrowing, leverage and funding risks; each includes a survey of salient risks |
| `supervisory_report` | 6 | 38–42 | May 2023 – Jun 2026 | Supervision and Regulation Reports: banking-system conditions, supervisory priorities, regulatory changes |
| `stress_test` | 7 | 3–70 | Jun 2023 – Jun 2026 | Stress test results for 2023–2026, the 2026 scenarios and methodology, and August 2025 large-bank capital requirements |
| `annual_report` | 3 | 214–224 | 2022 – 2024 | Board annual reports: operations, supervision, payment systems, audited financial statements |
| `working_paper` | 20 | 18–117 | May – Sep 2026 | Finance and Economics Discussion Series (FEDS) research papers on banking, monetary policy, credit and markets |

## Content characteristics

- **Tables and charts.** These are concentrated in the stress test documents, which have firm-by-firm capital
  ratio tables, and in the Financial Stability Reports. Sampled pages showed figures on about half of stress-test
  pages and two-thirds of Financial Stability Report pages. The Monetary Policy Reports and annual reports have
  them too, but less often.
- **Plain prose.** The FOMC minutes and Beige Books are almost entirely text.
- **Recurring series.** Most types are published at regular intervals, so questions can compare publications over
  time, for example "How did the FOMC's view of inflation change between March and June 2026?"
- **Document-specific facts.** Most answers depend on a date, a Federal Reserve District or a bank. About two-thirds
  of the documents are from 2025–2026.

## `metadata.csv` columns

| Column | Description |
| --- | --- |
| `filename` | PDF file name in this folder |
| `id` | Unique document ID, the file name without `.pdf` |
| `doc_type` | Document category, as in the table above |
| `title` | Document title. Working-paper titles end with their FEDS number, e.g. `(FEDS 2026-065)` |
| `date` | Publication date. `YYYY-MM-DD` for meeting-based series, where it is the **meeting date** for FOMC minutes (minutes are released about 3 weeks later). `YYYY-MM` for monthly releases. `YYYY` for annual reports, where it is the year covered |
| `pages` | Page count |
| `size_mb` | File size in MB |
| `has_text_layer` | `True` if text can be extracted without OCR (true for every file) |
| `sha256` | SHA-256 hash of the file, for integrity checks and deduplication |
| `url` | Original download URL on federalreserve.gov |
| `retrieved` | Download date |

## File naming

The files follow `fed_<series>_<date>.pdf`, for example `fed_fomc_minutes_2026-07-29.pdf`,
`fed_beige_book_2026-09-02.pdf` and `fed_feds_wp_2026-065.pdf`.

## Usage rights

These are publications of a U.S. federal agency and are in the public domain.
