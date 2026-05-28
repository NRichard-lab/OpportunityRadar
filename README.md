# Financial Jobs Radar

Financial Jobs Radar is a local-first enrichment tool for banks and credit unions. Version 1 reads an Excel list of companies, finds or verifies each official website, looks for a careers/jobs page, checks for RSS or Atom feeds, detects common recruiting platforms, and writes the results to a new Excel workbook.

This first version intentionally does not include a dashboard, database, authentication, or job-listing scraper. The goal is to build clean source data first.

## Install

Use Python 3.11 or newer.

```powershell
cd C:\Users\dog10\Documents\Codex\FinancialJobsRadar
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```powershell
python main.py --input sample_companies.xlsx --output output\financial_jobs_radar_enriched.xlsx
```

If `--input` is omitted, the script uses `sample_companies.xlsx`. If `--output` is omitted, it writes to `output\financial_jobs_radar_enriched.xlsx`.

The original input workbook is never overwritten.

## Input Excel Format

The input workbook must include:

- `Company Name`

Optional columns:

- `City`
- `State`
- `Known Website`
- `Notes`

If `Known Website` is provided, the script verifies and uses it before trying web search. If it is blank, the script searches for likely official websites using phrases such as company name plus “official website”, “careers”, and “jobs”.

## Output Columns

The output workbook includes:

- `Company Name`
- `City`
- `State`
- `Known Website`
- `Official Website`
- `Careers Page URL`
- `Jobs RSS Feed URL`
- `Job Platform`
- `Feed Found`
- `Search Status`
- `Confidence`
- `Last Checked`
- `Notes`

The output workbook freezes the header row, adds filters, auto-sizes columns, and makes URL values clickable.

## Search Status Values

- `Completed`: official website and careers page were found with medium or high confidence.
- `Partial`: official website was found and at least one useful jobs-related signal was found.
- `Needs Review`: official website was found, but careers data could not be confidently discovered.
- `Failed`: no official website could be found or verified.

## Known Limitations

Many modern career sites no longer publish RSS or Atom feeds. In those cases, `Feed Found` will be `FALSE` even when a company has a normal careers page.

Web search results can vary, and some sites block automated requests or disallow crawling in `robots.txt`. The script uses polite timeouts, small crawl limits, and delays, but some rows may still need human review.

Version 1 detects career pages and recruiting platforms only; it does not scrape individual job listings.
