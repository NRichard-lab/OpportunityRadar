from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_INPUT = BASE_DIR / "sample_companies.xlsx"
DEFAULT_MASTER = DATA_DIR / "master.xlsx"
DEFAULT_JSON_OUTPUT = DATA_DIR / "companies.json"
DEFAULT_FRONTEND_COMPANIES_JSON = BASE_DIR / "frontend" / "public" / "data" / "companies.json"
DEFAULT_JOBS_JSON = DATA_DIR / "jobs.json"
DEFAULT_FRONTEND_JOBS_JSON = BASE_DIR / "frontend" / "public" / "data" / "jobs.json"
DEFAULT_APPLICATIONS_JSON = DATA_DIR / "applications.json"
DEFAULT_JOBS_XLSX = BASE_DIR / "output" / "jobs_snapshot.xlsx"
LOG_DIR = BASE_DIR / "logs"
OUTPUT_DIR = BASE_DIR / "output"

USER_AGENT = (
    "FinancialJobsRadar/1.0 "
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
    "City",
    "State",
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
]
