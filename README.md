# Financial Jobs Radar

Financial Jobs Radar is a local-first app for tracking banks and credit unions, discovered job openings, resume/job fit, and jobs applied for.

The app is organized around one permanent company workbook and separate generated snapshots:

```text
data/
  master.xlsx
  companies.json
  jobs.json
  applications.json

output/
  jobs_snapshot.xlsx

logs/
```

## Data Rules

`data/master.xlsx` is the source of truth for company records. `data/companies.json` is generated from `master.xlsx` and overwritten on every export. It is never appended to.

`data/jobs.json` is the current discovered job listing snapshot. It can be overwritten on every job refresh. User application tracking is stored separately in `data/applications.json` or browser local storage so it is not lost when job listings refresh.

The frontend reads generated JSON from:

```text
frontend/public/data/companies.json
frontend/public/data/jobs.json
```

The Python export commands mirror generated JSON into those frontend-safe paths.

Official websites must not be guessed from a company name. Financial Jobs Radar only stores an official website after validating a provided Known Website, a real web search result, or a link discovered from an already verified source. Low-confidence matches are left for review unless explicitly allowed.

`Official Website` and `Careers Page URL` are discovery inputs only. They can help `fill-missing-job-boards` find the true public job board, but `collect-jobs` does not collect postings from them. If a careers page directly lists open roles, store that page in `Job Board URL` first.

## Setup

Use Python 3.11 or newer.

```powershell
cd C:\Users\dog10\Documents\Codex\FinancialJobsRadar
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Optional browser automation setup:

```powershell
pip install playwright
python -m playwright install chromium
```

Browser automation is manual/on-demand only. It does not run during startup or `export-json`.

## Commands

Export companies JSON from the master workbook, with no web search:

```powershell
python main.py --mode export-json --input data\master.xlsx --output-json data\companies.json
```

Bootstrap enrichment for a large initial Excel list. Run this rarely:

```powershell
python main.py --mode bootstrap-enrich --input input\companies.xlsx --master data\master.xlsx --use-browser-discovery
```

By default, `bootstrap-enrich` runs in fast static mode only. It performs website search, static careers discovery, feed checks, and static platform-link detection without Playwright. Browser discovery runs only when `--use-browser-discovery` is explicitly provided, and then only after static discovery fails to find a job board URL.

Speed and caching controls:

```powershell
python main.py --mode bootstrap-enrich --input input\companies.xlsx --master data\master.xlsx --max-workers 15 --browser-workers 3 --skip-recent-days 7
python main.py --mode bootstrap-enrich --input input\companies.xlsx --master data\master.xlsx --force
```

Rows are skipped unless `--force` is used when `Website Verified` is true and `Job Board URL` already exists, or when `Last Checked` is within `--skip-recent-days`. Logs include phase timing, progress counts, average seconds per company, estimated time remaining, and Excel write time.

Fill missing job board URLs from `master.xlsx`, then regenerate `companies.json`:

```powershell
python main.py --mode fill-missing-job-boards --master data\master.xlsx --output-json data\companies.json --limit 10 --use-browser-discovery
```

Collect current public job listings from companies with `Job Board URL` values:

```powershell
python main.py --mode collect-jobs --master data\master.xlsx --jobs-json data\jobs.json --jobs-xlsx output\jobs_snapshot.xlsx --limit-companies 25
```

Audit existing official website values and write review files:

```powershell
python main.py --mode audit-websites --master data\master.xlsx
```

Repair missing or suspicious official websites in small batches. This searches and validates candidates, then updates `master.xlsx` only when a High or Medium confidence candidate is found:

```powershell
python main.py --mode repair-websites --master data\master.xlsx --limit 25
```

Both website audit and repair support targeted and dry-run checks:

```powershell
python main.py --mode audit-websites --master data\master.xlsx --company "WECU" --dry-run
python main.py --mode repair-websites --master data\master.xlsx --company "WECU" --dry-run
```

Useful collection controls:

```powershell
python main.py --mode collect-jobs --max-workers 10 --browser-workers 3 --delay-seconds 1 --limit-companies 25 --company "WECU" --dry-run --debug-job-collection
```

`collect-jobs` splits work by collector type. HTTP/static collectors use `--max-workers`; Playwright/browser collectors use `--browser-workers` so the app does not launch too many browsers at once. Progress logs include company count, company name, collector, worker type, jobs saved, duration, average seconds per company, and estimated time remaining.

Saved job records include pay enrichment fields:

- `payMin`
- `payMax`
- `payText`
- `payPeriod`
- `payCurrency`

Pay extraction checks structured `JobPosting` data, rendered detail-page text, descriptions, and listing/card text. Debug collection logs include pay extraction source, candidate text, matched pattern, parsed min/max, and pay period.

Saved job records also include role classification:

- `roleType`: `IC`, `MGR`, `EXEC`, or `UNKNOWN`
- `roleTypeReason`: the title or description signal used for classification

The Job List tab can filter and sort by role type, and job cards/details show the role type metadata.

Start the frontend:

```powershell
cd frontend
npm run dev
```

Start the local backend API in another PowerShell window:

```powershell
python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000
```

Backend health check:

```text
GET http://127.0.0.1:8000/api/status
```

The Dashboard button `Find Missing Job Board URLs` calls:

```text
POST http://127.0.0.1:8000/api/fill-missing-job-boards
```

That button runs the same on-demand logic as:

```powershell
python main.py --mode fill-missing-job-boards --master data\master.xlsx --output-json data\companies.json --limit 10 --use-browser-discovery
```

It does not run on app startup. Browser automation only runs when the button is clicked.

## Deduping Companies

Company export dedupes rows before writing JSON.

Deduping keys:

- Official website domain when available.
- Otherwise normalized company name plus state.

Best-data-wins merge rules:

- Prefer row with `Job Board URL`.
- Prefer row with `Careers Page URL`.
- Prefer row with `Official Website`.
- Prefer higher confidence.
- Prefer newer `Last Checked`.
- Combine unique notes.

Company IDs are stable:

- Normalized official website domain when available.
- Otherwise normalized company name plus state.

Duplicate counts and merge results are logged.

## Job Collection

Collectors live in:

```text
collectors/
  base.py
  adp_collector.py
  workday_collector.py
  greenhouse_collector.py
  lever_collector.py
  icims_collector.py
  generic_collector.py
```

Collector selection is based on `Job Platform` or `Job Board URL`. ADP Workforce Now is supported first because WECU uses ADP. The generic collector looks for real job-card/listing containers, schema.org `JobPosting` data, and detail-page titles; it does not save generic links, buttons, navigation, benefits/culture pages, or accessibility text as postings.

Source rules:

- `collect-jobs` uses `Job Board URL` as the normal source of postings.
- A validated `Jobs RSS Feed URL` may be used only when `Feed Found` is true.
- `Official Website` and `Careers Page URL` are never fallback collection sources.
- Companies missing `Job Board URL` are skipped and logged as `Missing Job Board URL`.
- Non-job URLs such as loan applications, online banking, login, benefits, culture, locations, privacy, and terms pages are skipped as `Invalid Job Board URL`.
- This prevents navigation text, marketing text, benefits pages, and careers landing page content from being saved as jobs.

Rules:

- Do not apply for jobs.
- Do not submit forms.
- Do not log in.
- Do not bypass captchas or access controls.
- Only collect public job listing data.

`collect-jobs` logs total companies reviewed, companies skipped for missing job board URLs, companies attempted, jobs found, errors, duration, and average seconds per company.

Use `--debug-job-collection` to write candidate/rejection debug files under `logs/job_collection_debug/`.

The frontend Job List loads the generated job snapshot, removes invalid job records, applies filters and sorting, then paginates the results. Use the page size selector to show 10, 25, 50, or 100 jobs per page.

## Resume Fit

The Resume Fit Score estimates how well the resume appears to match visible job requirements, title alignment, skills, experience keywords, and banking or credit union relevance. It is not a hiring prediction.
