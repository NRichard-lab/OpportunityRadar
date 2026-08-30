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


UKG_READY_PAGE_SIZE = 100
UKG_READY_MAX_PAGES = 20
UKG_READY_BOARD_PATH = re.compile(r"^/ta/(?P<tenant>[A-Za-z0-9_-]+)\.(?:careers|jobs)/?$", re.IGNORECASE)


class UKGReadyCollector(BaseCollector):
    """Collect UKG Ready (formerly SaaShr/Kronos) public requisitions."""

    requires_browser = False

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        source_url, source_type = self.source_url(company)
        if not source_url:
            return []
        board_url, endpoint, tenant = build_ukg_ready_urls(source_url)
        jobs: list[JobRecord] = []
        seen_ids: set[str] = set()
        expected_total: int | None = None
        fetched = 0

        try:
            for page_number in range(UKG_READY_MAX_PAGES):
                offset = page_number * UKG_READY_PAGE_SIZE + 1
                payload = self.fetch_page(endpoint, offset)
                postings = payload.get("job_requisitions")
                paging = payload.get("_paging")
                if not isinstance(postings, list) or not isinstance(paging, dict):
                    raise ValueError("UKG Ready response is missing requisitions or paging metadata")
                total = paging.get("total")
                if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                    raise ValueError("UKG Ready response is missing a valid total")
                if expected_total is None:
                    expected_total = total
                    if total > UKG_READY_PAGE_SIZE * UKG_READY_MAX_PAGES:
                        raise ValueError("UKG Ready total exceeds the collection safety limit")
                elif total != expected_total:
                    raise ValueError("UKG Ready total changed during pagination")

                if not postings:
                    if expected_total == 0 and fetched == 0:
                        return []
                    raise ValueError(f"UKG Ready returned an empty page after {fetched} of {expected_total} jobs")

                fetched += len(postings)
                if fetched > expected_total:
                    raise ValueError("UKG Ready returned more jobs than its reported total")
                for posting in postings:
                    if not isinstance(posting, dict):
                        raise ValueError("UKG Ready returned a malformed requisition")
                    requisition_id = clean_text(posting.get("id"))
                    title = normalize_job_title(clean_text(posting.get("job_title")))
                    destination_url = build_destination_url(board_url, requisition_id)
                    self.record_candidate(title or f"UKG Ready requisition {requisition_id}", destination_url)
                    if not requisition_id:
                        self.reject_candidate(
                            title or "UKG Ready requisition without an ID",
                            "missing UKG Ready requisition ID",
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        raise ValueError("UKG Ready returned a requisition without an ID")
                    if requisition_id in seen_ids:
                        self.reject_candidate(
                            title,
                            "duplicate UKG Ready requisition ID",
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        continue
                    seen_ids.add(requisition_id)
                    if not is_valid_structured_job_title(title):
                        self.reject_candidate(
                            title,
                            rejection_reason(title),
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        continue

                    description = html_to_text(posting.get("job_description"))
                    pay_min = numeric_value(posting.get("base_pay_from"))
                    pay_max = numeric_value(posting.get("base_pay_to"))
                    pay_period = ukg_ready_pay_period(posting.get("base_pay_frequency"))
                    pay_text = ukg_ready_pay_text(pay_min, pay_max, pay_period)
                    if pay_text:
                        self.record_pay_extraction(
                            "UKG Ready requisition fields",
                            pay_text,
                            {
                                "payText": pay_text,
                                "payMin": pay_min,
                                "payMax": pay_max,
                                "payPeriod": pay_period,
                                "payPatternMatched": "UKG Ready base_pay fields",
                            },
                        )
                    company_id = str(company.get("Company ID") or stable_company_id(company))
                    jobs.append(
                        JobRecord(
                            id=make_job_id(company, title, f"ukg-ready-{tenant}-{requisition_id}-{company_id}"),
                            companyId=company_id,
                            companyName=str(company.get("Company Name") or ""),
                            title=title,
                            location=ukg_ready_location(posting.get("location")) or "Not listed",
                            workType=ukg_ready_work_type(posting),
                            payMin=pay_min,
                            payMax=pay_max,
                            payText=pay_text,
                            payPeriod=pay_period,
                            payCurrency="USD",
                            sourceUrl=destination_url,
                            jobPlatform="UKG Ready",
                            description=description,
                            descriptionSnippet=description[:360],
                            collectedAt=datetime.now(timezone.utc).isoformat(),
                            rawData={
                                "collector": self.__class__.__name__,
                                "structuredSource": True,
                                "sourceType": source_type,
                                "requisitionId": requisition_id,
                                "employeeType": clean_text(
                                    posting.get("employee_type", {}).get("name")
                                    if isinstance(posting.get("employee_type"), dict)
                                    else ""
                                ),
                                "tenant": tenant,
                            },
                        )
                    )
                    self.save_candidate(title, destination_url)

                if fetched == expected_total:
                    return jobs
                if len(postings) < UKG_READY_PAGE_SIZE:
                    raise ValueError("UKG Ready pagination ended before its reported total")
        finally:
            self.final_url_after_redirect = board_url
            self.flush_debug(company)

        raise ValueError("UKG Ready pagination reached its safety limit before completion")

    def fetch_page(self, endpoint: str, offset: int) -> dict[str, Any]:
        time.sleep(max(0, self.delay_seconds))
        response = self.session.get(
            endpoint,
            params={
                "offset": offset,
                "size": UKG_READY_PAGE_SIZE,
                "sort": "desc",
                "ein_id": "",
                "lang": "en-US",
            },
            headers={"Accept": "application/json"},
            timeout=20,
            allow_redirects=True,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("UKG Ready API returned a non-object response")
        return payload


def build_ukg_ready_urls(url: str) -> tuple[str, str, str]:
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not (host == "saashr.com" or host.endswith(".saashr.com"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise ValueError("UKG Ready collector requires an HTTPS saashr.com board URL")
    match = UKG_READY_BOARD_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("UKG Ready collector requires a tenant careers URL")
    tenant = match.group("tenant")
    board_url = urlunsplit(("https", parsed.netloc, f"/ta/{tenant}.careers", "lang=en-US", ""))
    endpoint = urlunsplit(
        ("https", parsed.netloc, f"/ta/rest/ui/recruitment/companies/%7C{quote(tenant, safe='')}/job-requisitions", "", "")
    )
    return board_url, endpoint, tenant


def build_destination_url(board_url: str, requisition_id: str) -> str:
    parsed = urlsplit(board_url)
    query = f"ShowJob={quote(requisition_id, safe='')}&lang=en-US"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def numeric_value(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def ukg_ready_location(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return ", ".join(
        clean_text(value.get(key))
        for key in ("city", "state", "country")
        if clean_text(value.get(key))
    )


def ukg_ready_pay_period(value: Any) -> str:
    lowered = clean_text(value).casefold()
    return {"hour": "hour", "year": "year", "month": "month", "week": "week"}.get(lowered, "unknown")


def ukg_ready_pay_text(pay_min: float | None, pay_max: float | None, period: str) -> str:
    values = [value for value in (pay_min, pay_max) if value is not None]
    if not values:
        return ""
    amount = f"${values[0]:,.2f}" if len(values) == 1 else f"${values[0]:,.2f} - ${values[1]:,.2f}"
    return f"{amount} per {period}" if period != "unknown" else amount


def ukg_ready_work_type(posting: dict[str, Any]) -> str:
    if posting.get("is_remote_job") is True:
        return "Remote"
    text = f"{posting.get('job_title', '')} {posting.get('job_description', '')}".casefold()
    if "hybrid" in text:
        return "Hybrid"
    if "on-site" in text or "onsite" in text:
        return "Onsite"
    return "Not Listed"


def html_to_text(value: Any) -> str:
    return clean_text(BeautifulSoup(str(value or ""), "html.parser").get_text(" "))


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())
