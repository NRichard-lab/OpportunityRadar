from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent
SUPPORTED_APP_ENVS = frozenset({"development", "production"})
SUPPORTED_AUTH_MODES = frozenset({"local", "portal_handoff"})
PRODUCTION_BROWSER_EGRESS_MODE = "network_namespace_dns_pinned_proxy_v1"


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


def _read_positive_int(name: str, *, default: int, maximum: int = 256) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer.") from exc
    if value < 1 or value > maximum:
        raise RuntimeError(f"{name} must be between 1 and {maximum}.")
    return value


def _read_nonnegative_int(name: str, *, default: int, maximum: int = 256) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a non-negative integer.") from exc
    if value < 0 or value > maximum:
        raise RuntimeError(f"{name} must be between 0 and {maximum}.")
    return value


APP_ENV = os.environ.get("APP_ENV", "").strip().lower()
APP_BASE_PATH = "/" + os.environ.get("APP_BASE_PATH", "").strip().strip("/") if os.environ.get("APP_BASE_PATH", "").strip().strip("/") else ""
APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "").strip().rstrip("/")
_public_url = urlparse(APP_PUBLIC_URL)
APP_PUBLIC_ORIGIN = f"{_public_url.scheme}://{_public_url.netloc}" if _public_url.scheme and _public_url.netloc else ""
AUTH_MODE = os.environ.get("AUTH_MODE", "").strip().lower()
BLUEASH_PORTAL_PUBLIC_URL = os.environ.get("BLUEASH_PORTAL_PUBLIC_URL", "").strip().rstrip("/")
BLUEASH_PORTAL_API_URL = os.environ.get("BLUEASH_PORTAL_API_URL", "").strip().rstrip("/")
BLUEASH_AUTH_CLIENT_ID = os.environ.get("BLUEASH_AUTH_CLIENT_ID", "").strip()
BLUEASH_AUTH_CLIENT_SECRET = os.environ.get("BLUEASH_AUTH_CLIENT_SECRET", "").strip()
OPPORTUNITY_RADAR_SECRET_KEY = os.environ.get("OPPORTUNITY_RADAR_SECRET_KEY", "").strip()
RADAR_SESSION_COOKIE_NAME = os.environ.get(
    "RADAR_SESSION_COOKIE_NAME",
    "__Host-opportunity_radar_session" if APP_ENV == "production" else "opportunity_radar_session",
).strip()
RADAR_SESSION_IDLE_SECONDS = _read_positive_int(
    "RADAR_SESSION_IDLE_SECONDS", default=30 * 60, maximum=30 * 60
)
RADAR_SESSION_ABSOLUTE_MAX_SECONDS = _read_positive_int(
    "RADAR_SESSION_ABSOLUTE_MAX_SECONDS", default=8 * 60 * 60, maximum=24 * 60 * 60
)
RADAR_INTROSPECTION_CACHE_SECONDS = _read_nonnegative_int(
    "RADAR_INTROSPECTION_CACHE_SECONDS", default=0, maximum=30
)
RADAR_HANDOFF_STATE_TTL_SECONDS = _read_positive_int(
    "RADAR_HANDOFF_STATE_TTL_SECONDS", default=5 * 60, maximum=10 * 60
)
APP_TRUSTED_ADMIN_USER_ID = os.environ.get("APP_TRUSTED_ADMIN_USER_ID", "").strip()

# Phase 1A production safety switches. Unsafe features require an explicit opt-in.
APP_ENABLE_BROWSER_JOBS = _read_bool("APP_ENABLE_BROWSER_JOBS")
APP_ENABLE_COMPANY_REFRESH = _read_bool("APP_ENABLE_COMPANY_REFRESH")
APP_ENABLE_UTILITIES = _read_bool("APP_ENABLE_UTILITIES")
APP_ENABLE_SCHEDULES = _read_bool("APP_ENABLE_SCHEDULES")
APP_ENABLE_DISCOVERY = _read_bool("APP_ENABLE_DISCOVERY")
APP_WRITE_FRONTEND_MIRRORS = _read_bool("APP_WRITE_FRONTEND_MIRRORS")
APP_BROWSER_EGRESS_MODE = os.environ.get("APP_BROWSER_EGRESS_MODE", "disabled").strip().lower()

# A production process must never turn a missing persistent mount into a new,
# empty SQLite database.  Production defaults fail closed even if the setting
# is accidentally omitted; development keeps the existing opt-in behavior so
# explicit local initialization and migration commands can create a database.
REQUIRE_EXISTING_DATABASE = _read_bool(
    "REQUIRE_EXISTING_DATABASE", default=APP_ENV == "production"
)

# First-release in-process concurrency limits. Every caller is clamped to these
# values; the global mutation gate still permits only one shared-data operation.
APP_MAX_HTTP_WORKERS = _read_positive_int("APP_MAX_HTTP_WORKERS", default=4, maximum=32)
APP_MAX_BROWSER_WORKERS = _read_positive_int("APP_MAX_BROWSER_WORKERS", default=1, maximum=4)
APP_MAX_ACTIVE_MAINTENANCE = _read_positive_int("APP_MAX_ACTIVE_MAINTENANCE", default=1, maximum=1)

MAX_RESUME_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_RESUME_PDF_PAGES = 100
MAX_RESUME_DOCX_FILES = 2_000
MAX_RESUME_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_RESUME_EXTRACTED_TEXT_CHARS = 2_000_000
MAX_IMPORT_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_IMPORT_ROWS = 50_000
MAX_IMPORT_WORKSHEETS = 10
MAX_IMPORT_ARCHIVE_FILES = 5_000
MAX_IMPORT_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
DEPLOYMENT_VERSION = os.environ.get("DEPLOYMENT_VERSION", "development").strip() or "development"

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
    "Company Description",
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
