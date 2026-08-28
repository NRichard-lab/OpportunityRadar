# Opportunity Radar

Opportunity Radar tracks companies, discovered job openings, resume/job fit, and applications.

The Phase 1A source baseline retains the existing Blue Ash shared-cookie integration as a
transitional contract. It is not a production deployment or the final authentication handoff.
See [docs/BLUEASH_AUTH_INTEGRATION.md](docs/BLUEASH_AUTH_INTEGRATION.md).

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

`data/opportunity_radar.db` is the source of truth for companies, jobs, applications, resumes, raw candidates, settings, utility history, and import history. The FastAPI routes and frontend read from SQLite.

`data/master.xlsx`, `data/companies.json`, `data/jobs.json`, and `data/applications.json` are compatibility imports, exports, backups, and recovery snapshots. Company CRUD regenerates the relevant private snapshots, but runtime reads do not depend on them. The browser does not retain raw resume or application recovery payloads; authenticated API/SQLite state is authoritative.

## SQLite Migration

Preview the migration without creating directories, backups, reports, or a database:

```powershell
py -m backend.cli migrate-to-sqlite --preview
```

Apply the migration transactionally:

```powershell
py -m backend.cli migrate-to-sqlite --apply
```

The apply command first copies every detected source artifact to `data/exports/pre_sqlite_migration_YYYYMMDD_HHMMSS/`. Its `manifest.json` records SHA-256 hashes, row counts, sizes, source timestamps, and the migration version. Import and CRUD/cascade validation occur against a temporary SQLite database; `data/opportunity_radar.db` is atomically activated only after foreign-key, record-ID, schema, integrity, and CRUD checks pass.

JSON and XLSX reports are written to `output/sqlite_migration_report_YYYYMMDD_HHMMSS.*`. Duplicate legacy job IDs are repaired deterministically and listed individually in both reports. Stable company IDs are never regenerated during migration.

Legacy local-development mirrors can be written to:

```text
frontend/public/data/companies.json
frontend/public/data/jobs.json
```

They are disabled by default and require an explicit development-only
`APP_WRITE_FRONTEND_MIRRORS=true`. Production rejects that setting so private runtime data is never
written into the frontend public directory. Runtime screens load authoritative records through the
authenticated FastAPI/SQLite API.

Official websites must not be guessed from a company name. Opportunity Radar only stores an official website after validating a provided Known Website, a real web search result, or a link discovered from an already verified source. Low-confidence matches are left for review unless explicitly allowed.

`Official Website` and `Careers Page URL` are discovery inputs only. They can help `fill-missing-job-boards` find the true public job board, but `collect-jobs` does not collect postings from them. If a careers page directly lists open roles, store that page in `Job Board URL` first.

## Setup

Use Python 3.11 or newer.

```powershell
cd <repository-path>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

The application does not automatically load `.env`. Set an explicit local environment before
starting the backend:

```powershell
$env:APP_ENV = "development"
$env:AUTH_MODE = "local"
$env:APP_PUBLIC_URL = "http://127.0.0.1:5173"
```

Missing or misspelled environment/authentication modes fail startup. Local authentication is valid
only in explicit development mode with a loopback public URL. Production requires Blue Ash
configuration plus the exact portal UUID in `APP_TRUSTED_ADMIN_USER_ID`.

Optional browser automation setup:

```powershell
pip install playwright
python -m playwright install chromium
```

Browser automation is manual/on-demand only. It does not run during startup or `export-json`.
Discovery and collection commands additionally require their corresponding explicit feature flags.

## Runtime Paths and Initial Feature Policy

Writable paths can be configured with `APP_DATA_DIR`, `DATABASE_URL`, `APP_IMPORT_DIR`,
`APP_EXPORT_DIR`, `APP_BACKUP_DIR`, and `APP_LOG_DIR`. Keep them outside an immutable application
image in a future hosted deployment.

For a subpath deployment, set the frontend build-time `VITE_BASE_PATH` to the same path as
`APP_BASE_PATH`, including a trailing slash. For the documented production URL, build with
`VITE_BASE_PATH=/OpportunityRadar/`; `APP_BASE_PATH` alone does not configure Vite unless it is
exported into the frontend build process.

The first-release switches default off: `APP_ENABLE_BROWSER_JOBS`,
`APP_ENABLE_COMPANY_REFRESH`, `APP_ENABLE_UTILITIES`, `APP_ENABLE_SCHEDULES`, and
`APP_ENABLE_DISCOVERY`. The API enforces them before starting work, and the frontend displays
disabled controls. The initial production mode also limits all protected APIs to the exact trusted
administrator ID.

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
python main.py --mode bootstrap-enrich --input input\companies.xlsx --master data\master.xlsx --max-workers 4 --browser-workers 1 --skip-recent-days 7
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
python main.py --mode collect-jobs --max-workers 4 --browser-workers 1 --delay-seconds 1 --limit-companies 25 --company "WECU" --dry-run --debug-job-collection
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
GET http://127.0.0.1:8000/api/health
```

See [Production runtime contract](docs/PRODUCTION_RUNTIME.md) before creating a hosted deployment.

## Utilities Tab

The `Utilities` tab centralizes local refresh and maintenance actions. It is disabled by default.
For intentional local development, set the required feature flags explicitly before starting the
backend. These buttons only work when the local backend API is running:

```powershell
python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000
```

The frontend can then be started separately:

```powershell
cd frontend
npm run dev
```

Utilities include company information refresh, company discovery, job collection, saved-job reprocessing, backup, export, and user-selected import. Technical worker, browser, delay, and debugging settings remain command-line concerns rather than normal-user controls.

Every maintenance card shows its last run, last runtime, average runtime from the most recent 20 successful runs, result, and recent execution history. Runs and progress are persisted in SQLite, survive browser navigation, and are marked failed if the backend restarts while they are active.

All utilities except user-selected import can be scheduled once per day. Schedules are backend-owned and continue without an open browser. The default application timezone is `America/Denver`; changing it in a schedule editor updates the shared scheduler timezone. Manual and scheduled triggers use the same registered maintenance function and create the same history records.

Maintenance APIs:

```text
GET  /api/maintenance/jobs
POST /api/maintenance/jobs/{job_key}/run
PUT  /api/maintenance/jobs/{job_key}/schedule
GET  /api/maintenance/jobs/{job_key}/history
```

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

The frontend Job List loads authoritative jobs from the authenticated API/SQLite store, removes invalid job records, applies filters and sorting, then paginates the results. Use the page size selector to show 10, 25, 50, or 100 jobs per page.

## Resume Fit

The Resume Fit Score estimates how well the resume appears to match visible job requirements, title alignment, skills, and experience terms. It is not a hiring prediction.

## Email Service

SMTP provider and daily digest settings are managed under `Utilities > Email`. Passwords are encrypted before SQLite storage and are never returned by the API.

For a hosted deployment, set a stable secret before starting the backend:

```text
OPPORTUNITY_RADAR_SECRET_KEY=<a long deployment secret>
```

Desktop installs without that environment variable use the protected `data/.email_secret.key` recovery key. Keep that file with database backups; losing it requires re-entering the SMTP password.
