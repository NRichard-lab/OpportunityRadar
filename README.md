# Financial Jobs Radar

Financial Jobs Radar is a local-first enrichment tool for banks and credit unions. Version 1 reads an Excel list of companies, finds or verifies each official website, looks for a careers/jobs page, checks for RSS or Atom feeds, detects common recruiting platforms, and writes the results to a new Excel workbook.

## Opportunity Radar app (in progress)

The repository now also contains the beginning of the later local-first **Opportunity Radar** app:

- `backend/`: FastAPI API with SQLite as the source of truth.
- `frontend/`: React/Vite dashboard with Dashboard, Companies, Job List, Jobs Applied For, and Resume Match tabs.
- `data/opportunity_radar.db`: Created on first backend startup and intentionally excluded from Git.

The dashboard starts with a small sample record so the data flow is visible. It does not search the web automatically, apply to jobs, log in to sites, bypass CAPTCHAs, or send resume/job text to AI services.

### Run the app locally

In one terminal:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn backend.main:app --reload
```

In another terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

### Debug job collection

Job collection is always user initiated and uses only each company’s saved Verified Job Board URL. To collect all eligible companies and write diagnostic artifacts:

```powershell
python main.py --debug-job-collection
```

To limit the diagnostic run to one company:

```powershell
python main.py --debug-job-collection --company-id 1
```

Diagnostics are written to `output/job_collection_diagnostics.xlsx`, `logs/job_collection_diagnostics.json`, and `logs/job_collection_debug/`.

The Opportunity Radar app currently includes the dashboard and local SQLite data layer. Authentication, automatic job-listing scraping, job applications, CAPTCHA bypassing, and external AI integrations are not implemented.

## Install

Use Python 3.11 or newer.

```powershell
cd path\to\FinancialJobsRadar
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
