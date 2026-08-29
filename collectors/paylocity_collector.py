from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from collectors.base import BaseCollector
from excel_tools import stable_company_id
from job_tools import JobRecord, make_job_id
from job_validation import is_valid_structured_job_title, normalize_job_title, rejection_reason


PAYLOCITY_HOST = "recruiting.paylocity.com"
PAYLOCITY_LISTING_PREFIX = "/recruiting/jobs/"
PAYLOCITY_DETAIL_PREFIX = "/Recruiting/Jobs/Details/"
PAYLOCITY_MAX_JOBS = 5000
PAYLOCITY_JOB_ID = re.compile(r"^[1-9]\d{0,18}$")


class PaylocityCollector(BaseCollector):
    requires_browser = False

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        source_url, source_type = self.source_url(company)
        board_url = validate_paylocity_listing_url(
            self.resolve_embedded_job_board_url(source_url, "Paylocity")
        )
        jobs: list[JobRecord] = []
        listing_url = board_url
        try:
            response = self.get(board_url)
            listing_url = validate_paylocity_listing_url(str(getattr(response, "url", "") or board_url))
            self.final_url_after_redirect = listing_url
            payload = parse_paylocity_page_data(str(getattr(response, "text", "") or ""))
            listings = payload.get("Jobs")
            if not isinstance(listings, list):
                raise ValueError("Paylocity pageData is missing its Jobs list")
            if len(listings) > PAYLOCITY_MAX_JOBS:
                raise ValueError("Paylocity Jobs list exceeded its safety limit")

            module_id = clean_text(payload.get("ModuleId"))
            seen_job_ids: set[str] = set()
            for listing in listings:
                if not isinstance(listing, dict):
                    raise ValueError("Paylocity Jobs list contained a non-object record")

                job_id = paylocity_job_id(listing.get("JobId"))
                title = normalize_job_title(str(listing.get("JobTitle") or ""))
                destination_url = build_paylocity_detail_url(job_id) if job_id else listing_url
                self.record_candidate(title or f"Paylocity job {job_id or 'without an ID'}", destination_url)

                if not job_id:
                    self.reject_candidate(
                        title or "Paylocity job without an ID",
                        "missing or invalid Paylocity JobId",
                        destination_url,
                        company=company,
                        job_board_url=board_url,
                    )
                    raise ValueError("Paylocity Jobs list contained a record without a valid JobId")
                if job_id in seen_job_ids:
                    self.reject_candidate(
                        title,
                        "duplicate Paylocity JobId",
                        destination_url,
                        company=company,
                        job_board_url=board_url,
                    )
                    continue
                seen_job_ids.add(job_id)
                if listing.get("IsInternal") is True:
                    self.reject_candidate(
                        title,
                        "internal Paylocity job",
                        destination_url,
                        company=company,
                        job_board_url=board_url,
                    )
                    continue
                if not title:
                    self.reject_candidate(
                        "Paylocity job without a title",
                        "blank title",
                        destination_url,
                        company=company,
                        job_board_url=board_url,
                    )
                    raise ValueError("Paylocity Jobs list contained a record without a title")
                if not is_valid_structured_job_title(title):
                    self.reject_candidate(
                        title,
                        rejection_reason(title),
                        destination_url,
                        company=company,
                        job_board_url=board_url,
                    )
                    continue

                detail: dict[str, str] = {}
                detail_retrieved = False
                try:
                    detail_response = self.get(destination_url)
                    validate_paylocity_detail_url(
                        str(getattr(detail_response, "url", "") or destination_url),
                        job_id,
                    )
                    detail = parse_paylocity_detail(str(getattr(detail_response, "text", "") or ""))
                    detail_title = normalize_job_title(detail.get("title", ""))
                    if detail_title and detail_title.casefold() != title.casefold():
                        raise ValueError("Paylocity detail title did not match its listing record")
                    if not any(detail.values()):
                        raise ValueError("Paylocity detail page did not contain recognizable job content")
                    detail_retrieved = True
                except Exception as exc:
                    if self.debug:
                        self.debug_lines.append(f"DETAIL_FAILED\t{job_id}\t{type(exc).__name__}: {exc}")

                job = build_paylocity_job(
                    company,
                    listing,
                    detail,
                    job_id,
                    title,
                    destination_url,
                    source_type,
                    module_id,
                    detail_retrieved,
                )
                jobs.append(job)
                if job.payText:
                    self.record_pay_extraction(
                        "Paylocity detail Salary Description",
                        title,
                        {
                            "payText": job.payText,
                            "payMin": job.payMin,
                            "payMax": job.payMax,
                            "payPeriod": job.payPeriod,
                            "payPatternMatched": "Salary Description",
                        },
                    )
                self.save_candidate(title, destination_url)
        finally:
            # Diagnostics should identify the authoritative listing, not the last detail fetched.
            self.final_url_after_redirect = listing_url
            self.flush_debug(company)
        return jobs


def find_paylocity_listing_scope(page):
    """Retained for embedded-board/browser discovery compatibility."""

    if "recruiting.paylocity.com" in page.url.lower():
        return page
    for frame in page.frames:
        frame_url = frame.url.lower()
        if "recruiting.paylocity.com" in frame_url and "/recruiting/jobs/" in frame_url:
            return frame
    return None


def validate_paylocity_listing_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Paylocity job board URL is malformed") from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    path = parsed.path or ""
    if (
        parsed.scheme.casefold() != "https"
        or host != PAYLOCITY_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Paylocity collector requires an HTTPS recruiting.paylocity.com job board URL")
    if not path.casefold().startswith(PAYLOCITY_LISTING_PREFIX):
        raise ValueError("Paylocity collector requires a recruiting Jobs listing URL")
    suffix = path[len(PAYLOCITY_LISTING_PREFIX):].casefold()
    if suffix.startswith("details/") or suffix.startswith("apply/"):
        raise ValueError("Paylocity collector requires a Jobs listing rather than a job detail URL")
    return str(url).strip()


def validate_paylocity_detail_url(url: str, job_id: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Paylocity detail URL is malformed") from exc
    expected_path = f"{PAYLOCITY_DETAIL_PREFIX}{job_id}".casefold()
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or host != PAYLOCITY_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/").casefold() != expected_path
    ):
        raise ValueError("Paylocity detail response redirected outside the expected job URL")
    return str(url).strip()


def parse_paylocity_page_data(html: str) -> dict[str, Any]:
    marker = re.search(r"window\.pageData\s*=\s*", str(html or ""))
    if marker is None:
        raise ValueError("Paylocity listing did not contain window.pageData")
    try:
        payload, _end = json.JSONDecoder().raw_decode(html, marker.end())
    except (TypeError, ValueError) as exc:
        raise ValueError("Paylocity listing contained malformed window.pageData JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Paylocity window.pageData was not an object")
    return payload


def paylocity_job_id(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    text = str(value or "").strip()
    return text if PAYLOCITY_JOB_ID.fullmatch(text) else ""


def build_paylocity_detail_url(job_id: str) -> str:
    if not PAYLOCITY_JOB_ID.fullmatch(str(job_id or "")):
        raise ValueError("Paylocity detail URL requires a numeric JobId")
    return f"https://{PAYLOCITY_HOST}{PAYLOCITY_DETAIL_PREFIX}{job_id}"


def parse_paylocity_detail(html: str) -> dict[str, str]:
    soup = BeautifulSoup(str(html or ""), "html.parser")
    title_node = soup.select_one(".job-preview-title")
    location_node = soup.select_one(".preview-location")
    sections: dict[str, str] = {}
    for header in soup.select(".job-listing-header"):
        label = clean_text(header.get_text(" ", strip=True)).casefold()
        content = header.find_next_sibling()
        if label and content is not None:
            sections[label] = clean_text(content.get_text(" ", strip=True))
    return {
        "title": clean_text(title_node.get_text(" ", strip=True)) if title_node else "",
        "location": clean_text(location_node.get_text(" ", strip=True)) if location_node else "",
        "jobType": sections.get("job type", ""),
        "description": sections.get("description", ""),
        "salary": sections.get("salary description", ""),
    }


def build_paylocity_job(
    company: dict[str, Any],
    listing: dict[str, Any],
    detail: dict[str, str],
    job_id: str,
    title: str,
    destination_url: str,
    source_type: str,
    module_id: str,
    detail_retrieved: bool,
) -> JobRecord:
    company_id = str(company.get("Company ID") or stable_company_id(company))
    description = clean_text(detail.get("description") or listing.get("Description") or title)
    location = structured_location(listing) or clean_text(detail.get("location")) or "Not listed"
    # Paylocity exposes compensation in a labelled detail section. Do not infer
    # salary from the description, where sign-on bonuses and benefit amounts are common.
    pay_text = clean_text(detail.get("salary"))
    pay_min, pay_max = parse_pay(pay_text)
    pay_period = extract_pay_period(pay_text)
    work_type = paylocity_work_type(listing, detail, description)
    return JobRecord(
        # Use the stable Paylocity JobId explicitly; long board URLs can be truncated
        # by the global ID helper before their distinguishing suffix.
        id=make_job_id(company, title, f"paylocity-{job_id}-{company_id}"),
        companyId=company_id,
        companyName=str(company.get("Company Name") or ""),
        title=title,
        location=location,
        workType=work_type,
        payMin=pay_min,
        payMax=pay_max,
        payText=pay_text,
        payPeriod=pay_period,
        postedDate=clean_text(listing.get("PublishedDate")),
        sourceUrl=destination_url,
        jobPlatform="Paylocity",
        description=description,
        descriptionSnippet=description[:360].strip(),
        collectedAt=datetime.now(timezone.utc).isoformat(),
        rawData={
            "collector": PaylocityCollector.__name__,
            "structuredSource": True,
            "sourceType": source_type,
            "paylocityJobId": job_id,
            "moduleId": module_id,
            "hiringDepartment": clean_text(listing.get("HiringDepartment")),
            "detailRetrieved": detail_retrieved,
        },
    )


def structured_location(listing: dict[str, Any]) -> str:
    raw_location = listing.get("JobLocation")
    if isinstance(raw_location, dict):
        city = clean_text(raw_location.get("City"))
        state = clean_text(raw_location.get("State"))
        if city or state:
            return ", ".join(value for value in (city, state) if value)
    return clean_text(listing.get("LocationName"))


def extract_location(text: str) -> str:
    match = re.search(r"([A-Za-z .'-]+,\s*[A-Z]{2})\b", text)
    return clean_text(match.group(1)) if match else ""


def extract_work_type(text: str) -> str:
    lowered = text.lower()
    if "remote" in lowered:
        return "Remote"
    if "hybrid" in lowered:
        return "Hybrid"
    if "onsite" in lowered or "on-site" in lowered:
        return "Onsite"
    return "Not Listed"


def paylocity_work_type(listing: dict[str, Any], detail: dict[str, str], description: str) -> str:
    text = " ".join(
        clean_text(value)
        for value in (
            listing.get("JobTitle"),
            listing.get("LocationName"),
            detail.get("jobType"),
            description,
        )
    )
    lowered = text.casefold()
    if "hybrid" in lowered:
        return "Hybrid"
    if listing.get("IsRemote") is True or "remote" in lowered:
        return "Remote"
    if listing.get("IsRemote") is False:
        return "Onsite"
    return extract_work_type(text)


def extract_pay_text(text: str) -> str:
    match = re.search(
        r"\$\s?\d[\d,]*(?:\.\d{1,2})?"
        r"(?:\s*(?:-|to|–|—)\s*\$?\s?\d[\d,]*(?:\.\d{1,2})?)?"
        r"(?:\s*(?:/|per\s+)?(?:hour|hr|year|yr|annually|annual|month|monthly|week|weekly))?",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    return clean_text(match.group(0)) if match else ""


def parse_pay(pay_text: str) -> tuple[float | None, float | None]:
    values = [
        float(value.replace(",", ""))
        for value in re.findall(r"(?<![\d.])\$?\s?(\d[\d,]*(?:\.\d{1,2})?)", str(pay_text or ""))
    ]
    if len(values) >= 2:
        return min(values), max(values)
    if values:
        return values[0], values[0]
    return None, None


def extract_pay_period(pay_text: str) -> str:
    lowered = str(pay_text or "").casefold()
    if any(value in lowered for value in ("hour", "/hr", "per hr")):
        return "hourly"
    if any(value in lowered for value in ("annual", "year", "/yr")):
        return "annual"
    if "month" in lowered:
        return "monthly"
    if "week" in lowered:
        return "weekly"
    return "unknown"


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())
