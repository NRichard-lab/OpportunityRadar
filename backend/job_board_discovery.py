from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import REQUEST_TIMEOUT, USER_AGENT
from job_platforms import detect_job_platform


CAREERS_TERMS = ("careers", "career", "jobs", "employment", "join our team", "work with us")
JOB_LINK_TERMS = (
    "apply",
    "search jobs",
    "search careers",
    "view jobs",
    "find jobs",
    "job openings",
    "current openings",
    "open positions",
    "career opportunities",
    "browse jobs",
    "proceed",
    "continue",
)


class DiscoveryError(ValueError):
    def __init__(self, message: str, status: str = "Failed", careers_page_url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.careers_page_url = careers_page_url


@dataclass
class DiscoveryResult:
    company_website: str
    careers_page_url: str
    job_board_url: str
    platform: str
    job_board_type: str
    classification_confidence: str
    discovery_method: str


def _structured_organizations(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    organizations: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            value = json.loads(script.string or "")
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and "@graph" in item:
                values.extend(node for node in item["@graph"] if isinstance(node, dict))
            if isinstance(item, dict) and str(item.get("@type", "")).lower() in {
                "organization", "corporation", "financialservice", "bankorcreditunion", "localbusiness"
            }:
                organizations.append(item)
    return organizations


def _asset_value(amount: str, scale: str) -> float | None:
    try:
        value = float(amount.replace(",", ""))
    except ValueError:
        return None
    multiplier = {"million": 1_000_000, "billion": 1_000_000_000, "trillion": 1_000_000_000_000}.get(scale.lower(), 1)
    return value * multiplier


def _internal_company_pages(company_url: str, html: str, session: requests.Session) -> list[tuple[str, str]]:
    terms = ("about", "contact", "leadership", "annual report", "reports", "locations", "legal")
    source_host = urlparse(company_url).netloc.lower()
    ranked = sorted(
        (
            (score_link(text, url, terms), url)
            for text, url in links(company_url, html)
            if urlparse(url).netloc.lower() == source_host
        ),
        reverse=True,
    )
    pages: list[tuple[str, str]] = []
    seen: set[str] = set()
    for score, url in ranked:
        if score <= 0 or url in seen or len(pages) >= 8:
            continue
        seen.add(url)
        try:
            pages.append(fetch_page(session, url))
        except Exception:
            continue
    return pages


def _headquarters_location(pages: list[tuple[str, str]]) -> dict[str, str]:
    explicit_pattern = re.compile(
        r"(?:headquarters|principal office|main office|corporate office|mailing address)"
        r".{0,240}?\b([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,3}),\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b",
        re.I | re.S,
    )
    possible: set[tuple[str, str]] = set()
    structured_candidates: list[tuple[str, str, str]] = []
    for page_url, html in pages:
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        explicit = explicit_pattern.search(text)
        if explicit:
            return {"city": explicit.group(1).strip(), "state": explicit.group(2).upper(), "location_discovery_source": page_url, "location_confidence": "Verified", "possible_locations": ""}
        for organization in _structured_organizations(html):
            address = organization.get("address") or {}
            if not isinstance(address, dict):
                continue
            city, state = str(address.get("addressLocality") or "").strip(), str(address.get("addressRegion") or "").strip().upper()
            if city and re.fullmatch(r"[A-Z]{2}", state):
                possible.add((city, state))
                org_type = str(organization.get("@type", "")).lower()
                if org_type != "localbusiness" and "/locations" not in page_url.lower():
                    structured_candidates.append((city, state, page_url))
        for match in re.finditer(r"\b([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,2}),\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b", text):
            possible.add((match.group(1).strip(), match.group(2).upper()))

    unique_structured = {(city, state) for city, state, _ in structured_candidates}
    if len(unique_structured) == 1:
        city, state = next(iter(unique_structured))
        source = next(url for candidate_city, candidate_state, url in structured_candidates if (candidate_city, candidate_state) == (city, state))
        return {"city": city, "state": state, "location_discovery_source": source, "location_confidence": "Verified", "possible_locations": ""}
    possible_text = "; ".join(f"{city}, {state}" for city, state in sorted(possible))
    return {"city": "", "state": "", "location_discovery_source": "", "location_confidence": "Needs Review" if possible else "Not Found", "possible_locations": possible_text}


def gather_public_company_information(company_website: str, careers_page_url: str = "") -> dict[str, object]:
    if not company_website.strip() and not careers_page_url.strip():
        raise DiscoveryError("Enter a Company Website or Careers Page URL first.")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    sources: dict[str, str] = {}
    result: dict[str, object] = {
        "company_website": normalize_url(company_website), "careers_page_url": normalize_url(careers_page_url),
        "verified_job_board_url": "", "city": "", "state": "", "founded_year": None,
        "total_assets": None, "total_assets_display": "", "assets_as_of_date": "",
        "industry": "", "discovery_status": "Failed", "discovery_method": "", "sources": sources,
        "location_discovery_source": "", "location_confidence": "Not Found", "possible_locations": "",
    }

    pages: list[tuple[str, str]] = []
    if company_website.strip():
        try:
            company_final, company_html = fetch_page(session, company_website)
            result["company_website"] = company_final
            pages.append((company_final, company_html))
            pages.extend(_internal_company_pages(company_final, company_html, session))
            sources["company_website"] = company_final
        except Exception:
            # A slow homepage must not hide a reachable, user-supplied public
            # careers page or its verified ATS destination.
            pass

    try:
        board = discover_job_board(company_website, careers_page_url)
        result.update({
            "company_website": board.company_website,
            "careers_page_url": board.careers_page_url,
            "verified_job_board_url": board.job_board_url,
            "platform": board.platform,
            "job_board_type": board.job_board_type,
            "classification_confidence": board.classification_confidence,
            "discovery_method": board.discovery_method,
        })
        result["discovery_status"] = "Verified"
        sources.update({"careers_page_url": board.careers_page_url, "verified_job_board_url": board.job_board_url})
    except DiscoveryError as exc:
        result["discovery_status"] = exc.status
        if exc.careers_page_url:
            result["careers_page_url"] = exc.careers_page_url
            sources["careers_page_url"] = exc.careers_page_url

    careers_value = str(result.get("careers_page_url") or "")
    if careers_value and all(url != careers_value for url, _ in pages):
        try:
            careers_final, careers_html = fetch_page(session, careers_value)
            pages.append((careers_final, careers_html))
            result["careers_page_url"] = careers_final
        except Exception:
            pass

    for page_url, html in pages:
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        for organization in _structured_organizations(html):
            founding = str(organization.get("foundingDate") or "")
            match = re.search(r"\b(1[6-9]\d{2}|20\d{2})\b", founding)
            if match and not result["founded_year"]:
                result["founded_year"] = int(match.group(1)); sources["founded_year"] = page_url

        if not result["founded_year"]:
            match = re.search(r"\b(?:founded|established|since)\s+(?:in\s+)?(1[6-9]\d{2}|20\d{2})\b", text, re.I)
            if match:
                result["founded_year"] = int(match.group(1)); sources["founded_year"] = page_url

        if not result["total_assets"] and re.search(r"\b(bank|credit union|financial institution)\b", text, re.I):
            match = re.search(r"\btotal assets(?:\s+of|\s+were|\s+are|\s*:)?\s*\$\s*([\d,.]+)\s*(million|billion|trillion)?", text, re.I)
            if match:
                value = _asset_value(match.group(1), match.group(2) or "")
                if value is not None:
                    display = f"${match.group(1)}{(' ' + match.group(2).lower()) if match.group(2) else ''}"
                    result["total_assets"] = value; result["total_assets_display"] = display; sources["total_assets"] = page_url
                    nearby = text[match.end():match.end() + 140]
                    date_match = re.search(r"(?:as of|at)\s+([A-Z][a-z]+\s+\d{1,2},\s+20\d{2}|[A-Z][a-z]+\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2})", nearby)
                    if date_match:
                        result["assets_as_of_date"] = date_match.group(1); sources["assets_as_of_date"] = page_url

        if not result["industry"]:
            if re.search(r"\bcredit union\b", text, re.I): result["industry"] = "Financial Services"; sources["industry"] = page_url
            elif re.search(r"\b(bank|financial institution)\b", text, re.I): result["industry"] = "Financial Services"; sources["industry"] = page_url

    location = _headquarters_location(pages)
    result.update(location)
    if location["location_discovery_source"]:
        sources["city"] = location["location_discovery_source"]
        sources["state"] = location["location_discovery_source"]

    result["information_source_note"] = "; ".join(f"{field}: {url}" for field, url in sources.items())
    return result


def normalize_url(url: str) -> str:
    value = url.strip()
    if not value:
        return ""
    return value if "://" in value else f"https://{value}"


def public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def fetch_page(session: requests.Session, url: str) -> tuple[str, str]:
    normalized = normalize_url(url)
    if not public_http_url(normalized):
        raise DiscoveryError("Enter a valid public HTTP or HTTPS URL.")
    try:
        response = session.get(normalized, timeout=max(REQUEST_TIMEOUT, 15), allow_redirects=True)
    except (requests.ConnectionError, requests.Timeout) as request_error:
        try:
            from playwright.sync_api import sync_playwright

            edge_path = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
            with sync_playwright() as playwright:
                options: dict[str, object] = {"headless": True, "args": ["--disable-http2"]}
                if edge_path.exists():
                    options["executable_path"] = str(edge_path)
                browser = playwright.chromium.launch(**options)
                try:
                    page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36")
                    page.goto(normalized, wait_until="commit", timeout=30_000)
                    page.wait_for_timeout(3_000)
                    return page.url, page.content()
                finally:
                    browser.close()
        except Exception:
            raise request_error
    response.raise_for_status()
    if not public_http_url(response.url):
        raise DiscoveryError("The public link redirected to an unsupported destination.")
    return response.url, response.text


def links(base_url: str, html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        target = urldefrag(urljoin(base_url, href))[0]
        if public_http_url(target):
            found.append((anchor.get_text(" ", strip=True), target))
    # External-site warning modals commonly keep their public destination in a
    # data attribute while the visible trigger points only to a local fragment.
    for element in soup.find_all(["a", "button"]):
        text = element.get_text(" ", strip=True)
        for attribute in ("data-url", "data-href", "data-link", "data-destination", "data-redirect-url", "data-external-url"):
            raw_target = str(element.get(attribute) or "").strip()
            target = urldefrag(urljoin(base_url, raw_target))[0]
            if raw_target and public_http_url(target):
                found.append((text, target))
    return found


def score_link(text: str, url: str, terms: tuple[str, ...]) -> int:
    text_value = text.lower()
    url_value = url.lower()
    return sum(4 for term in terms if term in text_value) + sum(1 for term in terms if term in url_value)


def best_link(page_links: list[tuple[str, str]], terms: tuple[str, ...]) -> tuple[str, str] | None:
    ranked = sorted(
        ((score_link(text, url, terms), text, url) for text, url in page_links),
        reverse=True,
    )
    if not ranked or ranked[0][0] <= 0:
        return None
    _, text, url = ranked[0]
    return text, url


def _same_domain(left: str, right: str) -> bool:
    normalize = lambda value: urlparse(value).netloc.lower().removeprefix("www.")
    return normalize(left) == normalize(right)


def _has_jobposting_jsonld(html: str) -> bool:
    return bool(re.search(r'"@type"\s*:\s*"(?:[^" ]*,?\s*)*JobPosting', html, re.I))


def _self_hosted_listing_signals(page_url: str, html: str) -> tuple[bool, int]:
    """Require real listings, not merely recruiting prose or a generic Apply link."""
    soup = BeautifulSoup(html, "html.parser")
    if _has_jobposting_jsonld(html):
        return True, 3
    job_links: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        href = urldefrag(urljoin(page_url, str(anchor["href"])))[0]
        path = urlparse(href).path.lower()
        if text.lower() in {"apply", "apply now", "view jobs", "search jobs"}:
            continue
        looks_like_detail = bool(
            re.search(r"/(?:jobs?|positions?|openings?)/[^/]+", path, re.I)
            or re.search(r"[?&](?:job|jobid|postingid|requisitionid)=", href, re.I)
        )
        if len(text) >= 4 and looks_like_detail and href.rstrip("/") != page_url.rstrip("/"):
            job_links.add(href)
    text = soup.get_text(" ", strip=True).lower()
    has_listing_label = any(term in text for term in ("current openings", "open positions", "career opportunities", "view job"))
    return len(job_links) >= 3 and has_listing_label, len(job_links)


def discover_job_board(company_website: str, careers_page_url: str = "") -> DiscoveryResult:
    if not company_website.strip() and not careers_page_url.strip():
        raise DiscoveryError("Enter a Company Website or Careers Page URL first.")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    company_final = normalize_url(company_website)

    if careers_page_url.strip():
        careers_final, careers_html = fetch_page(session, careers_page_url)
    else:
        company_final, company_html = fetch_page(session, company_website)
        career_link = best_link(links(company_final, company_html), CAREERS_TERMS)
        if not career_link:
            raise DiscoveryError("No public Careers link was found on the company website. Enter the Careers Page URL and try again.")
        _, career_url = career_link
        careers_final, careers_html = fetch_page(session, career_url)

    # Classification happens before following any external job-board link. A
    # company-domain page with an actual public list is itself the board source.
    is_self_hosted, listing_count = _self_hosted_listing_signals(careers_final, careers_html)
    if is_self_hosted and (not company_final or _same_domain(company_final, careers_final)):
        return DiscoveryResult(
            company_website=company_final, careers_page_url=careers_final,
            job_board_url=careers_final, platform="Self-Hosted / In-House",
            job_board_type="Self-Hosted / In-House", classification_confidence="High",
            discovery_method=f"Classified Careers Page → Self-Hosted / In-House ({listing_count} public job links)",
        )

    page_links = links(careers_final, careers_html)
    ats_links = [(text, url) for text, url in page_links if detect_job_platform(url)]
    candidate = best_link(ats_links, JOB_LINK_TERMS) if ats_links else None
    if not candidate and ats_links:
        candidate = ats_links[0]
    if not candidate:
        candidate = best_link(page_links, JOB_LINK_TERMS)
    if not candidate:
        raise DiscoveryError(
            "No public Apply, Search Jobs, or equivalent link was found on the Careers Page.",
            "Needs Review",
            careers_final,
        )

    link_text, candidate_url = candidate
    board_final, board_html = fetch_page(session, candidate_url)
    platform = detect_job_platform(board_final)

    # Some careers links pass through one public recruiting landing page before the ATS.
    if not platform:
        second_links = links(board_final, board_html)
        ats_links = [(text, url) for text, url in second_links if detect_job_platform(url)]
        second = best_link(ats_links, JOB_LINK_TERMS) if ats_links else None
        if not second and ats_links:
            second = ats_links[0]
        if second:
            link_text, second_url = second
            board_final, _ = fetch_page(session, second_url)
            platform = detect_job_platform(board_final)

    if not platform:
        if public_http_url(board_final):
            return DiscoveryResult(
                company_website=company_final, careers_page_url=careers_final,
                job_board_url=board_final, platform="Other External ATS",
                job_board_type="Other External ATS", classification_confidence="Medium",
                discovery_method=f"Followed Careers → {link_text.strip() or 'Jobs'} link; external ATS needs review",
            )
        raise DiscoveryError(
            "The public careers link did not lead to a recognized external job-board landing page.",
            "Needs Review",
            careers_final,
        )

    label = link_text.strip() or "Jobs"
    return DiscoveryResult(
        company_website=company_final,
        careers_page_url=careers_final,
        job_board_url=board_final,
        platform=platform,
        job_board_type=platform if platform in {"Workday", "ADP", "Greenhouse", "Lever", "ICIMS", "Paylocity", "UKG", "SaaS HR", "Dayforce"} else "Other External ATS",
        classification_confidence="High",
        discovery_method=f"Followed Careers → {label} link",
    )
