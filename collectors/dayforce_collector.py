from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from backend.outbound_security import install_playwright_url_guard, launch_playwright_chromium, safe_page_goto
from collectors.base import BaseCollector
from excel_tools import stable_company_id
from job_platforms import DAYFORCE_HOST, canonical_job_board_url, match_dayforce_board_path
from job_tools import JobRecord, make_job_id
from job_validation import is_valid_structured_job_title, normalize_job_title, rejection_reason


DAYFORCE_PAGE_SIZE = 25
DAYFORCE_MAX_PAGES = 40
DAYFORCE_MAX_POSTINGS = DAYFORCE_PAGE_SIZE * DAYFORCE_MAX_PAGES


class DayforceCollector(BaseCollector):
    """Collect a public Dayforce board through its browser-origin search API."""

    # The public search POST requires the CSRF token and cookies established by
    # loading the tenant board. A naked HTTP POST is rejected by Dayforce.
    requires_browser = True

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        source_url, source_type = self.source_url(company)
        if not source_url:
            return []

        board_url, _search_url, namespace, board_code, _culture = build_dayforce_urls(source_url)
        jobs: list[JobRecord] = []
        seen_posting_ids: set[str] = set()
        fetched_count = 0
        expected_total: int | None = None
        pagination_complete = False

        try:
            pages = self.fetch_all_pages(board_url)
            if not isinstance(pages, list) or not pages:
                raise ValueError("Dayforce pagination returned no pages")

            for page_index, page in enumerate(pages):
                expected_offset = page_index * DAYFORCE_PAGE_SIZE
                if not isinstance(page, tuple) or len(page) != 2:
                    raise ValueError("Dayforce pagination returned a malformed page result")
                requested_offset, payload = page
                if requested_offset != expected_offset:
                    raise ValueError(
                        f"Dayforce page sequence changed from expected offset {expected_offset} to {requested_offset}"
                    )
                postings, total, response_offset, count = validate_dayforce_page(payload, expected_offset)

                if expected_total is None:
                    expected_total = total
                    if expected_total > DAYFORCE_MAX_POSTINGS:
                        raise ValueError(
                            f"Dayforce reported {expected_total} postings, above the collection safety limit"
                        )
                elif total != expected_total:
                    raise ValueError(
                        f"Dayforce total changed during pagination from {expected_total} to {total}"
                    )

                if response_offset != expected_offset:
                    raise ValueError(
                        f"Dayforce response offset {response_offset} did not match requested offset {expected_offset}"
                    )
                if expected_total == 0:
                    if page_index != 0 or postings or count:
                        raise ValueError("Dayforce returned postings for an explicit zero total")
                    if len(pages) != 1:
                        raise ValueError("Dayforce returned extra pages for an explicit zero total")
                    pagination_complete = True
                    break
                if not postings:
                    raise ValueError(
                        f"Dayforce returned an empty page after {fetched_count} of {expected_total} postings"
                    )

                fetched_count += len(postings)
                if fetched_count > expected_total:
                    raise ValueError(
                        f"Dayforce returned {fetched_count} postings but reported only {expected_total}"
                    )

                for posting in postings:
                    if not isinstance(posting, dict):
                        raise ValueError("Dayforce returned a malformed posting")
                    posting_id = dayforce_posting_id(posting)
                    title = normalize_job_title(clean_text(posting.get("jobTitle")))
                    destination_url = f"{board_url}/jobs/{posting_id}" if posting_id else board_url
                    self.record_candidate(title or f"Dayforce posting {posting_id or 'without an ID'}", destination_url)

                    if not posting_id:
                        self.reject_candidate(
                            title or "Dayforce posting without an ID",
                            "missing Dayforce jobPostingId",
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        raise ValueError("Dayforce listing contained a posting without a valid jobPostingId")
                    if posting_id in seen_posting_ids:
                        self.reject_candidate(
                            title or f"Dayforce posting {posting_id}",
                            "duplicate Dayforce jobPostingId",
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        continue
                    seen_posting_ids.add(posting_id)

                    if not is_valid_structured_job_title(title):
                        self.reject_candidate(
                            title,
                            rejection_reason(title),
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        continue

                    job = build_job(
                        company,
                        posting,
                        posting_id,
                        title,
                        destination_url,
                        source_type,
                        namespace,
                        board_code,
                    )
                    jobs.append(job)
                    self.save_candidate(title, destination_url)

                if fetched_count == expected_total:
                    if page_index != len(pages) - 1:
                        raise ValueError("Dayforce pagination returned pages after the reported total")
                    pagination_complete = True
                    break
                if len(postings) < DAYFORCE_PAGE_SIZE:
                    raise ValueError(
                        f"Dayforce pagination ended early after {fetched_count} of {expected_total} postings"
                    )
        finally:
            self.final_url_after_redirect = board_url
            self.flush_debug(company)

        if expected_total is None:
            raise ValueError("Dayforce pagination returned no maxCount")
        if fetched_count != expected_total:
            raise ValueError(
                f"Dayforce pagination returned {fetched_count} postings but reported {expected_total}"
            )
        if len(seen_posting_ids) != expected_total:
            raise ValueError(
                f"Dayforce pagination returned {len(seen_posting_ids)} unique postings but reported {expected_total}"
            )
        if not pagination_complete:
            raise ValueError("Dayforce pagination reached its safety limit before completion")
        return jobs

    def fetch_all_pages(self, board_url: str) -> list[tuple[int, dict[str, Any]]]:
        board_url, search_url, namespace, board_code, culture = build_dayforce_urls(board_url)
        local_browser_path = Path(__file__).resolve().parents[1] / ".playwright-browsers"
        if local_browser_path.exists() and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browser_path)

        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = launch_playwright_chromium(playwright, headless=True)
            context = None
            try:
                context = browser.new_context(service_workers="block")
                install_playwright_url_guard(context)
                page = context.new_page()

                with page.expect_response(
                    lambda response: is_dayforce_search_response(response, search_url),
                    timeout=45000,
                ) as response_info:
                    try:
                        safe_page_goto(page, board_url, wait_until="domcontentloaded", timeout=45000)
                    except PlaywrightTimeoutError:
                        # The API response is authoritative even when unrelated
                        # page resources keep the navigation from settling.
                        pass
                initial_response = response_info.value
                if initial_response.status != 200:
                    raise ValueError(f"Dayforce initial search returned HTTP {initial_response.status}")
                initial_payload = initial_response.json()
                if not isinstance(initial_payload, dict):
                    raise ValueError("Dayforce search endpoint returned a non-object JSON response")

                request = initial_response.request
                observed_payload = request.post_data_json
                validate_initial_request(observed_payload, namespace, board_code, culture)
                headers = request.all_headers()
                csrf_token = clean_text(headers.get("x-csrf-token"))
                if not csrf_token:
                    raise ValueError("Dayforce initial search request did not include its CSRF token")

                _postings, total, _offset, _count = validate_dayforce_page(initial_payload, 0)
                if total > DAYFORCE_MAX_POSTINGS:
                    raise ValueError(
                        f"Dayforce reported {total} postings, above the collection safety limit"
                    )
                page_count = max(1, math.ceil(total / DAYFORCE_PAGE_SIZE))
                if page_count > DAYFORCE_MAX_PAGES:
                    raise ValueError("Dayforce pagination exceeds the collection page limit")

                pages: list[tuple[int, dict[str, Any]]] = [(0, initial_payload)]
                for page_number in range(1, page_count):
                    offset = page_number * DAYFORCE_PAGE_SIZE
                    payload = fetch_page_in_browser(
                        page,
                        search_url,
                        build_search_payload(namespace, board_code, culture, offset),
                        csrf_token,
                    )
                    pages.append((offset, payload))
                return pages
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                browser.close()


def build_dayforce_urls(board_url: str) -> tuple[str, str, str, str, str]:
    canonical = canonical_job_board_url(str(board_url or "").strip())
    parsed = urlsplit(canonical)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or host != DAYFORCE_HOST:
        raise ValueError("Dayforce collector requires an HTTPS jobs.dayforcehcm.com board URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Dayforce job board URL contains unsupported authority components")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Dayforce job board URL contains an invalid port") from exc
    if port not in {None, 443}:
        raise ValueError("Dayforce job board URL contains unsupported authority components")
    matched = match_dayforce_board_path(parsed.path)
    if matched is None:
        raise ValueError("Dayforce collector requires a tenant namespace and job board code")
    culture, namespace, board_code = matched
    root_path = f"/{culture}/{namespace}/{board_code}"
    normalized_board = urlunsplit(("https", DAYFORCE_HOST, root_path, "", ""))
    search_url = f"https://{DAYFORCE_HOST}/api/geo/{namespace}/jobposting/search"
    return normalized_board, search_url, namespace, board_code, culture


def build_search_payload(namespace: str, board_code: str, culture: str, offset: int) -> dict[str, Any]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("Dayforce pagination offset must be a non-negative integer")
    return {
        "clientNamespace": namespace,
        "jobBoardCode": board_code,
        "cultureCode": culture,
        "distanceUnit": 0,
        "paginationStart": offset,
    }


def validate_initial_request(payload: Any, namespace: str, board_code: str, culture: str) -> None:
    if not isinstance(payload, dict):
        raise ValueError("Dayforce initial search request did not contain an object body")
    expected = build_search_payload(namespace, board_code, culture, 0)
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"Dayforce initial search request contained an unexpected {key}")


def is_dayforce_search_response(response: Any, search_url: str) -> bool:
    request = getattr(response, "request", None)
    method = str(getattr(request, "method", "")).upper()
    return method == "POST" and str(getattr(response, "url", "")).rstrip("/") == search_url.rstrip("/")


def fetch_page_in_browser(
    page: Any,
    search_url: str,
    payload: dict[str, Any],
    csrf_token: str,
) -> dict[str, Any]:
    result = page.evaluate(
        """
        async ({url, payload, csrfToken}) => {
          const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
              "accept": "application/json, text/plain, */*",
              "content-type": "application/json",
              "x-csrf-token": csrfToken
            },
            body: JSON.stringify(payload)
          });
          return {status: response.status, text: await response.text()};
        }
        """,
        {"url": search_url, "payload": payload, "csrfToken": csrf_token},
    )
    if not isinstance(result, dict):
        raise ValueError("Dayforce browser search returned a malformed result")
    status = result.get("status")
    if isinstance(status, bool) or not isinstance(status, int) or status != 200:
        raise ValueError(f"Dayforce search returned HTTP {status}")
    try:
        response_payload = json.loads(str(result.get("text") or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("Dayforce search endpoint returned invalid JSON") from exc
    if not isinstance(response_payload, dict):
        raise ValueError("Dayforce search endpoint returned a non-object JSON response")
    return response_payload


def validate_dayforce_page(payload: Any, expected_offset: int) -> tuple[list[Any], int, int, int]:
    if not isinstance(payload, dict):
        raise ValueError("Dayforce search endpoint returned a non-object JSON response")
    postings = payload.get("jobPostings")
    if not isinstance(postings, list):
        raise ValueError("Dayforce response is missing its jobPostings list")
    total = strict_nonnegative_int(payload.get("maxCount"), "maxCount")
    offset = strict_nonnegative_int(payload.get("offset"), "offset")
    count = strict_nonnegative_int(payload.get("count"), "count")
    if offset != expected_offset:
        raise ValueError(
            f"Dayforce response offset {offset} did not match requested offset {expected_offset}"
        )
    if count != len(postings):
        raise ValueError(
            f"Dayforce response count {count} did not match its {len(postings)} jobPostings"
        )
    if count > DAYFORCE_PAGE_SIZE:
        raise ValueError("Dayforce returned more postings than its public page size")
    return postings, total, offset, count


def strict_nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Dayforce response is missing a valid {field_name}")
    return value


def dayforce_posting_id(posting: dict[str, Any]) -> str:
    value = clean_text(posting.get("jobPostingId"))
    return value if re.fullmatch(r"[0-9]+", value) else ""


def build_job(
    company: dict[str, Any],
    posting: dict[str, Any],
    posting_id: str,
    title: str,
    destination_url: str,
    source_type: str,
    namespace: str,
    board_code: str,
) -> JobRecord:
    company_id = str(company.get("Company ID") or stable_company_id(company))
    description = clean_html(posting.get("jobDescription"))
    locations = posting_locations(posting)
    return JobRecord(
        id=make_job_id(company, title, f"dayforce-{posting_id}-{company_id}"),
        companyId=company_id,
        companyName=str(company.get("Company Name") or ""),
        title=title,
        location="; ".join(locations) or ("Remote" if posting.get("hasVirtualLocation") is True else "Not listed"),
        workType=dayforce_work_type(posting, description, locations),
        postedDate=clean_text(posting.get("postingStartTimestampUTC")),
        sourceUrl=destination_url,
        jobPlatform="Dayforce",
        description=description,
        descriptionSnippet=description[:360],
        collectedAt=datetime.now(timezone.utc).isoformat(),
        rawData={
            "collector": DayforceCollector.__name__,
            "structuredSource": True,
            "sourceType": source_type,
            "clientNamespace": namespace,
            "jobBoardCode": board_code,
            "jobBoardId": posting.get("jobBoardId"),
            "jobPostingId": posting_id,
            "jobReqId": clean_text(posting.get("jobReqId")),
            "postingExpiryTimestampUTC": clean_text(posting.get("postingExpiryTimestampUTC")),
            "isEvergreen": posting.get("isEvergreen"),
            "hasVirtualLocation": posting.get("hasVirtualLocation"),
            "postingAppliedStatus": posting.get("postingAppliedStatus"),
        },
    )


def posting_locations(posting: dict[str, Any]) -> list[str]:
    locations = posting.get("postingLocations")
    if not isinstance(locations, list):
        return []
    values: list[str] = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        formatted = clean_text(
            location.get("formattedAddress")
            or location.get("locationName")
            or location.get("name")
        )
        if not formatted:
            city = clean_text(location.get("city"))
            state_value = location.get("state")
            if isinstance(state_value, dict):
                state = clean_text(
                    state_value.get("code")
                    or state_value.get("shortName")
                    or state_value.get("name")
                )
            else:
                state = clean_text(state_value)
            formatted = ", ".join(value for value in (city, state) if value)
        if formatted and formatted not in values:
            values.append(formatted)
    return values


def dayforce_work_type(posting: dict[str, Any], description: str, locations: list[str]) -> str:
    if posting.get("hasVirtualLocation") is True:
        return "Remote"
    lowered = " ".join((clean_text(posting.get("jobTitle")), description)).casefold()
    if "hybrid" in lowered:
        return "Hybrid"
    if locations:
        return "Onsite"
    return "Not Listed"


def clean_html(value: Any) -> str:
    return clean_text(BeautifulSoup(str(value or ""), "html.parser").get_text(" "))


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())
