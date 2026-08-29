from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urlparse, urlunparse

import requests
import tldextract
from bs4 import BeautifulSoup

from config import DISALLOWED_RESULT_DOMAINS, OFFICIAL_WEBSITE_SEARCH_PHRASES, REQUEST_TIMEOUT
from job_platforms import detect_job_platform, hostname_matches_domain


LOGGER = logging.getLogger(__name__)

MAX_HTTP_ATTEMPTS = 2
MAX_OFFICIAL_SEARCH_QUERIES = 3
MAX_OFFICIAL_RESULTS_PER_QUERY = 4
MAX_OFFICIAL_SEARCH_RESULTS = 8
RETRY_BACKOFF_SECONDS = 0.15

STATE_NAMES = {
    "CO": "colorado",
    "WA": "washington",
}


@dataclass
class SearchCandidate:
    url: str
    title: str = ""
    snippet: str = ""
    rank: int = 0


@dataclass
class WebsiteEvaluation:
    url: str
    final_url: str = ""
    confidence: str = "Low"
    score: int = 0
    verified: bool = False
    notes: list[str] = field(default_factory=list)
    rejected: bool = False
    rejection_reason: str = ""
    discovery_method: str = "Not Found"
    candidate_urls: list[str] = field(default_factory=list)
    html: str = field(default="", repr=False)


def request_with_limited_retries(
    session: requests.Session,
    url: str,
    *,
    timeout: float = REQUEST_TIMEOUT,
    max_attempts: int = MAX_HTTP_ATTEMPTS,
    backoff_seconds: float = RETRY_BACKOFF_SECONDS,
    **kwargs: object,
) -> requests.Response:
    """GET a public URL with a small retry budget for transient failures only."""
    attempts = max(1, min(int(max_attempts), 3))
    last_error: requests.RequestException | None = None
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=timeout, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                raise
        else:
            status = int(getattr(response, "status_code", 0) or 0)
            retryable = status == 429 or 500 <= status < 600
            if not retryable or attempt + 1 >= attempts:
                return response
            response.close()
        if backoff_seconds > 0:
            time.sleep(backoff_seconds * (attempt + 1))
    if last_error is not None:
        raise last_error
    raise RuntimeError("HTTP retry loop ended without a response")


def canonicalize_http_url(url: str) -> str:
    """Normalize a public HTTP(S) URL while preserving its path and query."""
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("blank URL")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if scheme not in {"http", "https"} or not hostname:
        raise ValueError("URL must use HTTP or HTTPS and include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL contains an invalid port") from exc
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if scheme == "http" else 443
    netloc = display_host if port in {None, default_port} else f"{display_host}:{port}"
    path = parsed.path or "/"
    canonical = urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))
    return urldefrag(canonical)[0]


def validate_and_canonicalize_url(
    url: str,
    session: requests.Session,
    *,
    require_html: bool = False,
    reject_disallowed: bool = True,
    max_attempts: int = MAX_HTTP_ATTEMPTS,
) -> tuple[str, requests.Response]:
    """Confirm a URL responds and return its final canonical redirect target."""
    canonical = canonicalize_http_url(url)
    if reject_disallowed and is_disallowed_result(canonical):
        raise ValueError("URL points to a disallowed third-party or aggregator domain")
    response = request_with_limited_retries(
        session,
        canonical,
        timeout=REQUEST_TIMEOUT,
        max_attempts=max_attempts,
        allow_redirects=True,
    )
    try:
        response.raise_for_status()
    except requests.RequestException:
        response.close()
        raise
    final_url = canonicalize_http_url(str(response.url or canonical))
    if reject_disallowed and is_disallowed_result(final_url):
        response.close()
        raise ValueError("URL redirected to a disallowed third-party or aggregator domain")
    if require_html:
        content_type = str(response.headers.get("content-type", "")).lower()
        if "html" not in content_type:
            response.close()
            raise ValueError(f"non-HTML content type: {content_type or 'unknown'}")
    return final_url, response


def normalize_company_name(name: str) -> str:
    cleaned = name.lower()
    cleaned = re.sub(r"\b(credit union|federal credit union|bank|national association|n\.a\.|na)\b", "", cleaned)
    return re.sub(r"[^a-z0-9]+", "", cleaned)


def company_identity_tokens(name: str) -> list[str]:
    ignored = {"credit", "union", "federal", "bank", "national", "association", "the", "and"}
    return [
        word
        for word in re.findall(r"[a-z0-9]+", name.lower())
        if word not in ignored and len(word) >= 3
    ]


def domain_matches_company(company: str, url: str) -> bool:
    domain_text = normalize_company_name(registered_domain(url).split(".")[0])
    normalized_company = normalize_company_name(company)
    if not domain_text or not normalized_company:
        return False
    if domain_text == normalized_company:
        return True
    return any(token == domain_text or token in domain_text for token in company_identity_tokens(company))


def registered_domain(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    extracted = tldextract.extract(hostname)
    if not extracted.domain or not extracted.suffix:
        return hostname
    return f"{extracted.domain}.{extracted.suffix}".lower()


def site_root(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return f"{parsed.scheme}://{parsed.netloc}/"


def resolve_site_root(url: str, session: requests.Session) -> str:
    try:
        final_url, response = validate_and_canonicalize_url(url, session, require_html=False)
        response.close()
        return site_root(final_url)
    except Exception:
        return site_root(url)


def is_disallowed_result(url: str) -> bool:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    lowered = url.lower()
    if lowered.endswith((".pdf", ".doc", ".docx")):
        return True
    if any(term in lowered for term in ["routing-number", "routingnumber", "reviews", "review", "directory", "branches"]):
        return True
    return any(hostname_matches_domain(hostname, blocked) for blocked in DISALLOWED_RESULT_DOMAINS)


def search_web(
    company: str,
    city: str = "",
    state: str = "",
    max_results: int = MAX_OFFICIAL_RESULTS_PER_QUERY,
    *,
    max_queries: int = MAX_OFFICIAL_SEARCH_QUERIES,
    max_total_results: int = MAX_OFFICIAL_SEARCH_RESULTS,
    cancelled: object | None = None,
) -> list[SearchCandidate]:
    try:
        from ddgs import DDGS
    except ImportError as exc:
        LOGGER.warning("ddgs is not installed: %s", exc)
        return []

    candidates: list[SearchCandidate] = []
    seen: set[str] = set()

    per_query_limit = max(1, min(int(max_results), MAX_OFFICIAL_RESULTS_PER_QUERY))
    query_limit = max(1, min(int(max_queries), MAX_OFFICIAL_SEARCH_QUERIES))
    total_limit = max(1, min(int(max_total_results), MAX_OFFICIAL_SEARCH_RESULTS))
    location = " ".join(part for part in [city, state] if part).strip()
    phrase_templates = ["\"{company}\" official website", *OFFICIAL_WEBSITE_SEARCH_PHRASES]
    if location:
        phrase_templates = [
            "\"{company}\" {location} official website",
            *phrase_templates,
        ]
    phrase_templates = list(dict.fromkeys(phrase_templates))[:query_limit]

    for phrase_template in phrase_templates:
        check_search_cancelled(cancelled)
        phrase = phrase_template.format(company=company, location=location)
        try:
            with DDGS(timeout=min(REQUEST_TIMEOUT, 5)) as ddgs:
                for result in ddgs.text(phrase, max_results=per_query_limit):
                    check_search_cancelled(cancelled)
                    url = result.get("href") or result.get("url") or ""
                    if not url or url in seen or is_disallowed_result(url):
                        continue
                    seen.add(url)
                    candidates.append(
                        SearchCandidate(
                            url=url,
                            title=result.get("title", ""),
                            snippet=result.get("body", ""),
                            rank=len(candidates) + 1,
                        )
                    )
                    if len(candidates) >= total_limit:
                        return candidates
        except Exception as exc:
            LOGGER.warning("Search failed for '%s': %s", phrase, exc)

    return candidates


def evaluate_official_website_details(
    company: str,
    url: str,
    session: requests.Session,
    city: str = "",
    state: str = "",
    require_location: bool = False,
) -> WebsiteEvaluation:
    """Score and verify a possible official website without guessing."""
    result = WebsiteEvaluation(url=url, discovery_method="Web Search")
    if not url:
        result.rejected = True
        result.rejection_reason = "blank URL"
        return result
    if is_disallowed_result(url):
        result.rejected = True
        result.rejection_reason = "third-party, directory, review, social, job board, or document URL"
        result.notes.append(result.rejection_reason)
        return result

    try:
        final_url, response = validate_and_canonicalize_url(
            url,
            session,
            require_html=True,
            reject_disallowed=True,
        )
        result.final_url = site_root(final_url)

        domain_related = domain_matches_company(company, final_url)
        if domain_related:
            result.score += 18
            result.notes.append("domain resembles company name")
        html = response.text
        response.close()
        result.html = html
        soup = BeautifulSoup(html, "html.parser")
        meta_description = ""
        meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        if meta:
            meta_description = str(meta.get("content") or "")
        h1_text = " ".join(node.get_text(" ", strip=True) for node in soup.find_all("h1")[:3])
        page_text = " ".join(
            value.strip()
            for value in [
                soup.title.string if soup.title else "",
                meta_description,
                h1_text,
                soup.get_text(" ", strip=True)[:9000],
            ]
            if value
        ).lower()
        company_words = company_identity_tokens(company)
        matched_words = sum(1 for word in company_words if word in page_text)
        finance_terms = ["bank", "banking", "credit union", "federal credit union", "checking", "savings", "loans", "mortgage", "routing number", "ncua", "fdic", "online banking"]
        finance_matches = sum(1 for term in finance_terms if term in page_text)
        official_links = sum(1 for term in ["online banking", "checking", "savings", "loans", "locations", "contact", "careers"] if term in page_text)

        if matched_words:
            result.score += 12 * matched_words
            result.notes.append("company name terms appear on homepage")
        if finance_matches:
            result.score += min(30, 6 * finance_matches)
            result.notes.append("financial institution terms appear on homepage")
        if official_links:
            result.score += min(18, 3 * official_links)
            result.notes.append("official banking site navigation appears on homepage")
        location_terms = [city.lower().strip(), STATE_NAMES.get(state.upper().strip(), "")]
        location_terms = [term for term in location_terms if term]
        location_matches = any(term in page_text for term in location_terms)
        if location_matches:
            result.score += 5
            result.notes.append("location context appears on homepage")

        if matched_words == 0:
            result.score -= 35
            result.notes.append("company name terms not found on homepage")
        if finance_matches == 0:
            result.score -= 30
            result.notes.append("banking/credit union terms not found on homepage")
        if detect_job_board_only(final_url, page_text):
            result.score -= 50
            result.notes.append("site appears to be only a job board or third-party recruiting page")
        if looks_parked_or_unrelated(page_text):
            result.score -= 50
            result.notes.append("site appears parked, empty, for sale, or unrelated")
        if require_location and location_terms and not location_matches:
            result.notes.append("location context was not verified")

        if result.score >= 55 and matched_words and finance_matches:
            result.confidence = "High"
            result.verified = True
        elif result.score >= 35 and (matched_words or domain_related) and finance_matches:
            result.confidence = "Medium"
            result.verified = True
        elif result.score < 10:
            result.confidence = "Low"
    except Exception as exc:
        result.final_url = site_root(url)
        result.rejected = True
        result.rejection_reason = f"could not verify homepage: {exc}"
        result.notes.append(result.rejection_reason)

    return result


def evaluate_official_website(
    company: str,
    url: str,
    session: requests.Session,
    city: str = "",
    state: str = "",
    require_location: bool = False,
) -> tuple[str, str]:
    result = evaluate_official_website_details(company, url, session, city, state, require_location)
    return result.confidence, "; ".join(result.notes)


def detect_job_board_only(url: str, page_text: str) -> bool:
    platform_terms = ["workday", "greenhouse", "lever", "icims", "paylocity", "paycom", "jobvite", "smartrecruiters", "recruiting"]
    domain = registered_domain(url)
    return any(term in domain for term in platform_terms) and not any(term in page_text for term in ["checking", "savings", "loans", "routing number"])


def looks_parked_or_unrelated(page_text: str) -> bool:
    parked_terms = ["domain is for sale", "buy this domain", "parked free", "related searches", "coming soon"]
    if len(page_text.strip()) < 120:
        return True
    return any(term in page_text for term in parked_terms)


def choose_official_website(
    company: str,
    known_website: str,
    session: requests.Session,
    city: str = "",
    state: str = "",
) -> tuple[str, str, str]:
    details = choose_official_website_details(company, known_website, session, city, state)
    return details.final_url if details.verified else "", details.confidence, "; ".join(details.notes)


def choose_official_website_details(
    company: str,
    known_website: str,
    session: requests.Session,
    city: str = "",
    state: str = "",
    allow_low_confidence: bool = False,
    cancelled: object | None = None,
) -> WebsiteEvaluation:
    score_by_confidence = {"Low": 1, "Medium": 2, "High": 3}
    check_search_cancelled(cancelled)
    known_result: WebsiteEvaluation | None = None
    if known_website:
        known_result = evaluate_official_website_details(company, known_website, session, city=city, state=state)
        check_search_cancelled(cancelled)
        known_result.discovery_method = "Known Website"
        known_result.notes.insert(
            0,
            "validated known website" if known_result.verified else "known website did not pass verification",
        )
        if known_result.verified:
            return known_result

    candidates = search_web(
        company,
        city=city,
        state=state,
        max_results=MAX_OFFICIAL_RESULTS_PER_QUERY,
        max_queries=2 if known_result else MAX_OFFICIAL_SEARCH_QUERIES,
        max_total_results=6 if known_result else MAX_OFFICIAL_SEARCH_RESULTS,
        cancelled=cancelled,
    )
    candidate_urls = [site_root(candidate.url) for candidate in candidates]
    LOGGER.info("Candidate URLs for %s: %s", company, candidate_urls)
    if not candidates:
        if known_result is not None:
            known_result.notes.append("limited web search found no verified replacement")
            return known_result
        return WebsiteEvaluation(
            url="",
            final_url="",
            confidence="Low",
            verified=False,
            discovery_method="Not Found",
            candidate_urls=[],
            notes=["no search candidates found"],
        )

    best = WebsiteEvaluation(url="", candidate_urls=candidate_urls, notes=["no verified candidate selected"])

    for candidate in candidates:
        check_search_cancelled(cancelled)
        candidate_url = site_root(candidate.url)
        evaluation = evaluate_official_website_details(
            company,
            candidate_url,
            session,
            city=city,
            state=state,
            require_location=True,
        )
        evaluation.discovery_method = "Web Search"
        evaluation.candidate_urls = candidate_urls
        if candidate.rank <= 5:
            evaluation.score += 8
            evaluation.notes.append("candidate came from top search results")
        if candidate.title or candidate.snippet:
            result_context = f"{candidate.title} {candidate.snippet}".lower()
            if any(token in result_context for token in company_identity_tokens(company)):
                evaluation.score += 8
                evaluation.notes.append("search result title/snippet references company")
        LOGGER.info(
            "Website candidate for %s: %s score=%s confidence=%s rejected=%s notes=%s",
            company,
            candidate_url,
            evaluation.score,
            evaluation.confidence,
            evaluation.rejected,
            "; ".join(evaluation.notes),
        )
        if evaluation.score >= 55 and evaluation.verified:
            evaluation.confidence = "High"
        elif evaluation.score >= 35 and (evaluation.verified or allow_low_confidence):
            evaluation.confidence = "Medium"
            evaluation.verified = True
        if (
            score_by_confidence[evaluation.confidence] > score_by_confidence[best.confidence]
            or evaluation.score > best.score
        ):
            best = evaluation
            if evaluation.confidence == "High" and evaluation.verified:
                break

    if best.verified and best.confidence in {"High", "Medium"}:
        best.notes.insert(
            0,
            "known website failed verification; selected verified replacement from web search"
            if known_result is not None
            else "selected verified official website from web search",
        )
        return best
    if allow_low_confidence and best.final_url:
        best.verified = True
        best.notes.insert(0, "selected low-confidence website because --allow-low-confidence was used")
        return best
    best.verified = False
    if known_result is not None:
        best.notes.append("known website also failed verification")
    best.notes.insert(0, f"possible website, needs review: {best.final_url or best.url}")
    return best


def check_search_cancelled(cancelled: object | None) -> None:
    if cancelled is not None and getattr(cancelled, "is_set", lambda: False)():
        raise InterruptedError("Cancelled by user.")
