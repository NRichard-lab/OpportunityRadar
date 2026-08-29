from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, parse_qs, quote, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from collectors.base import BaseCollector
from excel_tools import stable_company_id
from job_tools import JobRecord, make_job_id
from job_validation import is_valid_job_title, normalize_job_title, rejection_reason


ADP_HOST = "workforcenow.adp.com"
ADP_RECRUITMENT_PATH = "/mdf/recruitment/recruitment.html"
ADP_API_SUFFIX = "/careercenter/public/events/staffing/v1/job-requisitions"
ADP_PAGE_SIZE = 20
ADP_MAX_PAGES = 25
ADP_MAX_REQUISITIONS = 500


class ADPCollector(BaseCollector):
    """Collect public ADP Workforce Now postings through its career-center API."""

    requires_browser = False

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        source_url, source_type = self.source_url(company)
        if not source_url:
            return []

        board_url = self.resolve_embedded_job_board_url(source_url, "ADP Workforce Now")
        api_url, tenant_params = build_adp_api_request(board_url)
        jobs: list[JobRecord] = []
        seen_external_ids: set[str] = set()
        fetched_requisition_count = 0
        next_sequence: int | None = None
        expected_total: int | None = None
        pagination_complete = False

        try:
            for _page_number in range(ADP_MAX_PAGES):
                listing_url = build_api_url(api_url, tenant_params, skip=next_sequence)
                payload = response_json_object(self.get(listing_url))
                requisitions = payload.get("jobRequisitions")
                if not isinstance(requisitions, list):
                    raise ValueError("ADP listing response did not contain a jobRequisitions list")
                meta = payload.get("meta")
                if not isinstance(meta, dict):
                    raise ValueError("ADP listing response did not contain a meta object")
                total = required_nonnegative_integer(meta, "totalNumber")
                expected_total = total
                if not requisitions:
                    if fetched_requisition_count < expected_total:
                        raise ValueError(
                            f"ADP pagination returned an empty page after {fetched_requisition_count} of {expected_total} requisitions"
                        )
                    pagination_complete = True
                    break

                start = required_nonnegative_integer(meta, "startSequence")
                expected_start = next_sequence or 1
                if start != expected_start:
                    raise ValueError(f"ADP page started at sequence {start}; expected {expected_start}")
                if fetched_requisition_count + len(requisitions) > ADP_MAX_REQUISITIONS:
                    raise ValueError("ADP pagination exceeded its requisition safety limit")
                fetched_requisition_count += len(requisitions)

                for requisition in requisitions:
                    if not isinstance(requisition, dict):
                        self.record_candidate("Malformed ADP requisition")
                        self.reject_candidate(
                            "Malformed ADP requisition",
                            "ADP requisition was not an object",
                            company=company,
                            job_board_url=board_url,
                        )
                        raise ValueError("ADP listing contained a malformed requisition")

                    external_id = external_job_id(requisition)
                    title = normalize_job_title(str(requisition.get("requisitionTitle") or ""))
                    destination_url = build_destination_url(board_url, external_id) if external_id else board_url
                    self.record_candidate(title or f"ADP requisition {external_id or 'without an ID'}", destination_url)

                    if not external_id:
                        self.reject_candidate(
                            title or "ADP requisition without an ID",
                            "missing ADP ExternalJobID",
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        raise ValueError("ADP listing contained a requisition without ExternalJobID")
                    if external_id in seen_external_ids:
                        self.reject_candidate(
                            title,
                            "duplicate ADP ExternalJobID",
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        continue
                    seen_external_ids.add(external_id)
                    if not is_valid_job_title(title):
                        self.reject_candidate(
                            title,
                            rejection_reason(title),
                            destination_url,
                            company=company,
                            job_board_url=board_url,
                        )
                        continue

                    detail_url = build_detail_url(api_url, tenant_params, external_id)
                    detail = requisition
                    detail_retrieved = False
                    try:
                        detail_payload = response_json_object(self.get(detail_url))
                        detail = detail_payload.get("jobRequisition", detail_payload)
                        if not isinstance(detail, dict):
                            detail = requisition
                        else:
                            detail_retrieved = True
                    except Exception as exc:
                        if self.debug:
                            self.debug_lines.append(f"DETAIL_FAILED\t{external_id}\t{type(exc).__name__}: {exc}")

                    job = build_job(
                        company,
                        title,
                        destination_url,
                        detail,
                        requisition,
                        source_type,
                        external_id,
                        detail_url,
                        detail_retrieved,
                    )
                    jobs.append(job)
                    if job.payText:
                        self.record_pay_extraction(
                            "ADP structured fields",
                            title,
                            {
                                "payText": job.payText,
                                "payMin": job.payMin,
                                "payMax": job.payMax,
                                "payPeriod": job.payPeriod,
                                "payPatternMatched": "payGradeRange/SalaryRange",
                            },
                        )
                    self.save_candidate(title, destination_url)

                if fetched_requisition_count >= ADP_MAX_REQUISITIONS:
                    pagination_complete = fetched_requisition_count >= expected_total
                    break

                following_sequence = start + len(requisitions)
                if following_sequence - 1 > total:
                    raise ValueError(
                        f"ADP page ended at sequence {following_sequence - 1} beyond reported total {total}"
                    )
                if following_sequence > total:
                    pagination_complete = True
                    break
                if len(requisitions) < ADP_PAGE_SIZE:
                    if following_sequence <= total:
                        raise ValueError(
                            f"ADP pagination ended early after sequence {following_sequence - 1} of {total}"
                        )
                    pagination_complete = True
                    break
                if next_sequence is not None and following_sequence <= next_sequence:
                    raise ValueError("ADP pagination did not advance")
                next_sequence = following_sequence
        finally:
            # Diagnostics should report the user-facing board, not the last API detail URL.
            self.final_url_after_redirect = board_url
            self.flush_debug(company)

        if expected_total is not None and fetched_requisition_count < expected_total:
            raise ValueError(
                f"ADP pagination returned {fetched_requisition_count} requisitions but reported {expected_total}"
            )
        if not pagination_complete:
            raise ValueError("ADP pagination reached its safety limit before completion")
        return dedupe_jobs(jobs)


def build_adp_api_request(board_url: str) -> tuple[str, dict[str, str]]:
    parsed = urlsplit(str(board_url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host != ADP_HOST:
        raise ValueError("ADP collector requires an HTTPS workforcenow.adp.com job board URL")

    marker_index = parsed.path.lower().find(ADP_RECRUITMENT_PATH)
    if marker_index < 0:
        raise ValueError("ADP collector requires a Workforce Now recruitment.html job board URL")
    path_suffix = parsed.path[marker_index + len(ADP_RECRUITMENT_PATH):]
    if path_suffix not in {"", "/"}:
        raise ValueError("ADP Workforce Now job board URL has an unexpected path suffix")
    prefix = parsed.path[:marker_index].rstrip("/")
    if not prefix:
        raise ValueError("ADP Workforce Now job board URL is missing its tenant path")

    query = parse_qs(parsed.query)
    cid = first_query_value(query, "cid")
    cc_id = first_query_value(query, "ccId")
    if not cid or not cc_id:
        raise ValueError("ADP Workforce Now job board URL is missing cid or ccId")
    language = first_query_value(query, "lang") or "en_US"
    tenant_params = {"cid": cid, "ccId": cc_id, "lang": language, "locale": language}
    return urlunsplit(("https", ADP_HOST, f"{prefix}{ADP_API_SUFFIX}", "", "")), tenant_params


def build_api_url(api_url: str, tenant_params: dict[str, str], *, skip: int | None = None) -> str:
    params: list[tuple[str, str | int]] = [*tenant_params.items(), ("$top", ADP_PAGE_SIZE)]
    if skip is not None:
        params.append(("$skip", skip))
    return f"{api_url}?{urlencode(params, safe='$')}"


def build_detail_url(api_url: str, tenant_params: dict[str, str], external_id: str) -> str:
    query = urlencode(list(tenant_params.items()))
    return f"{api_url}/{quote(external_id, safe='')}?{query}"


def build_destination_url(board_url: str, external_id: str) -> str:
    parsed = urlsplit(board_url)
    allowed_names = {"cid": "cid", "ccid": "ccId", "type": "type", "lang": "lang"}
    query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        canonical_name = allowed_names.get(key.casefold())
        if canonical_name and all(existing_key != canonical_name for existing_key, _value in query):
            query.append((canonical_name, value))
    query.append(("jobId", external_id))
    return urlunsplit(("https", ADP_HOST, parsed.path, urlencode(query), ""))


def response_json_object(response: Any) -> dict[str, Any]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("ADP API returned a non-object JSON response")
    return payload


def first_query_value(query: dict[str, list[str]], name: str) -> str:
    for key, values in query.items():
        if key.casefold() == name.casefold() and values:
            return str(values[0]).strip()
    return ""


def external_job_id(requisition: dict[str, Any]) -> str:
    custom_group = requisition.get("customFieldGroup")
    fields = custom_group.get("stringFields") if isinstance(custom_group, dict) else []
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, dict):
            continue
        name_code = field.get("nameCode")
        code = name_code.get("codeValue") if isinstance(name_code, dict) else ""
        if str(code or "").casefold() == "externaljobid":
            value = field.get("stringValue")
            if isinstance(value, dict):
                value = value.get("value") or value.get("codeValue")
            return str(value or "").strip()
    return str(requisition.get("externalJobId") or requisition.get("jobId") or "").strip()


def build_job(
    company: dict[str, Any],
    title: str,
    destination_url: str,
    detail: dict[str, Any],
    listing: dict[str, Any],
    source_type: str,
    external_id: str,
    detail_url: str,
    detail_retrieved: bool,
) -> JobRecord:
    description = clean_html(detail.get("requisitionDescription") or listing.get("requisitionDescription") or "")
    locations = requisition_locations(detail) or requisition_locations(listing)
    work_type = code_short_name(detail.get("workLevelCode")) or code_short_name(listing.get("workLevelCode")) or "Not Listed"
    posted_date = clean_text(str(detail.get("postDate") or listing.get("postDate") or ""))
    pay_text = custom_string_value(detail, "SalaryRange") or custom_string_value(listing, "SalaryRange")
    pay_min, pay_max, pay_currency = structured_pay_range(detail)
    if pay_min is None and pay_max is None:
        pay_min, pay_max, pay_currency = structured_pay_range(listing)
    pay_period = structured_pay_period(detail) or structured_pay_period(listing) or "unknown"
    if not pay_text and (pay_min is not None or pay_max is not None):
        range_values = " - ".join(str(value) for value in (pay_min, pay_max) if value is not None)
        pay_text = clean_text(f"{pay_currency} {range_values} {pay_period}")
    company_id = str(company.get("Company ID") or stable_company_id(company))
    return JobRecord(
        # The board URL is long enough to truncate before jobId in the global
        # helper, so key ADP records by company + ExternalJobID instead.
        id=make_job_id(company, title, f"adp-{external_id}-{company_id}"),
        companyId=company_id,
        companyName=str(company.get("Company Name") or ""),
        title=title,
        location="; ".join(locations),
        workType=work_type,
        payMin=pay_min,
        payMax=pay_max,
        payText=pay_text,
        payPeriod=pay_period,
        payCurrency=pay_currency,
        postedDate=posted_date,
        sourceUrl=destination_url,
        jobPlatform="ADP Workforce Now",
        description=description,
        descriptionSnippet=description[:240],
        collectedAt=datetime.now().astimezone().replace(microsecond=0).isoformat(),
        rawData={
            "collector": ADPCollector.__name__,
            "sourceType": source_type,
            "externalJobId": external_id,
            "itemID": str(listing.get("itemID") or ""),
            "clientRequisitionID": str(listing.get("clientRequisitionID") or ""),
            "detailUrl": detail_url,
            "detailRetrieved": detail_retrieved,
        },
    )


def custom_string_value(requisition: dict[str, Any], name: str) -> str:
    custom_group = requisition.get("customFieldGroup")
    fields = custom_group.get("stringFields") if isinstance(custom_group, dict) else []
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, dict):
            continue
        name_code = field.get("nameCode")
        code = name_code.get("codeValue") if isinstance(name_code, dict) else ""
        if str(code or "").casefold() == name.casefold():
            return clean_text(str(field.get("stringValue") or ""))
    return ""


def structured_pay_range(requisition: dict[str, Any]) -> tuple[float | None, float | None, str]:
    pay_range = requisition.get("payGradeRange")
    if not isinstance(pay_range, dict):
        return None, None, "USD"
    minimum, minimum_currency = amount_and_currency(pay_range.get("minimumRate"))
    maximum, maximum_currency = amount_and_currency(pay_range.get("maximumRate"))
    return minimum, maximum, minimum_currency or maximum_currency or "USD"


def amount_and_currency(value: Any) -> tuple[float | None, str]:
    if not isinstance(value, dict):
        return None, ""
    try:
        amount = float(value.get("amountValue"))
    except (TypeError, ValueError):
        amount = None
    return amount, clean_text(str(value.get("currencyCode") or ""))


def structured_pay_period(requisition: dict[str, Any]) -> str:
    custom_group = requisition.get("customFieldGroup")
    fields = custom_group.get("codeFields") if isinstance(custom_group, dict) else []
    for field in fields if isinstance(fields, list) else []:
        if not isinstance(field, dict):
            continue
        name_code = field.get("nameCode")
        name = name_code.get("codeValue") if isinstance(name_code, dict) else ""
        if str(name or "").casefold() != "salarytype":
            continue
        code = str(field.get("codeValue") or "").casefold()
        label = str(field.get("shortName") or "").casefold()
        if code == "hr" or "hour" in label:
            return "hourly"
        if code == "an" or "annual" in label or "year" in label:
            return "annual"
        if code == "mo" or "month" in label:
            return "monthly"
        if code == "wk" or "week" in label:
            return "weekly"
        return clean_text(str(field.get("shortName") or field.get("codeValue") or "")).lower()
    return ""


def requisition_locations(requisition: dict[str, Any]) -> list[str]:
    raw_locations = requisition.get("requisitionLocations")
    locations: list[str] = []
    for location in raw_locations if isinstance(raw_locations, list) else []:
        if not isinstance(location, dict):
            continue
        value = code_short_name(location.get("nameCode")) or clean_text(str(location.get("name") or ""))
        if value and value not in locations:
            locations.append(value)
    return locations


def code_short_name(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return clean_text(str(value.get("shortName") or value.get("longName") or value.get("codeValue") or ""))


def clean_html(value: Any) -> str:
    return clean_text(BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True))


def clean_text(value: str) -> str:
    return " ".join(str(value or "").split())


def required_nonnegative_integer(payload: dict[str, Any], name: str) -> int:
    if name not in payload or isinstance(payload[name], bool):
        raise ValueError(f"ADP listing meta did not contain a valid {name}")
    try:
        value = int(payload[name])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ADP listing meta did not contain a valid {name}") from exc
    if value < 0:
        raise ValueError(f"ADP listing meta did not contain a valid {name}")
    return value


def dedupe_jobs(jobs: list[JobRecord]) -> list[JobRecord]:
    unique: dict[str, JobRecord] = {}
    for job in jobs:
        unique[job.sourceUrl or job.id] = job
    return list(unique.values())
