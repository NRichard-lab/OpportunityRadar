from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent
SUPPORTED_APP_ENVS = frozenset({"development", "production"})
SUPPORTED_AUTH_MODES = frozenset({"local", "blueash"})


def _read_bool(name: str, *, default: bool = False) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    value = raw_value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean value (true or false).")


APP_ENV = os.environ.get("APP_ENV", "").strip().lower()
APP_BASE_PATH = "/" + os.environ.get("APP_BASE_PATH", "").strip().strip("/") if os.environ.get("APP_BASE_PATH", "").strip().strip("/") else ""
APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "").strip().rstrip("/")
_public_url = urlparse(APP_PUBLIC_URL)
APP_PUBLIC_ORIGIN = f"{_public_url.scheme}://{_public_url.netloc}" if _public_url.scheme and _public_url.netloc else ""
AUTH_MODE = os.environ.get("AUTH_MODE", "").strip().lower()
BLUEASH_API_URL = os.environ.get("BLUEASH_API_URL", "").strip().rstrip("/")
BLUEASH_LOGIN_URL = os.environ.get("BLUEASH_LOGIN_URL", "").strip()
BLUEASH_SESSION_COOKIE = os.environ.get("BLUEASH_SESSION_COOKIE", "").strip()
BLUEASH_COOKIE_DOMAIN = os.environ.get("BLUEASH_COOKIE_DOMAIN", "").strip()
BLUEASH_APP_SLUG = os.environ.get("BLUEASH_APP_SLUG", "").strip()
APP_TRUSTED_ADMIN_USER_ID = os.environ.get("APP_TRUSTED_ADMIN_USER_ID", "").strip()

# Phase 1A production safety switches. Unsafe features require an explicit opt-in.
APP_ENABLE_BROWSER_JOBS = _read_bool("APP_ENABLE_BROWSER_JOBS")
APP_ENABLE_COMPANY_REFRESH = _read_bool("APP_ENABLE_COMPANY_REFRESH")
APP_ENABLE_UTILITIES = _read_bool("APP_ENABLE_UTILITIES")
APP_ENABLE_SCHEDULES = _read_bool("APP_ENABLE_SCHEDULES")
APP_ENABLE_DISCOVERY = _read_bool("APP_ENABLE_DISCOVERY")
APP_WRITE_FRONTEND_MIRRORS = _read_bool("APP_WRITE_FRONTEND_MIRRORS")

DATA_DIR = Path(os.environ.get("APP_DATA_DIR", str(BASE_DIR / "data"))).resolve()
IMPORT_DIR = Path(os.environ.get("APP_IMPORT_DIR", str(BASE_DIR / "Input" / "imports"))).resolve()
EXPORT_DIR = Path(
    os.environ.get("APP_EXPORT_DIR")
    or os.environ.get("APP_OUTPUT_DIR")
    or str(BASE_DIR / "output")
).resolve()
BACKUP_DIR = Path(os.environ.get("APP_BACKUP_DIR", str(DATA_DIR / "exports"))).resolve()
LOG_DIR = Path(os.environ.get("APP_LOG_DIR", str(BASE_DIR / "logs"))).resolve()
OUTPUT_DIR = EXPORT_DIR

DEFAULT_INPUT = BASE_DIR / "sample_companies.xlsx"
DEFAULT_MASTER = DATA_DIR / "master.xlsx"
DEFAULT_JSON_OUTPUT = DATA_DIR / "companies.json"
DEFAULT_FRONTEND_COMPANIES_JSON = BASE_DIR / "frontend" / "public" / "data" / "companies.json"
DEFAULT_JOBS_JSON = DATA_DIR / "jobs.json"
DEFAULT_FRONTEND_JOBS_JSON = BASE_DIR / "frontend" / "public" / "data" / "jobs.json"
DEFAULT_APPLICATIONS_JSON = DATA_DIR / "applications.json"
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'opportunity_radar.db').as_posix()}")
DEFAULT_DATABASE = Path(unquote(urlparse(DATABASE_URL).path.lstrip("/") if os.name == "nt" else urlparse(DATABASE_URL).path)) if DATABASE_URL.startswith("sqlite:") else DATA_DIR / "opportunity_radar.db"
DEFAULT_MIGRATION_EXPORT_DIR = BACKUP_DIR
DEFAULT_JOBS_XLSX = EXPORT_DIR / "jobs_snapshot.xlsx"


def feature_flags_payload() -> dict[str, bool]:
    return {
        "browserJobs": APP_ENABLE_BROWSER_JOBS,
        "companyRefresh": APP_ENABLE_COMPANY_REFRESH,
        "utilities": APP_ENABLE_UTILITIES,
        "schedules": APP_ENABLE_SCHEDULES,
        "discovery": APP_ENABLE_DISCOVERY,
    }

USER_AGENT = (
    "OpportunityRadar/1.0 "
    "(local company website enrichment; polite low-volume requests)"
)
REQUEST_TIMEOUT = 12
POLITE_DELAY_SECONDS = 0.75
MAX_CRAWL_PAGES = 8

OFFICIAL_WEBSITE_SEARCH_PHRASES = [
    "\"{company}\" \"credit union\"",
    "\"{company}\" \"federal credit union\"",
    "\"{company}\" \"bank\"",
    "\"{company}\" official website",
    "\"{company}\" careers",
]

CAREERS_KEYWORDS = [
    "careers",
    "jobs",
    "employment",
    "join our team",
    "work with us",
    "opportunities",
    "open positions",
    "current openings",
]

COMMON_FEED_PATHS = [
    "/feed",
    "/rss",
    "/rss.xml",
    "/jobs/rss",
    "/jobs/feed",
    "/careers/rss",
    "/careers/feed",
    "/employment/rss",
    "/employment/feed",
]

DISALLOWED_RESULT_DOMAINS = [
    "wikipedia.org",
    "facebook.com",
    "linkedin.com",
    "yelp.com",
    "crunchbase.com",
    "zoominfo.com",
    "bbb.org",
    "glassdoor.com",
    "indeed.com",
    "ziprecruiter.com",
    "jobzmall.com",
    "monster.com",
    "careerbuilder.com",
    "simplyhired.com",
    "jooble.org",
    "salary.com",
    "mapquest.com",
    "yellowpages.com",
    "opencorporates.com",
    "dnb.com",
    "ncua.gov",
    "routingnumber",
    "usbanklocations.com",
    "bankbranchlocator.com",
    "branchspot.com",
]

OUTPUT_COLUMNS = [
    "Company ID",
    "Company Name",
    "Industry",
    "City",
    "State",
    "Country",
    "Known Website",
    "Official Website",
    "Website Discovery Method",
    "Website Candidate URLs",
    "Website Verification Notes",
    "Website Verified",
    "Careers Page URL",
    "Job Board URL",
    "Job Board Discovery Method",
    "Jobs RSS Feed URL",
    "Job Platform",
    "Feed Found",
    "Search Status",
    "Confidence",
    "Last Checked",
    "Notes",
    "Founded Year",
    "Total Assets",
    "Assets As Of Date",
    "Company Information Last Checked",
]
