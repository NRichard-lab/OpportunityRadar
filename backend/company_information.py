from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from threading import Event, Semaphore
from typing import Any
from urllib.parse import urldefrag, urlparse

import requests
from bs4 import BeautifulSoup

from config import APP_MAX_BROWSER_WORKERS, REQUEST_TIMEOUT
from job_board_discovery import (
    JOB_INDICATORS,
    company_related_to_url,
    discover_job_board_for_row,
    rejection_reason,
)
from job_platforms import detect_job_platform
from search_tools import (
    choose_official_website_details,
    company_identity_tokens,
    is_disallowed_result,
    registered_domain,
    request_with_limited_retries,
)
from website_tools import find_careers_page, is_same_registered_domain, make_session


FIELD_LABELS = {
    "officialWebsite": "Company website",
    "careersPageUrl": "Careers page",
    "jobBoardUrl": "Job-board URL",
    "jobPlatform": "Job-board platform",
    "city": "Headquarters city",
    "state": "Headquarters state",
    "companyDescription": "Company description",
    "industry": "Industry",
}
URL_FIELDS = {"officialWebsite", "careersPageUrl", "jobBoardUrl"}
PLACEHOLDER_VALUES = {"", "unknown", "n/a", "na", "none", "not available", "tbd", "-"}
AGGREGATOR_DOMAINS = {
    "careerbuilder.com",
    "glassdoor.com",
    "indeed.com",
    "linkedin.com",
    "monster.com",
    "simplyhired.com",
    "ziprecruiter.com",
}

_browser_semaphore = Semaphore(max(1, APP_MAX_BROWSER_WORKERS))


@dataclass
class CompanyInformationDiscovery:
    updates: dict[str, Any] = field(default_factory=dict)
    found_fields: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    attempted_network: bool = False
    failed: bool = False


def normalized_company_key(name: str) -> str:
    """Return the conservative identity used to avoid duplicate network work."""
    return " ".join(str(name or "").strip().casefold().split())


def has_public_http_shape(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw or any(character.isspace() for character in raw):
        return False
    parsed = urlparse(raw)
    hostname = str(parsed.hostname or "").strip().casefold().rstrip(".")
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or hostname == "localhost"
        or hostname.endswith((".localhost", ".local", ".internal"))
    ):
        return False
    try:
        return ipaddress.ip_address(hostname).is_global
    except ValueError:
        return True


def is_aggregator_url(value: Any) -> bool:
    if not has_public_http_shape(value):
        return False
    domain = registered_domain(str(value))
    return any(domain == blocked or domain.endswith(f".{blocked}") for blocked in AGGREGATOR_DOMAINS)


def field_is_clearly_invalid(company: dict[str, Any], field_name: str) -> bool:
    value = company.get(field_name)
    if field_name in URL_FIELDS:
        if not has_public_http_shape(value):
            return bool(str(value or "").strip())
        if field_name == "officialWebsite":
            return is_disallowed_result(str(value)) or bool(detect_job_platform(str(value)))
        return is_aggregator_url(value) or bool(rejection_reason(str(value)))
    if field_name == "jobPlatform":
        detected = detect_job_platform(str(company.get("jobBoardUrl") or ""))
        saved = str(value or "").strip()
        if saved.casefold() in PLACEHOLDER_VALUES or saved.casefold() in {"indeed", "linkedin", "ziprecruiter"}:
            return True
        if len(saved) < 2:
            return True
        return bool(saved and detected and saved.casefold() != detected.casefold())
    cleaned = str(value or "").strip()
    if cleaned.casefold() in PLACEHOLDER_VALUES:
        return True
    if field_name == "companyDescription":
        return len(cleaned) < 40
    if field_name in {"city", "state", "industry"}:
        return len(cleaned) < 2
    return False


def missing_company_information_fields(company: dict[str, Any]) -> set[str]:
    missing: set[str] = set()
    for field_name in FIELD_LABELS:
        value = company.get(field_name)
        if not str(value or "").strip() or field_is_clearly_invalid(company, field_name):
            missing.add(field_name)
    if not bool(company.get("websiteVerified")):
        missing.add("officialWebsite")
    return missing


def needs_company_information(company: dict[str, Any]) -> bool:
    return bool(missing_company_information_fields(company))


def confirmed_company_domain(company: dict[str, Any]) -> str:
    if not company.get("websiteVerified"):
        return ""
    return plausible_company_domain(company)


def plausible_company_domain(company: dict[str, Any]) -> str:
    domains = plausible_company_domains(company)
    return next(iter(domains)) if len(domains) == 1 else ""


def plausible_company_domains(company: dict[str, Any]) -> set[str]:
    """Return every plausible first-party domain already saved on a record."""
    domains: set[str] = set()
    for field_name in ("officialWebsite", "knownWebsite", "companyWebsite"):
        raw_value = str(company.get(field_name) or "").strip()
        if not raw_value:
            continue
        candidate = raw_value if "://" in raw_value else f"https://{raw_value}"
        if (
            has_public_http_shape(candidate)
            and not is_disallowed_result(candidate)
            and not is_aggregator_url(candidate)
            and not detect_job_platform(candidate)
        ):
            domain = registered_domain(candidate)
            if domain:
                domains.add(domain)
    return domains


def merge_company_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a lookup input without mutating or deleting duplicate records."""
    if not records:
        raise ValueError("At least one company record is required.")
    ranked = sorted(
        records,
        key=lambda item: (
            -sum(not field_is_clearly_invalid(item, key) and bool(str(item.get(key) or "").strip()) for key in FIELD_LABELS),
            str(item.get("id") or ""),
        ),
    )
    merged = dict(ranked[0])
    for field_name in (*FIELD_LABELS, "knownWebsite", "companyWebsite", "country"):
        if str(merged.get(field_name) or "").strip():
            continue
        for record in ranked[1:]:
            if str(record.get(field_name) or "").strip():
                merged[field_name] = record[field_name]
                break
    if any(
        record.get("websiteVerified")
        and str(record.get("officialWebsite") or "") == str(merged.get("officialWebsite") or "")
        for record in records
    ):
        merged["websiteVerified"] = True
    return merged


def discover_company_information(
    company: dict[str, Any],
    *,
    requested_fields: set[str] | None = None,
    session: requests.Session | None = None,
    use_browser_discovery: bool = False,
    cancelled: Event | None = None,
) -> CompanyInformationDiscovery:
    """Discover only requested missing fields and return confirmed, non-blank updates."""
    requested = set(requested_fields or missing_company_information_fields(company))
    result = CompanyInformationDiscovery()
    if not requested:
        return result

    _check_cancelled(cancelled)
    current_board = str(company.get("jobBoardUrl") or "").strip()
    detected_platform = detect_job_platform(current_board)
    if "jobPlatform" in requested and current_board and detected_platform and not is_aggregator_url(current_board):
        result.updates["jobPlatform"] = detected_platform
        result.found_fields.append("jobPlatform")
        _mark_confirmed_replacement(result.updates, company, "jobPlatform", detected_platform)
        requested.discard("jobPlatform")
    if not requested:
        result.updates["searchStatus"] = "Completed"
        result.updates["reconcileSearchStatus"] = True
        return result

    session = session or make_session()
    result.attempted_network = True
    company_name = str(company.get("name") or company.get("Company Name") or "").strip()
    known_website = _preferred_website(company)
    _check_cancelled(cancelled)
    website = choose_official_website_details(
        company_name,
        known_website,
        session,
        str(company.get("city") or ""),
        str(company.get("state") or ""),
        cancelled=cancelled,
    )
    result.notes.extend(website.notes)
    official_url = website.final_url if website.verified else ""
    homepage_html = str(getattr(website, "html", "") or "")
    if not official_url:
        result.failed = any("could not verify" in note.casefold() for note in result.notes)
        result.notes.append("No official company website could be confirmed.")
        result.updates["searchStatus"] = "Partial"
        result.updates["reconcileSearchStatus"] = True
        return result

    result.updates.update(
        {
            "officialWebsite": official_url,
            "websiteVerified": True,
            "websiteDiscoveryMethod": website.discovery_method,
            "websiteCandidateUrls": "\n".join(website.candidate_urls),
            "websiteVerificationNotes": "; ".join(website.notes),
        }
    )
    result.found_fields.append("officialWebsite")
    _mark_confirmed_replacement(result.updates, company, "officialWebsite", official_url)

    metadata_fields = requested.intersection({"city", "state", "companyDescription", "industry"})
    if metadata_fields:
        _check_cancelled(cancelled)
        metadata, metadata_notes, homepage_html = extract_official_company_metadata(
            official_url,
            company_name,
            session,
            homepage_html=homepage_html,
        )
        result.notes.extend(metadata_notes)
        for key, value in metadata.items():
            if key in metadata_fields and value not in (None, ""):
                result.updates[key] = value
                result.found_fields.append(key)
                _mark_confirmed_replacement(result.updates, company, key, str(value))

    needs_careers = bool(requested.intersection({"careersPageUrl", "jobBoardUrl", "jobPlatform"}))
    careers_url = str(company.get("careersPageUrl") or "").strip()
    careers_html = ""
    if needs_careers:
        if careers_url and field_is_clearly_invalid(company, "careersPageUrl"):
            result.notes.append("The saved careers URL was invalid and was not reused.")
            careers_url = ""
        elif careers_url:
            careers_url, careers_html, validation_note = validate_related_company_url(
                careers_url,
                company_name,
                official_url,
                session,
                source_confirmed=False,
            )
            if validation_note:
                result.notes.append(validation_note)
        if not careers_url:
            _check_cancelled(cancelled)
            candidate, _platform, careers_notes = find_careers_page(
                official_url,
                session,
                initial_html=homepage_html,
                initial_final_url=official_url,
                cancelled=cancelled,
            )
            result.notes.extend(careers_notes)
            if candidate:
                careers_url, careers_html, validation_note = validate_related_company_url(
                    candidate,
                    company_name,
                    official_url,
                    session,
                    source_confirmed=True,
                )
                if validation_note:
                    result.notes.append(validation_note)
        if careers_url:
            result.updates["careersPageUrl"] = careers_url
            result.found_fields.append("careersPageUrl")
            _mark_confirmed_replacement(result.updates, company, "careersPageUrl", careers_url)

    needs_board = bool(requested.intersection({"jobBoardUrl", "jobPlatform"}))
    board_url = ""
    board_html = ""
    if needs_board and current_board and not field_is_clearly_invalid(company, "jobBoardUrl"):
        board_url, board_html, validation_note = validate_related_company_url(
            current_board,
            company_name,
            official_url,
            session,
            source_confirmed=False,
        )
        if validation_note:
            result.notes.append(validation_note)

    if needs_board and not board_url and careers_url and detect_job_platform(careers_url):
        board_url, board_html = careers_url, careers_html

    if needs_board and not board_url:
        _check_cancelled(cancelled)
        row = {
            "Company Name": company_name,
            "Official Website": official_url,
            "Known Website": official_url,
            "Careers Page URL": careers_url,
            "Job Board URL": "",
        }
        discovery = discover_job_board_for_row(
            row,
            session=session,
            use_browser_discovery=False,
            allow_search_fallback=False,
            max_pages=4,
            cancelled=cancelled,
        )
        if not discovery.found and use_browser_discovery:
            _check_cancelled(cancelled)
            with _browser_semaphore:
                _check_cancelled(cancelled)
                discovery = discover_job_board_for_row(
                    row,
                    session=session,
                    use_browser_discovery=True,
                    allow_search_fallback=False,
                    max_pages=4,
                    cancelled=cancelled,
                )
        result.notes.extend(filter(None, [discovery.reason, discovery.notes]))
        if discovery.found and discovery.candidate_selected:
            board_url, board_html, validation_note = validate_related_company_url(
                discovery.candidate_selected,
                company_name,
                official_url,
                session,
                source_confirmed=True,
            )
            if validation_note:
                result.notes.append(validation_note)

    if needs_board and not board_url and careers_url:
        combined_text = _page_text(careers_html)
        if any(indicator in combined_text for indicator in JOB_INDICATORS):
            board_url, board_html = careers_url, careers_html

    if board_url:
        platform = detect_job_platform(board_url) or "Company Careers Site"
        result.updates.update(
            {
                "jobBoardUrl": board_url,
                "jobPlatform": platform,
                "jobBoardDiscoveryMethod": "Validated Official Careers Link",
            }
        )
        result.found_fields.extend(["jobBoardUrl", "jobPlatform"])
        _mark_confirmed_replacement(result.updates, company, "jobBoardUrl", board_url)
        _mark_confirmed_replacement(result.updates, company, "jobPlatform", platform)

    result.found_fields = list(dict.fromkeys(result.found_fields))
    hypothetical = {**company, **result.updates}
    remaining = missing_company_information_fields(hypothetical)
    result.updates["searchStatus"] = "Completed" if not remaining else "Partial"
    result.updates["reconcileSearchStatus"] = True
    return result


def extract_official_company_metadata(
    official_url: str,
    company_name: str,
    session: requests.Session,
    *,
    homepage_html: str = "",
) -> tuple[dict[str, Any], list[str], str]:
    notes: list[str] = []
    html = homepage_html
    if not html:
        try:
            response = _get_with_limited_retries(session, official_url)
            html = response.text
            official_url = str(response.url or official_url)
            response.close()
        except Exception as exc:
            return {}, [f"Could not read official website metadata: {exc}"], ""
    soup = BeautifulSoup(html, "html.parser")
    metadata: dict[str, Any] = {}
    json_ld_items: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            json_ld_items.extend(_flatten_json_ld(json.loads(script.string or "null")))
        except (json.JSONDecodeError, TypeError):
            continue

    organization = _best_organization_item(json_ld_items, company_name)
    if organization:
        address = organization.get("address")
        if isinstance(address, list):
            address = next((item for item in address if isinstance(item, dict)), None)
        if isinstance(address, dict):
            metadata["city"] = _clean_text(address.get("addressLocality"))
            metadata["state"] = _clean_text(address.get("addressRegion"))
        description = _clean_description(organization.get("description"))
        if description:
            metadata["companyDescription"] = description
        industry = organization.get("industry")
        if isinstance(industry, list):
            industry = next((item for item in industry if isinstance(item, str)), "")
        if _clean_text(industry):
            metadata["industry"] = _clean_text(industry)
        elif "bankorcreditunion" in _schema_types(organization):
            metadata["industry"] = "Banking"

    if not metadata.get("companyDescription"):
        for attributes in (
            {"property": re.compile(r"^og:description$", re.I)},
            {"name": re.compile(r"^description$", re.I)},
        ):
            meta = soup.find("meta", attrs=attributes)
            description = _clean_description(meta.get("content") if meta else "")
            if description:
                metadata["companyDescription"] = description
                break

    metadata = {key: value for key, value in metadata.items() if value not in (None, "")}
    if metadata:
        notes.append(f"Confirmed {', '.join(FIELD_LABELS[key] for key in metadata)} from the official website.")
    return metadata, notes, html


def validate_related_company_url(
    candidate_url: str,
    company_name: str,
    official_url: str,
    session: requests.Session,
    *,
    source_confirmed: bool,
) -> tuple[str, str, str]:
    if not has_public_http_shape(candidate_url):
        return "", "", "Rejected a malformed careers or job-board URL."
    if is_aggregator_url(candidate_url) or rejection_reason(candidate_url):
        return "", "", "Rejected a generic third-party job aggregator or unrelated URL."
    try:
        response = _get_with_limited_retries(session, candidate_url)
    except Exception as exc:
        return "", "", f"Could not validate {candidate_url}: {exc}"
    final_url = urldefrag(str(response.url or candidate_url))[0]
    if is_aggregator_url(final_url) or rejection_reason(final_url):
        response.close()
        return "", "", "Rejected a URL that redirected to a third-party job aggregator or unrelated page."
    html = str(response.text or "")
    response.close()
    same_domain = bool(official_url and is_same_registered_domain(official_url, final_url))
    platform = detect_job_platform(final_url)
    text = _page_text(html)
    identity_terms = company_identity_tokens(company_name)
    content_related = bool(
        any(term in text for term in identity_terms)
        and any(indicator in text for indicator in JOB_INDICATORS)
    )
    url_related = company_related_to_url(company_name, final_url)
    if not (same_domain or content_related or (source_confirmed and (platform or url_related))):
        return "", "", "Rejected a careers or job-board result that could not be related to the company."
    if not _has_careers_page_purpose(final_url, html, platform):
        return "", "", "Rejected a company-related URL that was not confirmed as a careers or jobs page."
    return final_url, html, ""


def _preferred_website(company: dict[str, Any]) -> str:
    for key in ("officialWebsite", "knownWebsite", "companyWebsite"):
        value = str(company.get(key) or "").strip()
        candidate = value if "://" in value else f"https://{value}"
        if (
            has_public_http_shape(candidate)
            and not is_disallowed_result(candidate)
            and not detect_job_platform(candidate)
        ):
            return candidate
    return ""


def _get_with_limited_retries(session: requests.Session, url: str) -> requests.Response:
    response = request_with_limited_retries(
        session,
        url,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    try:
        response.raise_for_status()
    except requests.RequestException:
        response.close()
        raise
    return response


def _mark_confirmed_replacement(
    updates: dict[str, Any],
    company: dict[str, Any],
    field_name: str,
    confirmed_value: str,
) -> None:
    current = str(company.get(field_name) or "").strip()
    if current and current.rstrip("/") != confirmed_value.rstrip("/"):
        _add_replace_field(updates, field_name)
        source_values = updates.setdefault("replacementSourceValues", {})
        source_values.setdefault(field_name, current)


def _add_replace_field(updates: dict[str, Any], field_name: str) -> None:
    fields = updates.setdefault("replaceConfirmedFields", [])
    if field_name not in fields:
        fields.append(field_name)


def _best_organization_item(items: list[dict[str, Any]], company_name: str) -> dict[str, Any] | None:
    candidates: list[tuple[int, dict[str, Any]]] = []
    tokens = company_identity_tokens(company_name)
    for item in items:
        types = _schema_types(item)
        if not types.intersection({"organization", "corporation", "financialservice", "bankorcreditunion"}):
            continue
        if not isinstance(item.get("address"), (dict, list)) and not item.get("description") and not item.get("industry"):
            continue
        item_name = _clean_text(item.get("name")).casefold()
        score = 4 if tokens and any(token in item_name for token in tokens) else 0
        score += 2 if "bankorcreditunion" in types else 0
        score += 1 if isinstance(item.get("address"), (dict, list)) else 0
        candidates.append((score, item))
    if not candidates:
        return None
    candidates.sort(key=lambda value: value[0], reverse=True)
    best_score, best = candidates[0]
    if best_score == 0 and len(candidates) > 1:
        return None
    return best


def _schema_types(item: dict[str, Any]) -> set[str]:
    value = item.get("@type")
    values = value if isinstance(value, list) else [value]
    return {str(item_type or "").replace(" ", "").casefold() for item_type in values}


def _flatten_json_ld(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for child in value for item in _flatten_json_ld(child)]
    if not isinstance(value, dict):
        return []
    return [value, *_flatten_json_ld(value.get("@graph", []))]


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _clean_description(value: Any) -> str:
    description = _clean_text(value)
    if len(description) < 40:
        return ""
    return description[:1500]


def _page_text(html: str) -> str:
    if not html:
        return ""
    return " ".join(BeautifulSoup(html, "html.parser").get_text(" ", strip=True).casefold().split())


def _has_careers_page_purpose(final_url: str, html: str, platform: str) -> bool:
    if platform:
        return True
    parsed = urlparse(final_url)
    path = parsed.path.casefold()
    if re.search(r"/(?:careers?|jobs?|employment|openings|opportunities)(?:/|$|-)", path):
        return True
    soup = BeautifulSoup(html, "html.parser")
    prominent = " ".join(
        node.get_text(" ", strip=True)
        for node in [soup.title, *soup.find_all(["h1", "h2"], limit=5)]
        if node is not None
    ).casefold()
    full_text = _page_text(html)
    prominent_signals = ("career", "jobs", "employment", "open positions", "join our team")
    strong_phrases = (
        "apply for this job", "career opportunities", "current openings", "job openings",
        "open positions", "search jobs", "search and apply", "view all jobs",
    )
    return any(signal in prominent for signal in prominent_signals) or any(
        phrase in full_text for phrase in strong_phrases
    )


def _check_cancelled(cancelled: Event | None) -> None:
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError("Cancelled by user.")
