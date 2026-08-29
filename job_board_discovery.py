from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from openpyxl import Workbook
from openpyxl.styles import Font

from backend.file_security import atomic_save_workbook, atomic_write_text, sanitize_spreadsheet_value
from config import LOG_DIR, OUTPUT_DIR, POLITE_DELAY_SECONDS, REQUEST_TIMEOUT
from job_platforms import detect_job_platform
from search_tools import (
    company_identity_tokens,
    domain_matches_company,
    registered_domain,
)
from website_tools import extract_embedded_urls, extract_links, fetch_html, is_same_registered_domain, make_session, normalize_url


LOGGER = logging.getLogger(__name__)

HIGH_PRIORITY_TERMS = [
    "jobs",
    "careers",
    "employment",
    "openings",
    "open positions",
    "current openings",
    "search jobs",
    "search and apply",
    "apply now",
    "job opportunities",
    "join our team",
    "work with us",
    "working at",
    "recruiting",
    "recruitment",
    "talent community",
]

STRONG_LABEL_TERMS = [
    "search jobs",
    "current openings",
    "open positions",
    "careers",
    "apply now",
    "join our team",
]

REJECT_TERMS = [
    "loan",
    "loan-application",
    "loan application",
    "mortgage application",
    "membership application",
    "member application",
    "account application",
    "online banking",
    "credit card application",
    "/reviews",
    "reviews?",
    "/salaries",
    "/interviews",
    "login",
    "log-in",
    "signin",
    "sign-in",
    "pdf",
]

REJECT_DOMAINS = [
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "monster.com",
    "careerbuilder.com",
    "simplyhired.com",
    "yelp.com",
    "indeed.com",
]

JOB_INDICATORS = [
    "job title",
    "department",
    "location",
    "apply for this job",
    "submit application",
    "view all jobs",
    "job openings",
    "current openings",
    "search jobs",
]


@dataclass
class JobBoardCandidate:
    url: str
    text: str = ""
    source_url: str = ""
    source_method: str = ""
    score: int = 0
    platform: str = ""
    rejected: bool = False
    rejection_reason: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class JobBoardDiscoveryResult:
    company_name: str
    official_website: str = ""
    careers_page_url: str = ""
    existing_job_board_url: str = ""
    candidate_urls_found: list[str] = field(default_factory=list)
    candidate_scores: list[dict[str, object]] = field(default_factory=list)
    candidate_selected: str = ""
    discovery_method: str = "Not Found"
    status: str = "Needs Review"
    reason: str = ""
    notes: str = ""
    platform: str = ""
    found: bool = False
    likely_incorrect_existing: bool = False


def discover_job_board_for_row(
    row: dict[str, object],
    *,
    use_browser_discovery: bool = False,
    max_pages: int = 5,
    force: bool = False,
    debug: bool = False,
) -> JobBoardDiscoveryResult:
    session = make_session()
    company_name = str(row.get("Company Name") or "").strip()
    official_website = str(row.get("Official Website") or row.get("Known Website") or "").strip()
    careers_page_url = str(row.get("Careers Page URL") or "").strip()
    existing_job_board_url = str(row.get("Job Board URL") or "").strip()
    result = JobBoardDiscoveryResult(
        company_name=company_name,
        official_website=official_website,
        careers_page_url=careers_page_url,
        existing_job_board_url=existing_job_board_url,
    )

    if existing_job_board_url:
        existing_eval = evaluate_candidate(
            company_name,
            existing_job_board_url,
            text="Existing Job Board URL",
            source_url=careers_page_url or official_website,
            source_method="Existing",
            reached_from_official=True,
        )
        append_candidate(result, existing_eval)
        if existing_eval.rejected:
            result.likely_incorrect_existing = True
            result.status = "Likely Incorrect"
            result.reason = f"Existing Job Board URL rejected: {existing_eval.rejection_reason}"
            result.notes = "Existing Job Board URL was not overwritten because --force is required."
            if not force:
                return result
        elif not force:
            result.status = "Skipped"
            result.reason = "Existing Job Board URL present."
            result.notes = "Use --force to replace existing values."
            return result

    start_url = careers_page_url or official_website
    if not start_url:
        result.status = "Needs Review"
        result.reason = "No Careers Page URL, Official Website, or Known Website available."
        return result

    candidates = static_scan(start_url, company_name, session, "Static Link")
    best = best_acceptable_candidate(candidates)
    for candidate in candidates:
        append_candidate(result, candidate)
    if best and best.platform:
        return select_candidate(result, best, "Static Link", "Found known job platform link in static HTML.")

    if official_website:
        expanded = same_domain_careers_expansion(
            official_website,
            company_name,
            session,
            max_pages=max_pages,
            seed_url=careers_page_url,
        )
        for candidate in expanded:
            append_candidate(result, candidate)
        best = best_acceptable_candidate(expanded)
        if best:
            return select_candidate(result, best, best.source_method, "Found job board link while scanning internal careers pages.")

    if use_browser_discovery:
        browser_candidate = browser_discovery(start_url, company_name)
        if browser_candidate:
            append_candidate(result, browser_candidate)
            if not browser_candidate.rejected:
                if browser_candidate.platform:
                    return select_candidate(result, browser_candidate, "Browser Click", "Reached known job platform after clicking public careers/jobs link.")
                nested = static_scan(browser_candidate.url, company_name, session, "Browser Follow-up Scan")
                for candidate in nested:
                    append_candidate(result, candidate)
                best = best_acceptable_candidate(nested)
                if best:
                    return select_candidate(result, best, best.source_method, "Found job platform after browser click follow-up scan.")
                if browser_candidate.score >= 85:
                    return select_candidate(result, browser_candidate, "Browser Click", "Reached strong public careers/jobs page after clicking.")

    search_candidates = verified_search_fallback(company_name, official_website, session)
    for candidate in search_candidates:
        append_candidate(result, candidate)
    best = best_acceptable_candidate(search_candidates)
    if best and best.score >= 90:
        return select_candidate(result, best, "Verified Search", "Verified search result as related public job board.")

    result.status = "Needs Review"
    result.reason = "No high-confidence public job board URL found."
    result.notes = f"Rejected {len([c for c in result.candidate_scores if c.get('rejected')])} candidate(s)." if debug else ""
    return result


def static_scan(
    page_url: str,
    company_name: str,
    session: requests.Session,
    source_method: str,
) -> list[JobBoardCandidate]:
    candidates: list[JobBoardCandidate] = []
    try:
        final_url, html = fetch_html(page_url, session)
    except Exception as exc:
        return [
            JobBoardCandidate(
                url=page_url,
                source_url=page_url,
                source_method=source_method,
                rejected=True,
                rejection_reason=f"could not fetch page: {exc}",
            )
        ]

    for text, href in extract_links(final_url, html):
        if not could_be_job_link(text, href):
            continue
        candidate = evaluate_candidate(
            company_name,
            href,
            text=text,
            source_url=final_url,
            source_method=source_method,
            reached_from_official=True,
        )
        candidates.append(candidate)
    for embedded_url in extract_embedded_urls(final_url, html):
        if not detect_job_platform(embedded_url):
            continue
        candidates.append(evaluate_candidate(
            company_name,
            embedded_url,
            text="Embedded job board",
            source_url=final_url,
            source_method=source_method,
            reached_from_official=True,
        ))
    return candidates


def same_domain_careers_expansion(
    official_website: str,
    company_name: str,
    session: requests.Session,
    *,
    max_pages: int,
    seed_url: str = "",
) -> list[JobBoardCandidate]:
    candidates: list[JobBoardCandidate] = []
    pages = find_internal_candidate_pages(official_website, company_name, session, max_pages=max_pages, seed_url=seed_url)
    for page_url in pages:
        candidates.extend(static_scan(page_url, company_name, session, "Internal Careers Scan"))
    return candidates


def find_internal_candidate_pages(
    official_website: str,
    company_name: str,
    session: requests.Session,
    *,
    max_pages: int,
    seed_url: str = "",
) -> list[str]:
    pages: list[str] = []
    seen: set[str] = set()
    if seed_url and is_same_registered_domain(official_website, seed_url):
        pages.append(seed_url)
        seen.add(seed_url.rstrip("/"))
    try:
        final_url, html = fetch_html(official_website, session)
    except Exception as exc:
        LOGGER.info("Could not expand careers pages for %s: %s", company_name, exc)
        return pages

    scored: list[tuple[int, str]] = []
    for text, href in extract_links(final_url, html):
        clean = href.rstrip("/")
        if clean in seen or not is_same_registered_domain(official_website, href):
            continue
        score = score_link_text(text, href)
        if score <= 0:
            continue
        scored.append((score, href))
        seen.add(clean)

    for _, href in sorted(scored, key=lambda item: item[0], reverse=True):
        if len(pages) >= max_pages:
            break
        pages.append(href)
    return pages


def browser_discovery(start_url: str, company_name: str) -> JobBoardCandidate | None:
    try:
        from browser_tools import discover_job_board_with_browser

        discovery = discover_job_board_with_browser(start_url, company_name)
    except Exception as exc:
        return JobBoardCandidate(
            url=start_url,
            source_url=start_url,
            source_method="Browser Click",
            rejected=True,
            rejection_reason=f"browser discovery failed: {exc}",
        )
    final_url = str(discovery.get("final_url") or "")
    if not final_url:
        return None
    return evaluate_candidate(
        company_name,
        final_url,
        text=str(discovery.get("clicked_text") or "Browser click"),
        source_url=start_url,
        source_method="Browser Click",
        reached_from_official=True,
        clicked=True,
    )


def verified_search_fallback(
    company_name: str,
    official_website: str,
    session: requests.Session,
) -> list[JobBoardCandidate]:
    try:
        from ddgs import DDGS
    except ImportError:
        return []

    phrases = [
        f'"{company_name}" careers',
        f'"{company_name}" jobs',
        f'"{company_name}" "open positions"',
        f'"{company_name}" "Workday"',
        f'"{company_name}" "ADP"',
        f'"{company_name}" "Paylocity"',
        f'"{company_name}" "ICIMS"',
    ]
    candidates: list[JobBoardCandidate] = []
    seen: set[str] = set()
    for phrase in phrases:
        try:
            with DDGS(timeout=min(REQUEST_TIMEOUT, 5)) as ddgs:
                results = list(ddgs.text(phrase, max_results=3))
        except Exception as exc:
            LOGGER.info("Job board search fallback failed for %s: %s", phrase, exc)
            continue
        for result in results:
            url = result.get("href") or result.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            text = " ".join(part for part in [result.get("title", ""), result.get("body", "")] if part)
            candidate = evaluate_candidate(
                company_name,
                url,
                text=text,
                source_url=official_website,
                source_method="Verified Search",
                reached_from_official=False,
            )
            if verify_search_candidate(candidate, company_name, official_website, session):
                candidates.append(candidate)
    return candidates


def verify_search_candidate(
    candidate: JobBoardCandidate,
    company_name: str,
    official_website: str,
    session: requests.Session,
) -> bool:
    if candidate.rejected:
        return False
    if candidate.platform and company_related_to_url(company_name, candidate.url):
        candidate.score += 18
        candidate.notes.append("search result URL includes company identity")
        return True
    if official_website and is_same_registered_domain(official_website, candidate.url):
        candidate.score += 20
        candidate.notes.append("search result is on official domain")
        return True
    try:
        final_url, html = fetch_html(candidate.url, session)
    except Exception as exc:
        candidate.rejected = True
        candidate.rejection_reason = f"could not verify search result page: {exc}"
        return False
    text = page_text(html)
    if any(token in text for token in company_identity_tokens(company_name)) and any(term in text for term in JOB_INDICATORS):
        candidate.url = final_url
        candidate.score += 20
        candidate.notes.append("verified company terms and job listing indicators on page")
        return True
    candidate.rejected = True
    candidate.rejection_reason = "search result was not verified as related to the company"
    return False


def evaluate_candidate(
    company_name: str,
    url: str,
    *,
    text: str = "",
    source_url: str = "",
    source_method: str = "",
    reached_from_official: bool = False,
    clicked: bool = False,
) -> JobBoardCandidate:
    normalized = normalize_url(url)
    candidate = JobBoardCandidate(url=normalized, text=clean_text(text), source_url=source_url, source_method=source_method)
    rejection = rejection_reason(normalized)
    if rejection:
        candidate.rejected = True
        candidate.rejection_reason = rejection
        candidate.score = -100
        return candidate

    candidate.platform = detect_job_platform(normalized)
    candidate.score += score_link_text(text, normalized)
    if candidate.platform:
        candidate.score += 70
        candidate.notes.append(f"known job platform: {candidate.platform}")
    if company_related_to_url(company_name, normalized):
        candidate.score += 20
        candidate.notes.append("URL appears related to company identity")
    if reached_from_official:
        candidate.score += 18
        candidate.notes.append("candidate found from verified company page")
    if clicked:
        candidate.score += 14
        candidate.notes.append("candidate reached by browser click")
    if strong_careers_url(normalized):
        candidate.score += 18
        candidate.notes.append("URL contains careers/jobs signals")
    if candidate.score < 35 and not candidate.platform:
        candidate.rejected = True
        candidate.rejection_reason = "weak careers/jobs signal"
    return candidate


def score_link_text(text: str, href: str) -> int:
    haystack = f"{text} {href}".lower()
    score = 0
    for term in HIGH_PRIORITY_TERMS:
        if term in haystack:
            score += 10
    for term in STRONG_LABEL_TERMS:
        if term in text.lower():
            score += 18
    if re.search(r"/(jobs|careers|employment|openings)(/|$|-)", href.lower()):
        score += 14
    if len(clean_text(text)) > 120:
        score -= 15
    return score


def could_be_job_link(text: str, href: str) -> bool:
    haystack = f"{text} {href}".lower()
    return bool(detect_job_platform(href)) or any(term in haystack for term in HIGH_PRIORITY_TERMS)


def best_acceptable_candidate(candidates: list[JobBoardCandidate]) -> JobBoardCandidate | None:
    accepted = [candidate for candidate in candidates if not candidate.rejected]
    if not accepted:
        return None
    best = sorted(accepted, key=lambda candidate: candidate.score, reverse=True)[0]
    return best if best.score >= 55 or best.platform else None


def select_candidate(
    result: JobBoardDiscoveryResult,
    candidate: JobBoardCandidate,
    method: str,
    notes: str,
) -> JobBoardDiscoveryResult:
    result.candidate_selected = candidate.url
    result.discovery_method = method
    result.status = "Found"
    result.reason = "Selected highest-confidence public job board URL."
    result.notes = notes
    result.platform = candidate.platform
    result.found = True
    return result


def append_candidate(result: JobBoardDiscoveryResult, candidate: JobBoardCandidate) -> None:
    if candidate.url and candidate.url not in result.candidate_urls_found:
        result.candidate_urls_found.append(candidate.url)
    row = asdict(candidate)
    row["notes"] = "; ".join(candidate.notes)
    result.candidate_scores.append(row)


def rejection_reason(url: str) -> str:
    lower = url.lower()
    parsed = urlparse(url)
    domain = registered_domain(url)
    if not parsed.scheme.startswith("http"):
        return "non-public URL scheme"
    if lower.endswith((".pdf", ".doc", ".docx")):
        return "document URL"
    if any(domain == blocked or domain.endswith(f".{blocked}") for blocked in REJECT_DOMAINS):
        return "social media, directory, or generic third-party jobs site"
    for term in REJECT_TERMS:
        if term in lower:
            return f"non-job application or login URL contains '{term}'"
    if "benefits" in lower and not any(term in lower for term in ["job", "career", "opening"]):
        return "benefits/culture page without job signal"
    return ""


def strong_careers_url(url: str) -> bool:
    lower = url.lower()
    return any(term.replace(" ", "-") in lower or term in lower for term in HIGH_PRIORITY_TERMS)


def company_related_to_url(company_name: str, url: str) -> bool:
    if domain_matches_company(company_name, url):
        return True
    lower = re.sub(r"[^a-z0-9]+", "", url.lower())
    tokens = [token for token in company_identity_tokens(company_name) if token not in {"all", "one", "first", "bank"}]
    return any(len(token) >= 4 and token in lower for token in tokens)


def page_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(" ", strip=True).lower()


def clean_text(value: str) -> str:
    return " ".join(str(value or "").split())


def write_job_board_audit(results: list[JobBoardDiscoveryResult]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = LOG_DIR / "job_board_discovery_audit.json"
    xlsx_path = OUTPUT_DIR / "job_board_discovery_audit.xlsx"
    atomic_write_text(json_path, json.dumps([asdict(result) for result in results], indent=2))

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Job Board Discovery Audit"
    headers = [
        "Company Name",
        "Official Website",
        "Careers Page URL",
        "Existing Job Board URL",
        "Candidate URLs Found",
        "Candidate Scores",
        "Candidate Selected",
        "Discovery Method",
        "Status",
        "Reason",
        "Notes",
    ]
    sheet.append(headers)
    for result in results:
        sheet.append(
            [sanitize_spreadsheet_value(value) for value in [
                result.company_name,
                result.official_website,
                result.careers_page_url,
                result.existing_job_board_url,
                "\n".join(result.candidate_urls_found),
                json.dumps(result.candidate_scores, ensure_ascii=True),
                result.candidate_selected,
                result.discovery_method,
                result.status,
                result.reason,
                result.notes,
            ]]
        )
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    widths = [28, 42, 42, 42, 55, 70, 42, 22, 18, 42, 55]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = width
    atomic_save_workbook(xlsx_path, workbook)
    LOGGER.info("Wrote job board discovery audit JSON: %s", json_path)
    LOGGER.info("Wrote job board discovery audit workbook: %s", xlsx_path)
