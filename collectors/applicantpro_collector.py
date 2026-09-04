"""Collectors for ApplicantPro and isolved Hire public career sites.

The two products are one platform. An isolved Hire board serves the same Vue
career-site bundle (``applicant-pro-components.js``) from ``isolvedhire.com``
that an ApplicantPro board serves from ``applicantpro.com``, and both expose the
same public listing endpoint. Only the vendor domain and the reported platform
name differ, so both collectors share one implementation and differ by class
attribute.

Board page
    ``https://<tenant>.<vendor>/jobs/``. ``https://www.applicantpro.com/openings/
    <tenant>/jobs/`` is an alias that redirects to the tenant subdomain.

Tenant metadata
    The board page embeds a ``componentData`` literal carrying ``organizationId``,
    ``domainId``, ``subdomainName`` and ``domainName``. ``domainId`` is the key
    the listing endpoint is addressed by; it is never guessed.

Listing endpoint
    ``GET https://<tenant>.<vendor>/core/jobs/<domainId>?getParams={"isInternal":0}``
    returns ``{"success":true,"data":{"jobs":[...],"jobCount":N,...}}``. There is
    no pagination: one response carries the whole board, and ``jobCount`` is the
    board's own declared total, so ``len(jobs) == jobCount`` proves completeness.

Tenant isolation
    The endpoint is keyed *only* by ``domainId`` -- the host subdomain is ignored.
    Requesting another tenant's ``domainId`` from this tenant's host returns that
    other tenant's postings with a 200. Every record therefore has to be checked
    against the resolved tenant before it is stored, and any mismatch discards the
    whole response rather than importing a foreign board.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import requests

from collectors.base import BaseCollector
from excel_tools import stable_company_id
from job_platforms import hostname_matches_domain
from job_tools import CollectionNotAuthoritative, JobRecord, make_job_id
from job_validation import is_valid_structured_job_title, normalize_job_title, rejection_reason


APPLICANTPRO_DOMAIN = "applicantpro.com"
ISOLVED_HIRE_DOMAIN = "isolvedhire.com"

# The public board asks for external postings only. ``/internaljobs*`` is
# disallowed by both vendors' robots.txt, and an internal requisition is not a
# public opening.
PUBLIC_BOARD_PARAMS = '{"isInternal":0}'

# A tenant subdomain is a single DNS label. Anything else is either the vendor's
# own marketing site or a shape we have not verified.
TENANT_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

# Vendor-owned hosts that are not a tenant board.
RESERVED_SUBDOMAINS = {"www", "feeds", "api", "admin", "app", "support", "help", "status"}

# The alias form: https://www.applicantpro.com/openings/<tenant>/jobs/
OPENINGS_ALIAS_PATH = re.compile(r"^/openings/(?P<tenant>[^/]+)(?:/|$)", flags=re.IGNORECASE)

# The board page embeds tenant metadata as a JavaScript object literal, so the
# individual values are read by name rather than by parsing the whole literal.
_COMPONENT_DATA_BLOCK = re.compile(r"componentData\s*:\s*\{(?P<body>.{0,4000}?)\}\}", flags=re.DOTALL)
_NUMERIC_FIELD = "{name}\\s*:\\s*(?P<value>\\d+)"
_STRING_FIELD = "{name}\\s*:\\s*\"(?P<value>[^\"]*)\""

# A disabled or unclaimed career site lands here. It is not an empty board: the
# site is switched off, so its openings are unknown.
_INACTIVE_SITE_PATH = "/notset.php"
_INACTIVE_SITE_TEXT = "career site has been disabled"

MAX_BOARD_JOBS = 2000


class ApplicantProFamilyCollector(BaseCollector):
    """Shared implementation for the ApplicantPro career-site platform."""

    requires_browser = False

    vendor_domain = APPLICANTPRO_DOMAIN
    platform_name = "ApplicantPro"

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        source_url, source_type = self.source_url(company)
        if not source_url:
            return []
        board_url = self.resolve_embedded_job_board_url(source_url, self.platform_name)

        tenant = tenant_from_board_url(board_url, self.vendor_domain)
        if not tenant:
            raise CollectionNotAuthoritative(
                f"{board_url} is not a recognized {self.platform_name} tenant board URL; "
                f"refusing to collect."
            )

        canonical_board_url = f"https://{tenant}.{self.vendor_domain}/jobs/"
        try:
            response = self.get(canonical_board_url)
        except requests.RequestException as exc:
            raise CollectionNotAuthoritative(
                f"{self.platform_name} board {canonical_board_url} could not be read "
                f"({describe_request_failure(exc)}); existing jobs are retained."
            ) from exc
        finally:
            self.final_url_after_redirect = canonical_board_url

        landing = str(getattr(response, "url", "") or canonical_board_url)
        inactive = inactive_site_reason(landing, response.text)
        if inactive:
            raise CollectionNotAuthoritative(
                f"{self.platform_name} tenant \"{tenant}\" {inactive}; its openings are "
                f"unknown, so this is not an authoritative zero."
            )
        redirect_problem = redirect_left_the_tenant(landing, tenant, self.vendor_domain)
        if redirect_problem:
            raise CollectionNotAuthoritative(
                f"{canonical_board_url} {redirect_problem}; refusing to collect a board "
                f"that may belong to another tenant."
            )

        identity = parse_tenant_identity(response.text)
        identity_problem = tenant_identity_problem(identity, tenant, self.vendor_domain)
        if identity_problem:
            raise CollectionNotAuthoritative(
                f"{canonical_board_url} {identity_problem}; refusing to collect."
            )

        domain_id = identity["domainId"]
        listing_url = (
            f"https://{tenant}.{self.vendor_domain}/core/jobs/{domain_id}"
            f"?getParams={requests.utils.quote(PUBLIC_BOARD_PARAMS, safe='')}"
        )
        try:
            listing_response = self.get(listing_url)
            payload = listing_response.json()
        except requests.RequestException as exc:
            raise CollectionNotAuthoritative(
                f"{self.platform_name} listing for \"{tenant}\" could not be read "
                f"({describe_request_failure(exc)}); existing jobs are retained."
            ) from exc
        except ValueError as exc:
            raise CollectionNotAuthoritative(
                f"{self.platform_name} listing for \"{tenant}\" did not return JSON; "
                f"existing jobs are retained."
            ) from exc
        finally:
            # Diagnostics should point at the board a person can open, not the API.
            self.final_url_after_redirect = canonical_board_url

        records, declared_count = listing_payload(payload, self.platform_name, tenant)

        jobs: list[JobRecord] = []
        seen_ids: set[str] = set()
        company_id = str(company.get("Company ID") or stable_company_id(company))
        for record in records:
            posting_id = str(record.get("id") or "").strip()
            title = normalize_job_title(str(record.get("title") or ""))
            detail_url = canonical_detail_url(record, tenant, self.vendor_domain, posting_id)
            self.record_candidate(title or f"{self.platform_name} posting {posting_id or 'without an id'}", detail_url)

            foreign = foreign_tenant_reason(record, tenant, self.vendor_domain, domain_id)
            if foreign:
                self.reject_candidate(
                    title, foreign, detail_url, company=company, job_board_url=canonical_board_url
                )
                # One foreign row means the whole response is not this tenant's
                # board. Import none of it.
                raise CollectionNotAuthoritative(
                    f"{self.platform_name} listing for \"{tenant}\" returned a posting "
                    f"from another tenant ({foreign}); the response is discarded."
                )
            if not posting_id:
                self.reject_candidate(
                    title, f"{self.platform_name} posting has no id", detail_url,
                    company=company, job_board_url=canonical_board_url,
                )
                raise CollectionNotAuthoritative(
                    f"{self.platform_name} listing for \"{tenant}\" contained a posting "
                    f"without an id; the response is incomplete."
                )
            if not is_valid_structured_job_title(title):
                self.reject_candidate(
                    title, rejection_reason(title), detail_url,
                    company=company, job_board_url=canonical_board_url,
                )
                continue
            if posting_id in seen_ids:
                self.reject_candidate(
                    title, f"duplicate {self.platform_name} posting id", detail_url,
                    company=company, job_board_url=canonical_board_url,
                )
                continue
            seen_ids.add(posting_id)

            job = self.build_job(company_id, company, record, title, detail_url, source_type, tenant, domain_id)
            jobs.append(job)
            if job.payText:
                self.record_pay_extraction(
                    f"{self.platform_name} structured fields",
                    title,
                    {
                        "payText": job.payText,
                        "payMin": job.payMin,
                        "payMax": job.payMax,
                        "payPeriod": job.payPeriod,
                        "payPatternMatched": "payRate/minSalary/maxSalary",
                    },
                )
            self.save_candidate(title, detail_url)

        self.flush_debug(company)

        # ``jobCount`` is the board's own total. Equality is what makes a result --
        # including an empty one -- a complete view of the tenant rather than a
        # truncated response that would prune real openings.
        if len(records) != declared_count:
            raise CollectionNotAuthoritative(
                f"{self.platform_name} listing for \"{tenant}\" returned {len(records)} "
                f"postings but declared {declared_count}; the response is incomplete."
            )
        return jobs

    def build_job(
        self,
        company_id: str,
        company: dict[str, Any],
        record: dict[str, Any],
        title: str,
        detail_url: str,
        source_type: str,
        tenant: str,
        domain_id: int,
    ) -> JobRecord:
        pay_text, pay_min, pay_max, pay_period = published_pay(record)
        description = clean(str(record.get("jobLocation") or ""))
        department = clean(str(record.get("orgTitle") or ""))
        employment_type = clean(str(record.get("employmentType") or ""))
        summary_parts = [part for part in (department, employment_type) if part]
        return JobRecord(
            id=make_job_id(company, title, detail_url),
            companyId=company_id,
            companyName=str(company.get("Company Name") or ""),
            title=title,
            location=posting_location(record) or "Not listed",
            workType=work_type(record),
            payMin=pay_min,
            payMax=pay_max,
            payText=pay_text,
            payPeriod=pay_period,
            postedDate=clean(str(record.get("startDateRef") or "")),
            sourceUrl=detail_url,
            jobPlatform=self.platform_name,
            description=description,
            descriptionSnippet=" | ".join(summary_parts)[:360],
            collectedAt=datetime.now(timezone.utc).isoformat(),
            rawData={
                "collector": self.__class__.__name__,
                "sourceType": source_type,
                "tenant": tenant,
                "vendorDomain": self.vendor_domain,
                "domainId": domain_id,
                "postingId": str(record.get("id") or ""),
                "department": department,
                "employmentType": employment_type,
                "classification": clean(str(record.get("classification") or "")),
            },
        )


class ApplicantProCollector(ApplicantProFamilyCollector):
    vendor_domain = APPLICANTPRO_DOMAIN
    platform_name = "ApplicantPro"


class IsolvedHireCollector(ApplicantProFamilyCollector):
    vendor_domain = ISOLVED_HIRE_DOMAIN
    platform_name = "isolved Hire"


def tenant_from_board_url(board_url: str, vendor_domain: str) -> str:
    """Return the tenant subdomain a board URL identifies, or an empty string.

    Accepts the tenant subdomain form and the ``/openings/<tenant>/`` alias the
    vendor publishes on its own ``www`` host.
    """
    parsed = urlsplit(str(board_url or "").strip())
    if parsed.scheme.lower() not in {"https", ""}:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or not hostname_matches_domain(host, vendor_domain):
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    try:
        if parsed.port not in {None, 443}:
            return ""
    except ValueError:
        return ""

    label = host[: -len(f".{vendor_domain}")] if host != vendor_domain else ""
    if label and label not in RESERVED_SUBDOMAINS and TENANT_LABEL.fullmatch(label):
        return label

    # www.<vendor>/openings/<tenant>/... and <vendor>/openings/<tenant>/...
    if label in RESERVED_SUBDOMAINS or not label:
        alias = OPENINGS_ALIAS_PATH.match(parsed.path or "")
        if alias:
            tenant = alias.group("tenant").strip().lower()
            if tenant and tenant not in RESERVED_SUBDOMAINS and TENANT_LABEL.fullmatch(tenant):
                return tenant
    return ""


def inactive_site_reason(landing_url: str, body: str) -> str:
    """Return why a tenant's career site is switched off, or an empty string."""
    path = (urlsplit(str(landing_url or "")).path or "").lower()
    if path.startswith(_INACTIVE_SITE_PATH):
        if "disabled=1" in str(landing_url or "").lower():
            return "has a disabled career site"
        return "has no active career site"
    if _INACTIVE_SITE_TEXT in str(body or "").casefold():
        return "has a disabled career site"
    return ""


def redirect_left_the_tenant(landing_url: str, tenant: str, vendor_domain: str) -> str:
    """Return why a board request ended somewhere other than the tenant board."""
    parsed = urlsplit(str(landing_url or ""))
    host = (parsed.hostname or "").lower().rstrip(".")
    expected = f"{tenant}.{vendor_domain}"
    if not host:
        return "did not resolve to a host"
    if host != expected:
        return f"redirected to {host} instead of {expected}"
    return ""


def parse_tenant_identity(html: str) -> dict[str, Any]:
    """Return the ``componentData`` tenant fields embedded in a board page."""
    text = str(html or "")
    block = _COMPONENT_DATA_BLOCK.search(text)
    scope = block.group("body") if block else text
    identity: dict[str, Any] = {}
    for name in ("organizationId", "domainId"):
        match = re.search(_NUMERIC_FIELD.format(name=name), scope)
        if match:
            identity[name] = int(match.group("value"))
    for name in ("domainName", "subdomainName"):
        match = re.search(_STRING_FIELD.format(name=name), scope)
        if match:
            identity[name] = match.group("value").strip().lower()
    return identity


def tenant_identity_problem(identity: dict[str, Any], tenant: str, vendor_domain: str) -> str:
    """Return why a board page's embedded identity is unusable, or ""."""
    if "domainId" not in identity:
        return "did not publish a career-site domainId"
    if identity["domainId"] <= 0:
        return "published a non-positive career-site domainId"
    subdomain = str(identity.get("subdomainName") or "")
    if not subdomain:
        return "did not publish its tenant subdomain"
    if subdomain != tenant:
        return f"identifies tenant \"{subdomain}\" rather than \"{tenant}\""
    domain_name = str(identity.get("domainName") or "")
    if domain_name and domain_name != vendor_domain:
        return f"identifies vendor domain \"{domain_name}\" rather than \"{vendor_domain}\""
    return ""


def listing_payload(payload: Any, platform_name: str, tenant: str) -> tuple[list[dict[str, Any]], int]:
    """Return ``(postings, declared_count)`` from a listing response."""
    if not isinstance(payload, dict):
        raise CollectionNotAuthoritative(
            f"{platform_name} listing for \"{tenant}\" returned a non-object response."
        )
    if payload.get("success") is not True:
        raise CollectionNotAuthoritative(
            f"{platform_name} listing for \"{tenant}\" did not report success."
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise CollectionNotAuthoritative(
            f"{platform_name} listing for \"{tenant}\" returned no data object."
        )
    postings = data.get("jobs")
    if not isinstance(postings, list):
        raise CollectionNotAuthoritative(
            f"{platform_name} listing for \"{tenant}\" returned no jobs list."
        )
    if len(postings) > MAX_BOARD_JOBS:
        raise CollectionNotAuthoritative(
            f"{platform_name} listing for \"{tenant}\" returned {len(postings)} postings, "
            f"beyond the {MAX_BOARD_JOBS} safety limit."
        )
    declared = data.get("jobCount")
    if not isinstance(declared, int) or isinstance(declared, bool) or declared < 0:
        raise CollectionNotAuthoritative(
            f"{platform_name} listing for \"{tenant}\" did not report a job count."
        )
    for posting in postings:
        if not isinstance(posting, dict):
            raise CollectionNotAuthoritative(
                f"{platform_name} listing for \"{tenant}\" contained a malformed posting."
            )
    return postings, declared


def foreign_tenant_reason(
    record: dict[str, Any], tenant: str, vendor_domain: str, domain_id: int
) -> str:
    """Return why a posting belongs to a different tenant, or an empty string."""
    subdomain = str(record.get("subdomain") or "").strip().lower()
    if subdomain and subdomain != tenant:
        return f"posting belongs to tenant \"{subdomain}\", not \"{tenant}\""
    domain_name = str(record.get("domainName") or "").strip().lower()
    if domain_name and domain_name != vendor_domain:
        return f"posting belongs to vendor domain \"{domain_name}\", not \"{vendor_domain}\""
    site_id = record.get("siteId")
    if isinstance(site_id, (int, str)) and str(site_id).strip():
        try:
            if int(site_id) != domain_id:
                return f"posting belongs to career site {site_id}, not {domain_id}"
        except (TypeError, ValueError):
            return f"posting carries an unreadable career-site id ({site_id!r})"
    url_host = (urlsplit(str(record.get("jobUrl") or "")).hostname or "").lower().rstrip(".")
    if url_host and url_host != f"{tenant}.{vendor_domain}":
        return f"posting URL points at {url_host}, not {tenant}.{vendor_domain}"
    return ""


def canonical_detail_url(
    record: dict[str, Any], tenant: str, vendor_domain: str, posting_id: str
) -> str:
    """Return the tenant-hosted application URL for a posting."""
    published = str(record.get("jobUrl") or "").strip()
    parsed = urlsplit(published)
    if parsed.scheme == "https" and (parsed.hostname or "").lower() == f"{tenant}.{vendor_domain}":
        return published
    if posting_id:
        return f"https://{tenant}.{vendor_domain}/jobs/{posting_id}"
    return f"https://{tenant}.{vendor_domain}/jobs/"


def posting_location(record: dict[str, Any]) -> str:
    city = clean(str(record.get("city") or ""))
    region = clean(str(record.get("abbreviation") or "")) or clean(str(record.get("stateName") or ""))
    if city and region:
        return f"{city}, {region}"
    return city or region


def work_type(record: dict[str, Any]) -> str:
    value = clean(str(record.get("workplaceType") or "")).casefold()
    if value.startswith("remote"):
        return "Remote"
    if value.startswith("hybrid"):
        return "Hybrid"
    if value.startswith(("onsite", "on-site")):
        return "Onsite"
    return "Not Listed"


_PAY_PERIODS = {
    "hourly": "hour",
    "hour": "hour",
    "salary": "year",
    "yearly": "year",
    "annual": "year",
    "annually": "year",
    "weekly": "week",
    "monthly": "month",
    "daily": "day",
}


def published_pay(record: dict[str, Any]) -> tuple[str, float | None, float | None, str]:
    """Return pay only when the tenant actually published it.

    Nothing is inferred: an unpublished or non-numeric pay field yields no pay at
    all rather than a guess.
    """
    published = [
        amount
        for amount in (
            numeric_amount(record.get("minSalary")),
            numeric_amount(record.get("maxSalary")),
        )
        if amount is not None
    ]
    if not published:
        single = numeric_amount(record.get("payRate"))
        if single is None:
            return "", None, None, "unknown"
        published = [single]
    minimum, maximum = min(published), max(published)

    period = _PAY_PERIODS.get(clean(str(record.get("payType") or "")).casefold(), "unknown")
    amounts = format_amount(minimum) if minimum == maximum else f"{format_amount(minimum)}-{format_amount(maximum)}"
    suffix = f" per {period}" if period != "unknown" else ""
    return f"${amounts}{suffix}", minimum, maximum, period


def numeric_amount(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = clean(str(value)).replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        amount = float(text)
    except ValueError:
        return None
    if amount <= 0:
        return None
    return amount


def format_amount(amount: float) -> str:
    if float(amount).is_integer():
        return f"{int(amount):,}"
    return f"{amount:,.2f}"


def describe_request_failure(exc: Exception) -> str:
    """Describe a request failure without leaking the response body."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 403:
        return "HTTP 403 -- blocked or protected by a WAF"
    if status == 429:
        return "HTTP 429 -- rate limited"
    if status is not None:
        return f"HTTP {status}"
    return type(exc).__name__


def clean(value: str) -> str:
    return " ".join(str(value or "").split())


def public_board_params() -> dict[str, Any]:
    """The listing filter this collector sends, as a dictionary."""
    return json.loads(PUBLIC_BOARD_PARAMS)
