from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from collectors.base import BaseCollector
from excel_tools import stable_company_id
from job_tools import JobRecord, make_job_id
from job_validation import is_valid_structured_job_title, normalize_job_title, rejection_reason


HRMDIRECT_DOMAIN = "hrmdirect.com"
HRMDIRECT_LISTING_PATH = "/employment/job-openings.php"
HRMDIRECT_DETAIL_PATH = "/employment/job-opening.php"
HRMDIRECT_ID = re.compile(r"^[1-9]\d{0,18}$")
HRMDIRECT_TENANT_HOST = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.hrmdirect\.com$"
)
FACET_COUNT = re.compile(
    r"(?:-|\u2013|\u2014)\s*([0-9][0-9,]*)\s+Jobs?\s*$",
    re.IGNORECASE,
)
PAGINATION_QUERY_KEYS = {"page", "pagenum", "page_num", "start", "offset"}
MAX_HRMDIRECT_JOBS = 5000


class ClearCompanyCollector(BaseCollector):
    """Collect a legacy ClearCompany board served from a tenant HRMDirect host."""

    requires_browser = False

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        source_url, source_type = self.source_url(company)
        if not source_url:
            return []

        board_url = validate_hrmdirect_board_url(source_url)
        listing_url = board_url
        try:
            response = self.get(board_url)
            listing_url = validate_hrmdirect_board_url(
                str(getattr(response, "url", "") or board_url)
            )
            self.final_url_after_redirect = listing_url
            listings = parse_hrmdirect_listing(
                str(getattr(response, "text", "") or ""),
                listing_url,
            )

            jobs: list[JobRecord] = []
            seen_requisition_ids: set[str] = set()
            for listing in listings:
                requisition_id = str(listing["requisitionId"])
                title = normalize_job_title(str(listing["title"]))
                destination_url = str(listing["sourceUrl"])
                self.record_candidate(
                    title or f"HRMDirect requisition {requisition_id}",
                    destination_url,
                )

                if requisition_id in seen_requisition_ids:
                    self.reject_candidate(
                        title or f"HRMDirect requisition {requisition_id}",
                        "duplicate HRMDirect requisition ID",
                        destination_url,
                        company=company,
                        job_board_url=listing_url,
                    )
                    raise ValueError(
                        f"HRMDirect listing contained duplicate requisition ID {requisition_id}"
                    )
                seen_requisition_ids.add(requisition_id)

                if not title:
                    self.reject_candidate(
                        f"HRMDirect requisition {requisition_id}",
                        "blank title",
                        destination_url,
                        company=company,
                        job_board_url=listing_url,
                    )
                    raise ValueError(
                        "HRMDirect listing contained a requisition without a title"
                    )
                if not is_valid_structured_job_title(title):
                    self.reject_candidate(
                        title,
                        rejection_reason(title),
                        destination_url,
                        surrounding_text=str(listing.get("rowText") or ""),
                        company=company,
                        job_board_url=listing_url,
                    )
                    continue

                detail: dict[str, str] = {}
                detail_retrieved = False
                try:
                    detail_response = self.get(destination_url)
                    validate_hrmdirect_detail_url(
                        str(
                            getattr(detail_response, "url", "")
                            or destination_url
                        ),
                        expected_host=urlsplit(listing_url).hostname or "",
                        expected_requisition_id=requisition_id,
                        expected_location_id=str(listing.get("locationId") or ""),
                    )
                    detail = parse_hrmdirect_detail(
                        str(getattr(detail_response, "text", "") or "")
                    )
                    detail_title = normalize_job_title(detail.get("title", ""))
                    if detail_title and detail_title.casefold() != title.casefold():
                        raise ValueError(
                            "HRMDirect detail title did not match its listing record"
                        )
                    if not detail.get("description") and not detail.get("location"):
                        raise ValueError(
                            "HRMDirect detail page did not contain recognizable job content"
                        )
                    detail_retrieved = True
                except Exception as exc:
                    if self.debug:
                        self.debug_lines.append(
                            f"DETAIL_FAILED\t{requisition_id}\t"
                            f"{type(exc).__name__}: {exc}"
                        )

                job = build_hrmdirect_job(
                    company,
                    listing,
                    detail,
                    source_type,
                    detail_retrieved,
                )
                jobs.append(job)
                self.save_candidate(title, destination_url)

            if len(seen_requisition_ids) != len(listings):
                raise ValueError(
                    "HRMDirect listing did not contain the expected number of unique requisitions"
                )
            return jobs
        finally:
            # Diagnostics should identify the authoritative board, not the last detail URL.
            self.final_url_after_redirect = listing_url
            self.flush_debug(company)


def validate_hrmdirect_board_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    validate_hrmdirect_authority(parsed, host, "job board")
    if parsed.path.casefold() != HRMDIRECT_LISTING_PATH:
        raise ValueError(
            "ClearCompany collector requires an HRMDirect "
            "job-openings.php listing URL"
        )
    return urlunsplit(("https", host, HRMDIRECT_LISTING_PATH, "search=true", ""))


def validate_hrmdirect_detail_url(
    url: str,
    *,
    expected_host: str,
    expected_requisition_id: str,
    expected_location_id: str = "",
) -> str:
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    validate_hrmdirect_authority(parsed, host, "job detail")
    if host != str(expected_host or "").casefold().rstrip("."):
        raise ValueError("HRMDirect detail URL changed tenant hosts")
    if parsed.path.casefold() != HRMDIRECT_DETAIL_PATH:
        raise ValueError("HRMDirect detail URL did not use job-opening.php")

    parameters: dict[str, str] = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key in parameters:
            raise ValueError(
                "HRMDirect detail URL contained a duplicate query parameter"
            )
        parameters[key] = value
    if set(parameters) - {"req", "req_loc"}:
        raise ValueError("HRMDirect detail URL contained unexpected query parameters")
    requisition_id = parameters.get("req", "")
    location_id = parameters.get("req_loc", "")
    if not HRMDIRECT_ID.fullmatch(requisition_id):
        raise ValueError(
            "HRMDirect detail URL is missing a numeric requisition ID"
        )
    if requisition_id != expected_requisition_id:
        raise ValueError(
            "HRMDirect detail requisition ID did not match its listing row"
        )
    if location_id and not HRMDIRECT_ID.fullmatch(location_id):
        raise ValueError("HRMDirect detail URL contained an invalid location ID")
    if expected_location_id and location_id != expected_location_id:
        raise ValueError(
            "HRMDirect detail location ID did not match its listing row"
        )

    query_items = [("req", requisition_id)]
    if location_id:
        query_items.append(("req_loc", location_id))
    return urlunsplit(
        ("https", host, HRMDIRECT_DETAIL_PATH, urlencode(query_items), "job")
    )


def validate_hrmdirect_authority(parsed: Any, host: str, label: str) -> None:
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"HRMDirect {label} URL is malformed") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not HRMDIRECT_TENANT_HOST.fullmatch(host)
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(
            "ClearCompany collector requires an HTTPS "
            f"tenant.{HRMDIRECT_DOMAIN} {label} URL"
        )


def parse_hrmdirect_listing(html: str, board_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(str(html or ""), "html.parser")
    search_form = soup.select_one("form[action*='job-openings.php']")
    if search_form is None:
        raise ValueError(
            "HRMDirect listing did not contain its public search form"
        )
    if has_hrmdirect_pagination(soup):
        raise ValueError(
            "HRMDirect listing exposed pagination controls that this collector "
            "cannot verify"
        )

    rows = soup.select("tr[data-req-id]")
    if len(rows) > MAX_HRMDIRECT_JOBS:
        raise ValueError("HRMDirect listing exceeded its collection safety limit")
    facet_total = parse_hrmdirect_facet_total(soup)
    if not rows:
        empty_node = soup.select_one("#noOpeningsMsg, #noResultsMsg")
        empty_text = (
            clean_text(empty_node.get_text(" ", strip=True))
            if empty_node
            else ""
        )
        if empty_node is None or "no" not in empty_text.casefold():
            raise ValueError(
                "HRMDirect listing returned no rows without an explicit empty result"
            )
        if facet_total not in {None, 0}:
            raise ValueError(
                "HRMDirect listing reported facet jobs with an empty result table"
            )
        return []
    if facet_total is None:
        raise ValueError(
            "HRMDirect listing did not expose facet totals needed to verify "
            "completeness"
        )
    if len(rows) != facet_total:
        raise ValueError(
            f"HRMDirect listing returned {len(rows)} rows but its facets "
            f"reported {facet_total} jobs"
        )

    listings: list[dict[str, str]] = []
    for row in rows:
        requisition_id = clean_text(row.get("data-req-id"))
        if not HRMDIRECT_ID.fullmatch(requisition_id):
            raise ValueError(
                "HRMDirect listing row did not contain a valid numeric "
                "requisition ID"
            )
        title_cell = row.select_one("td.posTitle")
        anchor = title_cell.select_one("a[href]") if title_cell else None
        title = (
            clean_text(title_cell.get_text(" ", strip=True))
            if title_cell
            else ""
        )
        if anchor is None:
            raise ValueError(
                f"HRMDirect requisition {requisition_id} did not contain "
                "a detail link"
            )
        raw_detail_url = urljoin(board_url, str(anchor.get("href") or ""))
        source_url = validate_hrmdirect_detail_url(
            raw_detail_url,
            expected_host=urlsplit(board_url).hostname or "",
            expected_requisition_id=requisition_id,
        )
        query = dict(parse_qsl(urlsplit(source_url).query))
        city = cell_text(row, "td.cities")
        state = cell_text(row, "td.state")
        country = cell_text(row, "td.countries")
        location = ", ".join(
            value for value in (city, state, country) if value
        )
        listings.append(
            {
                "requisitionId": requisition_id,
                "locationId": query.get("req_loc", ""),
                "title": title,
                "department": cell_text(row, "td.departments"),
                "location": location,
                "sourceUrl": source_url,
                "rowText": clean_text(row.get_text(" ", strip=True)),
            }
        )
    return listings


def parse_hrmdirect_facet_total(soup: BeautifulSoup) -> int | None:
    totals: list[int] = []
    for select in soup.select("form[action*='job-openings.php'] select"):
        counts: list[int] = []
        options = [
            option
            for option in select.find_all("option")
            if clean_text(option.get("value")) != "-1"
        ]
        for option in options:
            match = FACET_COUNT.search(
                clean_text(option.get_text(" ", strip=True))
            )
            if match is None:
                raise ValueError(
                    "HRMDirect listing contained a facet without a job count"
                )
            counts.append(int(match.group(1).replace(",", "")))
        if counts:
            totals.append(sum(counts))
    if not totals:
        return None
    if len(set(totals)) != 1:
        raise ValueError("HRMDirect listing facet totals disagreed")
    total = totals[0]
    if total > MAX_HRMDIRECT_JOBS:
        raise ValueError(
            "HRMDirect facet total exceeded the collection safety limit"
        )
    return total


def has_hrmdirect_pagination(soup: BeautifulSoup) -> bool:
    if soup.select_one(
        "a[rel~='next'], .pagination, .pager, "
        "[class*='pagination'], [id*='pagination']"
    ):
        return True
    for anchor in soup.select("a[href]"):
        keys = {
            key.casefold()
            for key, _value in parse_qsl(
                urlsplit(str(anchor.get("href") or "")).query,
                keep_blank_values=True,
            )
        }
        if keys & PAGINATION_QUERY_KEYS:
            return True
    return False


def parse_hrmdirect_detail(html: str) -> dict[str, str]:
    soup = BeautifulSoup(str(html or ""), "html.parser")
    page_title = (
        clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    )
    title = re.split(
        r"\s+-\s+Careers\s+At\b",
        page_title,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    fields: dict[str, str] = {}
    for row in soup.select("table.viewFields tr"):
        label_node = row.select_one("td.viewFieldName")
        value_node = row.select_one("td.viewFieldValue")
        if label_node is None or value_node is None:
            continue
        label = clean_text(
            label_node.get_text(" ", strip=True)
        ).rstrip(":").casefold()
        fields[label] = clean_text(value_node.get_text(" ", strip=True))
    description_node = soup.select_one("div.jobDesc")
    description = (
        clean_text(description_node.get_text(" ", strip=True))
        if description_node
        else ""
    )
    return {
        "title": normalize_job_title(title),
        "department": fields.get("department", ""),
        "location": fields.get("location", ""),
        "description": description,
    }


def build_hrmdirect_job(
    company: dict[str, Any],
    listing: dict[str, str],
    detail: dict[str, str],
    source_type: str,
    detail_retrieved: bool,
) -> JobRecord:
    company_id = str(company.get("Company ID") or stable_company_id(company))
    requisition_id = str(listing["requisitionId"])
    title = normalize_job_title(str(listing["title"]))
    description = clean_text(detail.get("description") or title)
    location = clean_text(
        detail.get("location") or listing.get("location") or "Not listed"
    )
    return JobRecord(
        id=make_job_id(
            company,
            title,
            f"hrmdirect-{requisition_id}-{company_id}",
        ),
        companyId=company_id,
        companyName=str(company.get("Company Name") or ""),
        title=title,
        location=location,
        workType=hrmdirect_work_type(title, location, description),
        sourceUrl=str(listing["sourceUrl"]),
        jobPlatform="ClearCompany",
        description=description,
        descriptionSnippet=description[:360],
        collectedAt=datetime.now(timezone.utc).isoformat(),
        rawData={
            "collector": ClearCompanyCollector.__name__,
            "structuredSource": True,
            "sourceType": source_type,
            "hrmDirectRequisitionId": requisition_id,
            "hrmDirectLocationId": str(listing.get("locationId") or ""),
            "department": clean_text(
                detail.get("department") or listing.get("department")
            ),
            "detailRetrieved": detail_retrieved,
        },
    )


def hrmdirect_work_type(title: str, location: str, description: str) -> str:
    text = " ".join((title, location, description)).casefold()
    if "hybrid" in text:
        return "Hybrid"
    if re.search(r"\bremote\b", text):
        return "Remote"
    if any(value in text for value in ("on-site", "onsite", "in-person")):
        return "Onsite"
    return "Not Listed"


def cell_text(row: Any, selector: str) -> str:
    node = row.select_one(selector)
    return clean_text(node.get_text(" ", strip=True)) if node else ""


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())
