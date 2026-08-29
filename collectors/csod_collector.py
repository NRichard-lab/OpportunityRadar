from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from collectors.base import BaseCollector
from excel_tools import stable_company_id
from job_platforms import hostname_matches_domain
from job_tools import JobRecord, make_job_id
from job_validation import is_valid_structured_job_title, normalize_job_title, rejection_reason


CSOD_PAGE_SIZE = 25
CSOD_MAX_PAGES = 40
CSOD_MAX_REQUISITIONS = CSOD_PAGE_SIZE * CSOD_MAX_PAGES
CSOD_BOARD_PATH = re.compile(
    r"^/ux/ats/careersite/(?P<site>[1-9]\d*)/home"
    r"(?:/requisition/[1-9]\d*)?/?$",
    flags=re.IGNORECASE,
)
CSOD_ROUTE_ASSIGNMENT = re.compile(r"\b(?:var\s+)?csodPlayerRouteInfo\s*=\s*")
CSOD_CONTEXT_ASSIGNMENT = re.compile(r"\bcsod\.context\s*=\s*")
CSOD_CORP = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CSOD_CULTURE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2})?$")
CSOD_TOKEN = re.compile(r"^[A-Za-z0-9._-]{20,8192}$")
CSOD_SEARCH_PATH = "/rec-job-search/external/jobs"


@dataclass(frozen=True)
class CSODBootstrap:
    board_url: str
    tenant_host: str
    corp: str
    site_id: int
    culture_id: int
    culture_name: str
    search_url: str
    token: str = field(repr=False)


class CSODCollector(BaseCollector):
    """Collect public Cornerstone career-site requisitions through its search API."""

    requires_browser = False

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        source_url, source_type = self.source_url(company)
        if not source_url:
            return []

        embedded_url = self.resolve_embedded_job_board_url(source_url, "Cornerstone")
        board_url, _host, _site_id, _corp = normalize_csod_board_url(embedded_url)
        final_board_url = board_url
        jobs: list[JobRecord] = []
        seen_requisition_ids: set[str] = set()
        fetched_count = 0
        expected_total: int | None = None
        pagination_complete = False

        try:
            bootstrap_response = self.get(board_url)
            final_board_url, _host, _site_id, _corp = normalize_csod_board_url(
                str(getattr(bootstrap_response, "url", "") or board_url)
            )
            config = parse_csod_bootstrap(
                str(getattr(bootstrap_response, "text", "") or ""),
                final_board_url,
            )

            for page_number in range(1, CSOD_MAX_PAGES + 1):
                payload = self.fetch_search_page(config, page_number)
                total, requisitions = parse_csod_search_response(payload)

                if expected_total is None:
                    expected_total = total
                    if expected_total > CSOD_MAX_REQUISITIONS:
                        raise ValueError(
                            f"Cornerstone reported {expected_total} requisitions, above the collection safety limit"
                        )
                elif total != expected_total:
                    raise ValueError(
                        f"Cornerstone total changed during pagination from {expected_total} to {total}"
                    )

                if len(requisitions) > CSOD_PAGE_SIZE:
                    raise ValueError("Cornerstone returned more requisitions than the requested page size")
                if not requisitions:
                    if expected_total == 0 and fetched_count == 0:
                        pagination_complete = True
                        break
                    raise ValueError(
                        f"Cornerstone returned an empty page after {fetched_count} of {expected_total} requisitions"
                    )

                fetched_count += len(requisitions)
                if fetched_count > expected_total:
                    raise ValueError(
                        f"Cornerstone returned {fetched_count} requisitions but reported only {expected_total}"
                    )

                for requisition in requisitions:
                    if not isinstance(requisition, dict):
                        raise ValueError("Cornerstone returned a malformed requisition")
                    requisition_id = csod_requisition_id(requisition.get("requisitionId"))
                    title = normalize_job_title(clean_text(requisition.get("displayJobTitle")))
                    destination_url = (
                        build_csod_destination_url(config, requisition_id)
                        if requisition_id
                        else final_board_url
                    )
                    self.record_candidate(
                        title or f"Cornerstone requisition {requisition_id or 'without an ID'}",
                        destination_url,
                    )

                    if not requisition_id:
                        self.reject_candidate(
                            title or "Cornerstone requisition without an ID",
                            "missing or invalid Cornerstone requisitionId",
                            destination_url,
                            company=company,
                            job_board_url=final_board_url,
                        )
                        raise ValueError(
                            "Cornerstone listing contained a requisition without a valid requisitionId"
                        )
                    if requisition_id in seen_requisition_ids:
                        self.reject_candidate(
                            title or f"Cornerstone requisition {requisition_id}",
                            "duplicate Cornerstone requisitionId",
                            destination_url,
                            company=company,
                            job_board_url=final_board_url,
                        )
                        continue
                    seen_requisition_ids.add(requisition_id)

                    if not is_valid_structured_job_title(title):
                        self.reject_candidate(
                            title,
                            rejection_reason(title),
                            destination_url,
                            company=company,
                            job_board_url=final_board_url,
                        )
                        continue

                    job = build_csod_job(
                        company,
                        requisition,
                        requisition_id,
                        title,
                        destination_url,
                        source_type,
                        config,
                    )
                    jobs.append(job)
                    self.save_candidate(title, destination_url)

                if fetched_count == expected_total:
                    pagination_complete = True
                    break
                if len(requisitions) < CSOD_PAGE_SIZE:
                    raise ValueError(
                        f"Cornerstone pagination ended early after {fetched_count} of {expected_total} requisitions"
                    )
        finally:
            self.final_url_after_redirect = final_board_url
            self.flush_debug(company)

        if expected_total is None:
            raise ValueError("Cornerstone pagination returned no totalCount")
        if fetched_count != expected_total:
            raise ValueError(
                f"Cornerstone pagination returned {fetched_count} requisitions but reported {expected_total}"
            )
        if len(seen_requisition_ids) != expected_total:
            raise ValueError(
                f"Cornerstone pagination returned {len(seen_requisition_ids)} unique requisitions "
                f"but reported {expected_total}"
            )
        if not pagination_complete:
            raise ValueError("Cornerstone pagination reached its safety limit before completion")
        return jobs

    def fetch_search_page(self, config: CSODBootstrap, page_number: int) -> dict[str, Any]:
        time.sleep(max(0, self.delay_seconds))
        response = self.session.post(
            config.search_url,
            json=build_csod_search_payload(config, page_number),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {config.token}",
                "CSOD-Accept-Language": config.culture_name,
                "Origin": f"https://{config.tenant_host}",
                "Referer": config.board_url,
            },
            timeout=20,
            allow_redirects=True,
        )
        response.raise_for_status()
        validate_csod_search_url(str(getattr(response, "url", "") or config.search_url))
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Cornerstone search endpoint returned a non-object JSON response")
        return payload


def normalize_csod_board_url(board_url: str) -> tuple[str, str, int, str]:
    parsed = urlsplit(str(board_url or "").strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Cornerstone job board URL contains an invalid port") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not hostname_matches_domain(host, "csod.com")
        or host == "csod.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise ValueError("Cornerstone collector requires an HTTPS tenant.csod.com career-site URL")

    match = CSOD_BOARD_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("Cornerstone collector requires a numeric career-site ID and home route")
    site_id = int(match.group("site"))
    corp = ""
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        if name.casefold() == "c":
            corp = value.strip()
            break
    if not CSOD_CORP.fullmatch(corp):
        raise ValueError("Cornerstone career-site URL is missing a valid c tenant parameter")

    path = f"/ux/ats/careersite/{site_id}/home"
    normalized = urlunsplit(("https", host, path, urlencode({"c": corp}), ""))
    return normalized, host, site_id, corp


def parse_csod_bootstrap(html: str, board_url: str) -> CSODBootstrap:
    board_url, tenant_host, site_id, corp = normalize_csod_board_url(board_url)
    route = parse_json_assignment(html, CSOD_ROUTE_ASSIGNMENT, "csodPlayerRouteInfo")
    context = parse_json_assignment(html, CSOD_CONTEXT_ASSIGNMENT, "csod.context")

    route_site_id = positive_integer(route.get("cid"))
    if route_site_id != site_id:
        raise ValueError("Cornerstone bootstrap career-site ID did not match the board URL")
    context_corp = clean_text(context.get("corp"))
    if context_corp.casefold() != corp.casefold():
        raise ValueError("Cornerstone bootstrap tenant did not match the board URL")
    culture_id = positive_integer(context.get("cultureID"))
    culture_name = clean_text(context.get("cultureName"))
    if culture_id is None or not CSOD_CULTURE.fullmatch(culture_name):
        raise ValueError("Cornerstone bootstrap did not contain a valid public culture")
    token = str(context.get("token") or "")
    if not CSOD_TOKEN.fullmatch(token):
        raise ValueError("Cornerstone bootstrap did not contain a valid anonymous token")
    endpoints = context.get("endpoints")
    cloud_base = clean_text(endpoints.get("cloud")) if isinstance(endpoints, dict) else ""
    search_url = build_csod_search_url(cloud_base)
    return CSODBootstrap(
        board_url=board_url,
        tenant_host=tenant_host,
        corp=corp,
        site_id=site_id,
        culture_id=culture_id,
        culture_name=culture_name,
        search_url=search_url,
        token=token,
    )


def parse_json_assignment(html: str, assignment: re.Pattern[str], name: str) -> dict[str, Any]:
    marker = assignment.search(str(html or ""))
    if marker is None:
        raise ValueError(f"Cornerstone board did not expose {name}")
    try:
        value, _end = json.JSONDecoder().raw_decode(html, marker.end())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Cornerstone board exposed malformed {name} JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Cornerstone board {name} was not an object")
    return value


def build_csod_search_url(cloud_base: str) -> str:
    parsed = urlsplit(str(cloud_base or "").strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Cornerstone bootstrap exposed an invalid cloud API port") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not hostname_matches_domain(host, "api.csod.com")
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Cornerstone bootstrap exposed an unsupported cloud API origin")
    return urlunsplit(("https", host, CSOD_SEARCH_PATH, "", ""))


def validate_csod_search_url(search_url: str) -> None:
    parsed = urlsplit(str(search_url or "").strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Cornerstone search response URL contained an invalid port") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not hostname_matches_domain(host, "api.csod.com")
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path.rstrip("/").casefold() != CSOD_SEARCH_PATH.casefold()
    ):
        raise ValueError("Cornerstone search response redirected outside the public jobs endpoint")


def build_csod_search_payload(config: CSODBootstrap, page_number: int) -> dict[str, Any]:
    if page_number < 1:
        raise ValueError("Cornerstone pageNumber is one-based")
    return {
        "careerSiteId": config.site_id,
        "careerSitePageId": config.site_id,
        "pageNumber": page_number,
        "pageSize": CSOD_PAGE_SIZE,
        "cultureId": config.culture_id,
        "searchText": "",
        "cultureName": config.culture_name,
        "states": [],
        "countryCodes": [],
        "cities": [],
        "placeID": "",
        "radius": None,
        "postingsWithinDays": None,
        "customFieldCheckboxKeys": [],
        "customFieldDropdowns": [],
        "customFieldRadios": [],
    }


def parse_csod_search_response(payload: dict[str, Any]) -> tuple[int, list[Any]]:
    if clean_text(payload.get("status")).casefold() != "success":
        raise ValueError("Cornerstone search response did not report Success")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Cornerstone search response is missing its data object")
    total = data.get("totalCount")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("Cornerstone search response is missing a valid totalCount")
    requisitions = data.get("requisitions")
    if not isinstance(requisitions, list):
        raise ValueError("Cornerstone search response is missing its requisitions list")
    return total, requisitions


def csod_requisition_id(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    text = str(value or "").strip()
    return text if re.fullmatch(r"[1-9]\d{0,18}", text) else ""


def build_csod_destination_url(config: CSODBootstrap, requisition_id: str) -> str:
    if not csod_requisition_id(requisition_id):
        raise ValueError("Cornerstone destination requires a numeric requisitionId")
    path = f"/ux/ats/careersite/{config.site_id}/home/requisition/{quote(requisition_id, safe='')}"
    return urlunsplit(("https", config.tenant_host, path, urlencode({"c": config.corp}), ""))


def build_csod_job(
    company: dict[str, Any],
    requisition: dict[str, Any],
    requisition_id: str,
    title: str,
    destination_url: str,
    source_type: str,
    config: CSODBootstrap,
) -> JobRecord:
    company_id = str(company.get("Company ID") or stable_company_id(company))
    description = html_to_text(requisition.get("externalDescription"))
    locations, has_specific_location = csod_locations(requisition.get("locations"))
    work_type = csod_work_type(title, description, has_specific_location)
    location = "; ".join(locations)
    if work_type == "Remote" and not has_specific_location:
        location = "Remote"
    return JobRecord(
        id=make_job_id(company, title, f"csod-{requisition_id}-{company_id}"),
        companyId=company_id,
        companyName=str(company.get("Company Name") or ""),
        title=title,
        location=location or "Not listed",
        workType=work_type,
        postedDate=clean_text(requisition.get("postingEffectiveDate")),
        sourceUrl=destination_url,
        jobPlatform="Cornerstone",
        description=description,
        descriptionSnippet=description[:360],
        collectedAt=datetime.now(timezone.utc).isoformat(),
        rawData={
            "collector": CSODCollector.__name__,
            "structuredSource": True,
            "sourceType": source_type,
            "requisitionId": requisition_id,
            "postingExpirationDate": clean_text(requisition.get("postingExpirationDate")),
            "careerSiteId": config.site_id,
            "corp": config.corp,
            "cultureName": config.culture_name,
        },
    )


def csod_locations(value: Any) -> tuple[list[str], bool]:
    locations: list[str] = []
    has_specific_location = False
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        city = clean_text(item.get("city"))
        state = clean_text(item.get("state"))
        country = clean_text(item.get("country"))
        has_specific_location = has_specific_location or bool(city or state)
        location = ", ".join(part for part in (city, state, country) if part)
        if location and location not in locations:
            locations.append(location)
    return locations, has_specific_location


def csod_work_type(title: str, description: str, has_specific_location: bool) -> str:
    text = f"{title} {description}".casefold()
    if re.search(r"\bhybrid\b", text):
        return "Hybrid"
    if re.search(r"\bremote(?:ly)?\b", text):
        return "Remote"
    if re.search(r"\b(?:on-site|onsite|in-person)\b", text) or has_specific_location:
        return "Onsite"
    return "Not Listed"


def positive_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def html_to_text(value: Any) -> str:
    return clean_text(BeautifulSoup(str(value or ""), "html.parser").get_text(" "))


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())
