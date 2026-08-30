from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from collectors.base import BaseCollector
from excel_tools import stable_company_id
from job_tools import JobRecord, make_job_id
from job_validation import is_valid_structured_job_title, normalize_job_title, rejection_reason


ICIMS_MAX_PAGES = 50
ICIMS_JOB_PATH = re.compile(r"^/jobs/(?P<job_id>\d+)/(?P<requisition>[^/]+)/job/?$", re.IGNORECASE)


class ICIMSCollector(BaseCollector):
    """Collect public iCIMS job cards from the provider's iframe listing."""

    requires_browser = False

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        source_url, source_type = self.source_url(company)
        if not source_url:
            return []

        page_url = normalize_icims_board_url(source_url)
        seen_pages: set[str] = set()
        seen_jobs: set[str] = set()
        jobs: list[JobRecord] = []

        for _page_number in range(ICIMS_MAX_PAGES):
            if page_url in seen_pages:
                raise ValueError("iCIMS pagination repeated a page URL")
            seen_pages.add(page_url)
            response = self.get(page_url)
            validate_icims_url(response.url)
            soup = BeautifulSoup(response.text, "html.parser")
            cards = [card for card in soup.select("li.iCIMS_JobCardItem") if isinstance(card, Tag)]

            for card in cards:
                anchor = card.select_one(".title a[href]")
                href = urljoin(response.url, str(anchor.get("href") or "")) if isinstance(anchor, Tag) else ""
                title_node = anchor.select_one("h1, h2, h3, h4, h5, h6") if isinstance(anchor, Tag) else None
                title = normalize_job_title(title_node.get_text(" ", strip=True) if title_node else "")
                destination_url = canonical_icims_job_url(href) if href else ""
                self.record_candidate(title or "iCIMS posting without a title", destination_url)

                if not is_valid_structured_job_title(title):
                    self.reject_candidate(
                        title,
                        rejection_reason(title),
                        destination_url,
                        surrounding_text=clean_text(card.get_text(" ", strip=True)),
                        company=company,
                        job_board_url=source_url,
                    )
                    continue
                job_match = ICIMS_JOB_PATH.fullmatch(urlsplit(destination_url).path)
                if not job_match:
                    self.reject_candidate(
                        title,
                        "missing stable iCIMS job URL",
                        destination_url,
                        company=company,
                        job_board_url=source_url,
                    )
                    continue
                job_id = job_match.group("job_id")
                if job_id in seen_jobs:
                    self.reject_candidate(
                        title,
                        "duplicate iCIMS job ID",
                        destination_url,
                        company=company,
                        job_board_url=source_url,
                    )
                    continue
                seen_jobs.add(job_id)

                fields = icims_card_fields(card)
                description_node = card.select_one(".description")
                description = clean_text(description_node.get_text(" ", strip=True) if description_node else "")
                location = icims_location(card)
                pay_min = parse_icims_pay(fields.get("Min", ""))
                pay_max = parse_icims_pay(fields.get("Max", ""))
                pay_period = icims_pay_period(fields.get("Min", "") or fields.get("Max", ""))
                pay_text = icims_pay_text(pay_min, pay_max, pay_period)
                if pay_text:
                    self.record_pay_extraction(
                        "iCIMS listing fields",
                        f"{fields.get('Min', '')} {fields.get('Max', '')}",
                        {
                            "payText": pay_text,
                            "payMin": pay_min,
                            "payMax": pay_max,
                            "payPeriod": pay_period,
                            "payPatternMatched": "iCIMS Min/Max fields",
                        },
                    )

                company_id = str(company.get("Company ID") or stable_company_id(company))
                jobs.append(
                    JobRecord(
                        id=make_job_id(company, title, f"icims-{job_id}-{company_id}"),
                        companyId=company_id,
                        companyName=str(company.get("Company Name") or ""),
                        title=title,
                        location=location or "Not listed",
                        payMin=pay_min,
                        payMax=pay_max,
                        payText=pay_text,
                        payPeriod=pay_period,
                        payCurrency="USD",
                        sourceUrl=destination_url,
                        jobPlatform="ICIMS",
                        description=description,
                        descriptionSnippet=description[:360],
                        collectedAt=datetime.now(timezone.utc).isoformat(),
                        rawData={
                            "collector": self.__class__.__name__,
                            "structuredSource": True,
                            "sourceType": source_type,
                            "jobId": job_id,
                            "requisitionId": fields.get("ID", job_match.group("requisition")),
                            "category": fields.get("Category", ""),
                        },
                    )
                )
                self.save_candidate(title, destination_url)

            next_node = soup.select_one("link[rel='next']")
            next_url = urljoin(response.url, str(next_node.get("href") or "")) if isinstance(next_node, Tag) else ""
            if not next_url:
                self.final_url_after_redirect = normalize_icims_board_url(source_url)
                return jobs
            validate_icims_url(next_url)
            page_url = ensure_icims_iframe(next_url)

        raise ValueError("iCIMS pagination reached its safety limit before completion")


def validate_icims_url(url: str) -> None:
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not (host == "icims.com" or host.endswith(".icims.com"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise ValueError("iCIMS collector requires an HTTPS icims.com job board URL")


def normalize_icims_board_url(url: str) -> str:
    validate_icims_url(url)
    parsed = urlsplit(str(url or "").strip())
    if parsed.path.rstrip("/").lower() not in {"", "/jobs", "/jobs/intro", "/jobs/search"}:
        raise ValueError("iCIMS collector requires a jobs search or portal URL")
    query = urlencode({"ss": "1", "searchRelation": "keyword_all", "in_iframe": "1"})
    return urlunsplit(("https", parsed.netloc, "/jobs/search", query, ""))


def ensure_icims_iframe(url: str) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["in_iframe"] = "1"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def canonical_icims_job_url(url: str) -> str:
    validate_icims_url(url)
    parsed = urlsplit(url)
    return urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))


def icims_card_fields(card: Tag) -> dict[str, str]:
    fields: dict[str, str] = {}
    for group in card.select(".iCIMS_JobHeaderTag"):
        label = group.select_one("dt")
        value = group.select_one("dd")
        if label and value:
            fields[clean_text(label.get_text(" ", strip=True))] = clean_text(value.get_text(" ", strip=True))
    return fields


def icims_location(card: Tag) -> str:
    node = card.select_one(".header.left")
    if not node:
        return ""
    label = node.select_one(".field-label")
    if label:
        label.extract()
    value = clean_text(node.get_text(" ", strip=True))
    match = re.fullmatch(r"([A-Z]{2})-([A-Z]{2})-(.+)", value)
    return f"{match.group(3).replace('-', ' ')}, {match.group(2)}, {match.group(1)}" if match else value


def parse_icims_pay(value: str) -> float | None:
    match = re.search(r"\$\s*([0-9][\d,]*(?:\.\d+)?)", str(value or ""))
    return float(match.group(1).replace(",", "")) if match else None


def icims_pay_period(value: str) -> str:
    lowered = str(value or "").casefold()
    if "/hr" in lowered or "hour" in lowered:
        return "hour"
    if "/yr" in lowered or "year" in lowered:
        return "year"
    return "unknown"


def icims_pay_text(pay_min: float | None, pay_max: float | None, period: str) -> str:
    if pay_min is None and pay_max is None:
        return ""
    values = [value for value in (pay_min, pay_max) if value is not None]
    amount = f"${values[0]:,.2f}" if len(values) == 1 else f"${values[0]:,.2f} - ${values[1]:,.2f}"
    return f"{amount} per {period}" if period != "unknown" else amount


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())
