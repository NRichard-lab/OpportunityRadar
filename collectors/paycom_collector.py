from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from collectors.base import BaseCollector
from excel_tools import stable_company_id
from job_enrichment import extract_pay_info
from job_tools import JobRecord, make_job_id
from job_validation import is_valid_structured_job_title, normalize_job_title, rejection_reason


PAYCOM_PAGE_SIZE = 50
PAYCOM_MAX_PAGES = 20
PAYCOM_MAX_POSTINGS = PAYCOM_PAGE_SIZE * PAYCOM_MAX_PAGES
PAYCOM_HOST = "www.paycomonline.net"
PAYCOM_BOARD_PATH = re.compile(
    r"^/v4/ats/web\.php/portal/(?P<tenant>[0-9A-Fa-f]{32})/career-page/?$",
    flags=re.IGNORECASE,
)
PAYCOM_CONFIG = re.compile(
    r"var\s+configsFromHost\s*=\s*(\{.*?\});\s*var\s+Mountable",
    flags=re.DOTALL,
)


class PaycomCollector(BaseCollector):
    """Collect Paycom career-portal postings from its anonymous public API."""

    requires_browser = False

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        source_url, source_type = self.source_url(company)
        if not source_url:
            return []

        embedded_url = self.resolve_embedded_job_board_url(source_url, "Paycom")
        board_url = normalize_paycom_board_url(embedded_url)
        bootstrap = self.get(board_url)
        auth_token, api_base = parse_paycom_bootstrap(bootstrap.text)
        search_url = urljoin(api_base, "api/ats/job-posting-previews/search")
        jobs: list[JobRecord] = []
        seen_job_ids: set[str] = set()
        fetched_count = 0
        expected_total: int | None = None
        pagination_complete = False

        try:
            for page_number in range(PAYCOM_MAX_PAGES):
                skip = page_number * PAYCOM_PAGE_SIZE
                payload = self.fetch_search_page(search_url, auth_token, skip)
                total = paycom_total(payload)
                postings = paycom_postings(payload)

                if expected_total is None:
                    expected_total = total
                    if total > PAYCOM_MAX_POSTINGS:
                        raise ValueError(
                            f"Paycom reported {total} postings, above the collection safety limit"
                        )
                elif total != expected_total:
                    raise ValueError(
                        f"Paycom total changed during pagination from {expected_total} to {total}"
                    )

                if len(postings) > PAYCOM_PAGE_SIZE:
                    raise ValueError("Paycom returned more postings than the requested page size")
                if not postings:
                    if expected_total == 0 and fetched_count == 0:
                        pagination_complete = True
                        break
                    raise ValueError(
                        f"Paycom returned an empty page after {fetched_count} of {expected_total} postings"
                    )

                fetched_count += len(postings)
                if fetched_count > expected_total:
                    raise ValueError(
                        f"Paycom returned {fetched_count} postings but reported only {expected_total}"
                    )

                for posting in postings:
                    if not isinstance(posting, dict):
                        raise ValueError("Paycom returned a malformed posting preview")
                    job_id = clean_text(posting.get("jobId"))
                    title = normalize_job_title(clean_text(posting.get("jobTitle")))
                    destination_url = build_destination_url(board_url, job_id) if job_id else board_url
                    self.record_candidate(title or f"Paycom posting {job_id or 'without an ID'}", destination_url)

                    if not job_id:
                        self.reject_candidate(
                            title or "Paycom posting without an ID",
                            "missing Paycom job ID",
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        raise ValueError("Paycom listing contained a posting without a job ID")
                    if job_id in seen_job_ids:
                        self.reject_candidate(
                            title or f"Paycom posting {job_id}",
                            "duplicate Paycom job ID",
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        continue
                    seen_job_ids.add(job_id)

                    if not is_valid_structured_job_title(title):
                        self.reject_candidate(
                            title,
                            rejection_reason(title),
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        continue

                    detail: dict[str, Any] = {}
                    detail_error = ""
                    try:
                        detail_payload = self.fetch_detail(api_base, auth_token, job_id)
                        detail_value = detail_payload.get("jobPosting")
                        if isinstance(detail_value, dict):
                            detail = detail_value
                        else:
                            detail_error = "Paycom detail response did not contain a jobPosting object"
                    except Exception as exc:
                        # Search previews are authoritative current postings; detail is enrichment only.
                        detail_error = f"{type(exc).__name__}: {exc}"
                        if self.debug:
                            self.debug_lines.append(f"DETAIL_FAILED\t{job_id}\t{detail_error}")

                    job = build_job(
                        company,
                        posting,
                        detail,
                        job_id,
                        title,
                        destination_url,
                        source_type,
                        detail_error,
                    )
                    jobs.append(job)
                    if job.payText:
                        pay_info = extract_pay_info(job.payText)
                        self.record_pay_extraction("Paycom salaryRange", title, pay_info)
                    self.save_candidate(title, destination_url)

                if fetched_count == expected_total:
                    pagination_complete = True
                    break
                if len(postings) < PAYCOM_PAGE_SIZE:
                    raise ValueError(
                        f"Paycom pagination ended early after {fetched_count} of {expected_total} postings"
                    )
        finally:
            self.final_url_after_redirect = board_url
            self.flush_debug(company)

        if expected_total is None:
            raise ValueError("Paycom pagination returned no total")
        if fetched_count != expected_total:
            raise ValueError(
                f"Paycom pagination returned {fetched_count} postings but reported {expected_total}"
            )
        if len(seen_job_ids) != expected_total:
            raise ValueError(
                f"Paycom pagination returned {len(seen_job_ids)} unique postings but reported {expected_total}"
            )
        if not pagination_complete:
            raise ValueError("Paycom pagination reached its safety limit before completion")
        return jobs

    def fetch_search_page(self, search_url: str, auth_token: str, skip: int) -> dict[str, Any]:
        return self.request_json(
            "POST",
            search_url,
            auth_token,
            json=build_search_payload(skip),
        )

    def fetch_detail(self, api_base: str, auth_token: str, job_id: str) -> dict[str, Any]:
        return self.request_json(
            "GET",
            urljoin(api_base, f"api/ats/job-postings/{quote(job_id, safe='')}"),
            auth_token,
        )

    def request_json(
        self,
        method: str,
        url: str,
        auth_token: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        time.sleep(max(0, self.delay_seconds))
        response = self.session.request(
            method,
            url,
            headers={
                "Accept": "application/json",
                "Authorization": auth_token,
                "Locale": "en-US",
                "Translation-Highlights": "false",
            },
            timeout=20,
            allow_redirects=True,
            **kwargs,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Paycom API returned a non-object JSON response")
        return payload


def normalize_paycom_board_url(board_url: str) -> str:
    parsed = urlsplit(str(board_url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host != PAYCOM_HOST:
        raise ValueError("Paycom collector requires an HTTPS www.paycomonline.net board URL")
    match = PAYCOM_BOARD_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("Paycom collector requires a career-page URL with a tenant key")
    if parsed.username is not None or parsed.password is not None or parsed.port not in {None, 443}:
        raise ValueError("Paycom board URL contains unsupported authority components")
    path = f"/v4/ats/web.php/portal/{match.group('tenant')}/career-page"
    return urlunsplit(("https", PAYCOM_HOST, path, "", ""))


def parse_paycom_bootstrap(html: str) -> tuple[str, str]:
    match = PAYCOM_CONFIG.search(str(html or ""))
    if not match:
        raise ValueError("Paycom board did not expose its public portal configuration")
    try:
        config = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("Paycom portal configuration was malformed") from exc
    if not isinstance(config, dict):
        raise ValueError("Paycom portal configuration was not an object")
    auth_token = clean_text(config.get("sessionJWT"))
    try:
        library = json.loads(str(config.get("libConfig") or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("Paycom portal library configuration was malformed") from exc
    api_base = clean_text(library.get("atsPortalMantleServiceUrl")) if isinstance(library, dict) else ""
    validate_paycom_api_base(api_base)
    if not auth_token:
        raise ValueError("Paycom portal configuration did not include an anonymous session token")
    return auth_token, api_base.rstrip("/") + "/"


def validate_paycom_api_base(api_base: str) -> None:
    parsed = urlsplit(api_base)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not (host == "paycomonline.net" or host.endswith(".paycomonline.net"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise ValueError("Paycom portal exposed an unsupported API origin")


def build_search_payload(skip: int) -> dict[str, Any]:
    return {
        "skip": skip,
        "take": PAYCOM_PAGE_SIZE,
        "filtersForQuery": {
            "distanceFrom": 0,
            "workEnvironments": [],
            "positionTypes": [],
            "educationLevels": [],
            "categories": [],
            "travelTypes": [],
            "shiftTypes": [],
            "otherFilters": [],
            "keywordSearchText": "",
            "location": "",
            "sortOption": "",
        },
    }


def paycom_total(payload: dict[str, Any]) -> int:
    total = payload.get("jobPostingPreviewsCount")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("Paycom response is missing a valid jobPostingPreviewsCount")
    return total


def paycom_postings(payload: dict[str, Any]) -> list[Any]:
    postings = payload.get("jobPostingPreviews")
    if not isinstance(postings, list):
        raise ValueError("Paycom response is missing its jobPostingPreviews list")
    return postings


def build_destination_url(board_url: str, job_id: str) -> str:
    return f"{board_url.rsplit('/career-page', 1)[0]}/jobs/{quote(job_id, safe='')}"


def build_job(
    company: dict[str, Any],
    preview: dict[str, Any],
    detail: dict[str, Any],
    job_id: str,
    title: str,
    destination_url: str,
    source_type: str,
    detail_error: str,
) -> JobRecord:
    company_id = str(company.get("Company ID") or stable_company_id(company))
    description = html_to_text(detail.get("description")) or clean_text(preview.get("description"))
    qualifications = html_to_text(detail.get("qualifications"))
    if qualifications and qualifications not in description:
        description = clean_text(f"{description} {qualifications}")
    location = paycom_locations(detail, preview)
    pay_text = clean_text(detail.get("salaryRange"))
    posted_date, external_identifier = google_job_fields(detail.get("googleJobJson"))
    return JobRecord(
        id=make_job_id(company, title, f"paycom-{job_id}-{company_id}"),
        companyId=company_id,
        companyName=str(company.get("Company Name") or ""),
        title=title,
        location=location or "Not listed",
        workType=paycom_work_type(detail, preview),
        payText=pay_text,
        postedDate=posted_date or clean_text(preview.get("postedOn")),
        sourceUrl=destination_url,
        jobPlatform="Paycom",
        description=description,
        descriptionSnippet=description[:360],
        collectedAt=datetime.now(timezone.utc).isoformat(),
        rawData={
            "collector": PaycomCollector.__name__,
            "structuredSource": True,
            "sourceType": source_type,
            "jobId": job_id,
            "externalIdentifier": external_identifier,
            "clientCode": clean_text(detail.get("clientCode")),
            "positionType": clean_text(detail.get("positionType") or preview.get("positionType")),
            "jobCategory": clean_text(detail.get("jobCategory")),
            "detailRetrieved": bool(detail),
            "detailError": detail_error,
        },
    )


def paycom_locations(detail: dict[str, Any], preview: dict[str, Any]) -> str:
    values: list[str] = []
    primary = clean_text(detail.get("location") or preview.get("locations"))
    if primary:
        values.append(primary)
    secondary = detail.get("secondaryLocations")
    for item in secondary if isinstance(secondary, list) else []:
        value = clean_text(item.get("location") if isinstance(item, dict) else item)
        if value and value not in values:
            values.append(value)
    return "; ".join(values)


def paycom_work_type(detail: dict[str, Any], preview: dict[str, Any]) -> str:
    text = clean_text(detail.get("remoteType") or preview.get("remoteType")).casefold()
    if "remote" in text:
        return "Remote"
    if "hybrid" in text:
        return "Hybrid"
    description = html_to_text(detail.get("description")).casefold()
    if "in-person" in description or "onsite" in description or "on-site" in description:
        return "Onsite"
    return "Not Listed"


def google_job_fields(value: Any) -> tuple[str, str]:
    try:
        payload = json.loads(str(value or ""))
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    identifier = payload.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value") or identifier.get("name")
    return clean_text(payload.get("datePosted")), clean_text(identifier)


def html_to_text(value: Any) -> str:
    return clean_text(BeautifulSoup(str(value or ""), "html.parser").get_text(" "))


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())
