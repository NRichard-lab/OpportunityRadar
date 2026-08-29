from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from collectors.base import BaseCollector
from excel_tools import stable_company_id
from job_tools import JobRecord, make_job_id
from job_validation import is_valid_structured_job_title, normalize_job_title, rejection_reason


UKG_PAGE_SIZE = 50
UKG_MAX_PAGES = 20
UKG_MAX_OPPORTUNITIES = UKG_PAGE_SIZE * UKG_MAX_PAGES
UKG_BOARD_PATH = re.compile(
    r"^/(?P<tenant>[A-Za-z0-9_-]+)/JobBoard/(?P<board>[0-9A-Fa-f-]{36})/?$",
    flags=re.IGNORECASE,
)
UKG_RECRUITING_HOSTS = {"recruiting.ultipro.com", "recruiting2.ultipro.com"}


class UKGCollector(BaseCollector):
    """Collect public UKG Recruiting postings through the job-board search endpoint."""

    requires_browser = False

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        source_url, source_type = self.source_url(company)
        if not source_url:
            return []

        embedded_url = self.resolve_embedded_job_board_url(source_url, "UKG")
        board_url, search_url = build_ukg_urls(embedded_url)
        jobs: list[JobRecord] = []
        seen_opportunity_ids: set[str] = set()
        fetched_count = 0
        expected_total: int | None = None
        pagination_complete = False

        try:
            for page_number in range(UKG_MAX_PAGES):
                skip = page_number * UKG_PAGE_SIZE
                payload = self.fetch_page(search_url, skip)
                total = ukg_total(payload)
                opportunities = ukg_opportunities(payload)

                if expected_total is None:
                    expected_total = total
                    if expected_total > UKG_MAX_OPPORTUNITIES:
                        raise ValueError(
                            f"UKG reported {expected_total} opportunities, above the collection safety limit"
                        )
                elif total != expected_total:
                    raise ValueError(f"UKG total changed during pagination from {expected_total} to {total}")

                if len(opportunities) > UKG_PAGE_SIZE:
                    raise ValueError("UKG returned more opportunities than the requested page size")
                if not opportunities:
                    if expected_total == 0 and fetched_count == 0:
                        pagination_complete = True
                        break
                    raise ValueError(
                        f"UKG returned an empty page after {fetched_count} of {expected_total} opportunities"
                    )

                fetched_count += len(opportunities)
                if fetched_count > expected_total:
                    raise ValueError(
                        f"UKG returned {fetched_count} opportunities but reported only {expected_total}"
                    )

                for opportunity in opportunities:
                    if not isinstance(opportunity, dict):
                        raise ValueError("UKG returned a malformed opportunity")
                    opportunity_id = clean_text(opportunity.get("Id"))
                    title = normalize_job_title(clean_text(opportunity.get("Title")))
                    destination_url = build_destination_url(board_url, opportunity_id) if opportunity_id else board_url
                    self.record_candidate(title or f"UKG opportunity {opportunity_id or 'without an ID'}", destination_url)

                    if not opportunity_id:
                        self.reject_candidate(
                            title or "UKG opportunity without an ID",
                            "missing UKG opportunity ID",
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        raise ValueError("UKG listing contained an opportunity without an ID")
                    if opportunity_id in seen_opportunity_ids:
                        self.reject_candidate(
                            title or f"UKG opportunity {opportunity_id}",
                            "duplicate UKG opportunity ID",
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        continue
                    seen_opportunity_ids.add(opportunity_id)

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
                        opportunity,
                        opportunity_id,
                        title,
                        destination_url,
                        source_type,
                    )
                    jobs.append(job)
                    self.save_candidate(title, destination_url)

                if fetched_count == expected_total:
                    pagination_complete = True
                    break
                if len(opportunities) < UKG_PAGE_SIZE:
                    raise ValueError(
                        f"UKG pagination ended early after {fetched_count} of {expected_total} opportunities"
                    )
        finally:
            # Diagnostics should identify the user-facing board instead of the POST endpoint.
            self.final_url_after_redirect = board_url
            self.flush_debug(company)

        if expected_total is None:
            raise ValueError("UKG pagination returned no total")
        if fetched_count != expected_total:
            raise ValueError(
                f"UKG pagination returned {fetched_count} opportunities but reported {expected_total}"
            )
        if len(seen_opportunity_ids) != expected_total:
            raise ValueError(
                f"UKG pagination returned {len(seen_opportunity_ids)} unique opportunities but reported {expected_total}"
            )
        if not pagination_complete:
            raise ValueError("UKG pagination reached its safety limit before completion")
        return jobs

    def fetch_page(self, search_url: str, skip: int) -> dict[str, Any]:
        time.sleep(max(0, self.delay_seconds))
        response = self.session.post(
            search_url,
            json=build_search_payload(skip),
            headers={"Accept": "application/json"},
            timeout=20,
            allow_redirects=True,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("UKG search endpoint returned a non-object JSON response")
        return payload


def build_ukg_urls(board_url: str) -> tuple[str, str]:
    parsed = urlsplit(str(board_url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in UKG_RECRUITING_HOSTS:
        raise ValueError("UKG collector requires an HTTPS UKG Recruiting job board URL")
    match = UKG_BOARD_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("UKG collector requires a tenant and JobBoard UUID in the URL")
    if parsed.username is not None or parsed.password is not None or parsed.port not in {None, 443}:
        raise ValueError("UKG job board URL contains unsupported authority components")

    board_path = f"/{match.group('tenant')}/JobBoard/{match.group('board')}"
    normalized_board = urlunsplit(("https", host, board_path, "", ""))
    search_url = f"{normalized_board}/JobBoardView/LoadSearchResults"
    return normalized_board, search_url


def build_search_payload(skip: int) -> dict[str, Any]:
    return {
        "opportunitySearch": {
            "QueryString": "",
            "Filters": [],
            "Top": UKG_PAGE_SIZE,
            "Skip": skip,
            "OrderBy": [
                {
                    "Value": "postedDateDesc",
                    "PropertyName": "PostedDate",
                    "Ascending": False,
                }
            ],
        }
    }


def ukg_total(payload: dict[str, Any]) -> int:
    total = payload.get("totalCount")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("UKG response is missing a valid totalCount")
    return total


def ukg_opportunities(payload: dict[str, Any]) -> list[Any]:
    opportunities = payload.get("opportunities")
    if not isinstance(opportunities, list):
        raise ValueError("UKG response is missing its opportunities list")
    return opportunities


def build_destination_url(board_url: str, opportunity_id: str) -> str:
    return f"{board_url}/OpportunityDetail?opportunityId={quote(opportunity_id, safe='')}"


def build_job(
    company: dict[str, Any],
    opportunity: dict[str, Any],
    opportunity_id: str,
    title: str,
    destination_url: str,
    source_type: str,
) -> JobRecord:
    company_id = str(company.get("Company ID") or stable_company_id(company))
    description = clean_html(opportunity.get("BriefDescription"))
    location = opportunity_locations(opportunity)
    return JobRecord(
        # Long UKG board URLs can be truncated by the global helper before the
        # query-string ID, so key records with the stable opportunity ID.
        id=make_job_id(company, title, f"ukg-{opportunity_id}-{company_id}"),
        companyId=company_id,
        companyName=str(company.get("Company Name") or ""),
        title=title,
        location="; ".join(location) or "Not listed",
        workType=ukg_work_type(opportunity),
        postedDate=clean_text(opportunity.get("PostedDate")),
        sourceUrl=destination_url,
        jobPlatform="UKG",
        description=description,
        descriptionSnippet=description[:360],
        collectedAt=datetime.now(timezone.utc).isoformat(),
        rawData={
            "collector": UKGCollector.__name__,
            "structuredSource": True,
            "sourceType": source_type,
            "opportunityId": opportunity_id,
            "requisitionNumber": clean_text(opportunity.get("RequisitionNumber")),
            "jobCategoryName": clean_text(opportunity.get("JobCategoryName")),
            "fullTime": opportunity.get("FullTime"),
            "jobLocationType": opportunity.get("JobLocationType"),
        },
    )


def opportunity_locations(opportunity: dict[str, Any]) -> list[str]:
    locations = opportunity.get("Locations")
    if not isinstance(locations, list):
        return []
    values: list[str] = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        name = clean_text(location.get("LocalizedName") or location.get("LocalizedDescription"))
        address = location.get("Address")
        city_state = ""
        if isinstance(address, dict):
            city = clean_text(address.get("City"))
            state_value = address.get("State")
            state = clean_text(state_value.get("Code")) if isinstance(state_value, dict) else ""
            city_state = ", ".join(value for value in (city, state) if value)
        value = " — ".join(part for part in (name, city_state) if part) or city_state
        if value and value not in values:
            values.append(value)
    return values


def ukg_work_type(opportunity: dict[str, Any]) -> str:
    # Values are the public UKG JobLocationType enumeration displayed by the board.
    value = opportunity.get("JobLocationType")
    if value == 0:
        return "Hybrid"
    if value == 1:
        return "Onsite"
    if value == 2:
        return "Remote"
    text = " ".join(
        clean_text(opportunity.get(field))
        for field in ("Title", "BriefDescription")
    ).casefold()
    if "remote" in text:
        return "Remote"
    if "hybrid" in text:
        return "Hybrid"
    return "Not Listed"


def clean_html(value: Any) -> str:
    return clean_text(BeautifulSoup(str(value or ""), "html.parser").get_text(" "))


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())
