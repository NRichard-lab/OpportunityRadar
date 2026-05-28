from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "sample_companies.xlsx"
DEFAULT_OUTPUT = BASE_DIR / "output" / "financial_jobs_radar_enriched.xlsx"
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
    "{company} credit union official website",
    "{company} bank official website",
    "{company} careers",
    "{company} jobs",
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
]

OUTPUT_COLUMNS = [
    "Company Name",
    "City",
    "State",
    "Known Website",
    "Official Website",
    "Careers Page URL",
    "Jobs RSS Feed URL",
    "Job Platform",
    "Feed Found",
    "Search Status",
    "Confidence",
    "Last Checked",
    "Notes",
]
