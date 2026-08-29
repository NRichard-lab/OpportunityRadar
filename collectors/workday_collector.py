from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from collectors.base import BaseCollector
from excel_tools import stable_company_id
from job_tools import JobRecord, make_job_id
from job_validation import is_valid_structured_job_title, normalize_job_title, rejection_reason


WORKDAY_PAGE_SIZE = 20
WORKDAY_MAX_PAGES = 50
WORKDAY_MAX_POSTINGS = WORKDAY_PAGE_SIZE * WORKDAY_MAX_PAGES
WORKDAY_HOST = re.compile(
    r"^(?P<tenant>[a-z0-9][a-z0-9-]*)\.(?:wd\d+\.)?myworkdayjobs\.com$",
    flags=re.IGNORECASE,
)
WORKDAY_SITE = re.compile(r"^[A-Za-z0-9_-]+$")
WORKDAY_LOCALE = re.compile(r"^[A-Za-z]{2}(?:[-_][A-Za-z]{2})?$")


class WorkdayCollector(BaseCollector):
    """Collect public Workday postings through the board's CXS JSON API."""

    requires_browser = False

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        source_url, source_type = self.source_url(company)
        if not source_url:
            return []

        embedded_url = self.resolve_embedded_job_board_url(source_url, "Workday")
        board_url, api_base = build_workday_urls(embedded_url)
        listing_url = f"{api_base}/jobs"
        jobs: list[JobRecord] = []
        seen_paths: set[str] = set()
        fetched_count = 0
        expected_total: int | None = None
        pagination_complete = False

        try:
            for page_number in range(WORKDAY_MAX_PAGES):
                offset = page_number * WORKDAY_PAGE_SIZE
                payload = self.fetch_page(listing_url, offset)
                postings = workday_postings(payload)
                page_total = workday_total(payload, required=expected_total is None)

                if expected_total is None:
                    expected_total = page_total
                    if expected_total is None:  # pragma: no cover - guarded by required=True
                        raise ValueError("Workday listing response is missing a valid total")
                    if expected_total > WORKDAY_MAX_POSTINGS:
                        raise ValueError(
                            f"Workday reported {expected_total} postings, above the collection safety limit"
                        )
                elif page_total not in {None, 0, expected_total}:
                    raise ValueError(
                        f"Workday listing total changed during pagination from {expected_total} to {page_total}"
                    )

                if len(postings) > WORKDAY_PAGE_SIZE:
                    raise ValueError("Workday returned more postings than the requested page size")
                if not postings:
                    if fetched_count == expected_total:
                        pagination_complete = True
                        break
                    raise ValueError(
                        f"Workday pagination returned an empty page after {fetched_count} of {expected_total} postings"
                    )

                fetched_count += len(postings)
                if fetched_count > expected_total:
                    raise ValueError(
                        f"Workday returned {fetched_count} postings but reported only {expected_total}"
                    )

                for posting in postings:
                    if not isinstance(posting, dict):
                        self.record_candidate("Malformed Workday posting")
                        self.reject_candidate(
                            "Malformed Workday posting",
                            "Workday posting was not an object",
                            company=company,
                            job_board_url=board_url,
                        )
                        raise ValueError("Workday listing contained a malformed posting")

                    external_path = workday_external_path(posting)
                    title = normalize_job_title(clean_text(posting.get("title")))
                    destination_url = build_destination_url(board_url, external_path) if external_path else board_url
                    self.record_candidate(title or "Workday posting without a title", destination_url)

                    if not external_path:
                        self.reject_candidate(
                            title or "Workday posting without a path",
                            "missing Workday externalPath",
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        raise ValueError("Workday listing contained a posting without externalPath")
                    if external_path in seen_paths:
                        self.reject_candidate(
                            title or external_path,
                            "duplicate Workday externalPath",
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        continue
                    seen_paths.add(external_path)

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
                    detail_retrieved = False
                    try:
                        detail = self.fetch_detail(api_base, external_path)
                        detail_retrieved = True
                    except Exception as exc:
                        if self.debug:
                            self.debug_lines.append(
                                f"DETAIL_FAILED\t{external_path}\t{type(exc).__name__}: {exc}"
                            )

                    job = build_job(
                        company,
                        posting,
                        detail,
                        title,
                        destination_url,
                        external_path,
                        source_type,
                        detail_retrieved,
                    )
                    jobs.append(job)
                    if job.payText:
                        self.record_pay_extraction(
                            "Workday job description",
                            title,
                            {
                                "payText": job.payText,
                                "payMin": job.payMin,
                                "payMax": job.payMax,
                                "payPeriod": "unknown",
                                "payPatternMatched": "Workday job description salary range",
                            },
                        )
                    self.save_candidate(title, destination_url)

                if fetched_count == expected_total:
                    pagination_complete = True
                    break
                if len(postings) < WORKDAY_PAGE_SIZE:
                    raise ValueError(
                        f"Workday pagination ended early after {fetched_count} of {expected_total} postings"
                    )
        finally:
            # Diagnostics should identify the user-facing board, not the last API detail URL.
            self.final_url_after_redirect = board_url
            self.flush_debug(company)

        if expected_total is None:
            raise ValueError("Workday pagination returned no total")
        if fetched_count != expected_total:
            raise ValueError(
                f"Workday pagination returned {fetched_count} postings but reported {expected_total}"
            )
        if len(seen_paths) != expected_total:
            raise ValueError(
                f"Workday pagination returned {len(seen_paths)} unique postings but reported {expected_total}"
            )
        if not pagination_complete:
            raise ValueError("Workday pagination reached its safety limit before completion")
        return jobs

    def fetch_page(self, listing_url: str, offset: int) -> dict[str, Any]:
        time.sleep(max(0, self.delay_seconds))
        response = self.session.post(
            listing_url,
            json={
                "appliedFacets": {},
                "limit": WORKDAY_PAGE_SIZE,
                "offset": offset,
                "searchText": "",
            },
            headers={"Accept": "application/json"},
            timeout=20,
            allow_redirects=True,
        )
        response.raise_for_status()
        return response_json_object(response, "Workday listing endpoint")

    def fetch_detail(self, api_base: str, external_path: str) -> dict[str, Any]:
        response = self.get(f"{api_base}{external_path}")
        payload = response_json_object(response, "Workday detail endpoint")
        detail = payload.get("jobPostingInfo")
        if not isinstance(detail, dict):
            raise ValueError("Workday detail response is missing its jobPostingInfo object")
        return detail


def build_workday_urls(board_url: str) -> tuple[str, str]:
    parsed = urlsplit(str(board_url or "").strip())
    host = (parsed.hostname or "").lower()
    host_match = WORKDAY_HOST.fullmatch(host)
    if parsed.scheme != "https" or host_match is None:
        raise ValueError("Workday collector requires an HTTPS myworkdayjobs.com job board URL")
    if parsed.username is not None or parsed.password is not None or parsed.port not in {None, 443}:
        raise ValueError("Workday job board URL contains unsupported authority components")

    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    locale = ""
    if len(segments) == 2 and WORKDAY_LOCALE.fullmatch(segments[0]):
        locale, site = segments
    elif len(segments) == 1:
        site = segments[0]
    else:
        raise ValueError("Workday job board URL must contain exactly one career site")
    if not WORKDAY_SITE.fullmatch(site):
        raise ValueError("Workday job board URL contains an invalid career site")

    tenant = host_match.group("tenant").lower()
    board_segments = [segment for segment in (locale, site) if segment]
    board_path = "/" + "/".join(quote(segment, safe="-_") for segment in board_segments)
    normalized_board = urlunsplit(("https", host, board_path, "", ""))
    api_base = urlunsplit(
        (
            "https",
            host,
            f"/wday/cxs/{quote(tenant, safe='-_')}/{quote(site, safe='-_')}",
            "",
            "",
        )
    )
    return normalized_board, api_base


def workday_total(payload: dict[str, Any], *, required: bool) -> int | None:
    total = payload.get("total")
    if total is None and not required:
        return None
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("Workday listing response is missing a valid total")
    return total


def workday_postings(payload: dict[str, Any]) -> list[Any]:
    postings = payload.get("jobPostings")
    if not isinstance(postings, list):
        raise ValueError("Workday listing response is missing its jobPostings list")
    return postings


def workday_external_path(posting: dict[str, Any]) -> str:
    value = clean_text(posting.get("externalPath"))
    parsed = urlsplit(value)
    if (
        not value
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/job/")
    ):
        return ""
    return parsed.path


def build_destination_url(board_url: str, external_path: str) -> str:
    if not workday_external_path({"externalPath": external_path}):
        raise ValueError("Workday posting contains an invalid externalPath")
    return f"{board_url.rstrip('/')}{external_path}"


def response_json_object(response: Any, label: str) -> dict[str, Any]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{label} returned a non-object JSON response")
    return payload


def workday_external_id(detail: dict[str, Any], posting: dict[str, Any], external_path: str) -> str:
    external_id = clean_text(detail.get("jobReqId") or detail.get("jobPostingId"))
    if external_id:
        return external_id
    bullet_fields = posting.get("bulletFields")
    if isinstance(bullet_fields, list):
        for value in bullet_fields:
            external_id = clean_text(value)
            if external_id:
                return external_id
    return unquote(external_path.rsplit("/", 1)[-1])


def build_job(
    company: dict[str, Any],
    posting: dict[str, Any],
    detail: dict[str, Any],
    title: str,
    destination_url: str,
    external_path: str,
    source_type: str,
    detail_retrieved: bool,
) -> JobRecord:
    company_id = str(company.get("Company ID") or stable_company_id(company))
    external_id = workday_external_id(detail, posting, external_path)
    description = clean_html(detail.get("jobDescription"))
    location = workday_location(detail, posting)
    pay_text = extract_pay_text(description)
    pay_min, pay_max = parse_pay(pay_text)
    return JobRecord(
        id=make_job_id(company, title, f"workday-{external_id}-{company_id}"),
        companyId=company_id,
        companyName=str(company.get("Company Name") or ""),
        title=title,
        location=location or "Not listed",
        workType=extract_work_type(" ".join([location, description])),
        payMin=pay_min,
        payMax=pay_max,
        payText=pay_text,
        postedDate=clean_text(detail.get("startDate") or detail.get("postedOn") or posting.get("postedOn")),
        sourceUrl=destination_url,
        jobPlatform="Workday",
        description=description,
        descriptionSnippet=description[:360],
        collectedAt=datetime.now(timezone.utc).isoformat(),
        rawData={
            "collector": WorkdayCollector.__name__,
            "structuredSource": True,
            "sourceType": source_type,
            "externalJobId": external_id,
            "externalPath": external_path,
            "jobPostingId": clean_text(detail.get("jobPostingId")),
            "postedOn": clean_text(posting.get("postedOn")),
            "timeType": clean_text(detail.get("timeType")),
            "detailRetrieved": detail_retrieved,
        },
    )


def workday_location(detail: dict[str, Any], posting: dict[str, Any]) -> str:
    values: list[str] = []
    primary = clean_text(detail.get("location") or posting.get("locationsText"))
    if primary:
        values.append(primary)
    additional = detail.get("additionalLocations")
    if isinstance(additional, list):
        candidates = additional
    elif isinstance(additional, str):
        candidates = [additional]
    else:
        candidates = []
    for candidate in candidates:
        value = clean_text(candidate)
        if value and value not in values:
            values.append(value)
    return "; ".join(values)


def clean_html(value: Any) -> str:
    return clean_text(BeautifulSoup(str(value or ""), "html.parser").get_text(" "))


def extract_workday_location(row_text: str) -> str:
    match = re.search(r"\blocations?\s+(.+?)\s+posted on\b", row_text, flags=re.IGNORECASE)
    if match:
        return clean_text(match.group(1))
    match = re.search(r"\blocation\s+(.+?)\s+posted\b", row_text, flags=re.IGNORECASE)
    if match:
        return clean_text(match.group(1))
    return ""


def extract_posted_date(row_text: str) -> str:
    match = re.search(r"\bposted on\s+(.+?)(?:\s+job requisition|\s+R-\d+|$)", row_text, flags=re.IGNORECASE)
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


def extract_pay_text(text: str) -> str:
    patterns = [
        r"\$\s?\d{2,3}(?:,\d{3})?(?:\.\d{2})?\s*(?:-|to|–)\s*\$\s?\d{2,3}(?:,\d{3})?(?:\.\d{2})?",
        r"\$\s?\d{2,3}(?:\.\d{2})?\s*/\s*hour\s*(?:-|to|–)\s*\$\s?\d{2,3}(?:\.\d{2})?\s*/\s*hour",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(0))
    return ""


def parse_pay(pay_text: str) -> tuple[int | None, int | None]:
    values = []
    for value in re.findall(r"\$\s?(\d{2,3}(?:,\d{3})?(?:\.\d{2})?)", pay_text):
        number = float(value.replace(",", ""))
        values.append(int(number))
    if len(values) >= 2:
        return min(values), max(values)
    if len(values) == 1:
        return values[0], values[0]
    return None, None


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def click_next_page(page) -> bool:
    next_button = page.locator("button[aria-label='next'], [role='button'][aria-label='next']").first
    try:
        if next_button.count() == 0 or not next_button.is_visible(timeout=1000):
            return False
        disabled = next_button.get_attribute("disabled")
        aria_disabled = next_button.get_attribute("aria-disabled")
        if disabled is not None or str(aria_disabled).lower() == "true":
            return False
        next_button.scroll_into_view_if_needed(timeout=2000)
        next_button.click(timeout=5000)
        page.wait_for_load_state("networkidle", timeout=10000)
        page.wait_for_timeout(1000)
        return True
    except Exception:
        return False
