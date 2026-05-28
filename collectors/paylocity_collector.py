from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from collectors.base import BaseCollector
from excel_tools import stable_company_id
from job_tools import JobRecord, make_job_id
from job_validation import is_valid_job_title, normalize_job_title, rejection_reason


class PaylocityCollector(BaseCollector):
    requires_browser = True

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        local_browser_path = Path(__file__).resolve().parents[1] / ".playwright-browsers"
        if local_browser_path.exists() and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browser_path)

        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        source_url, source_type = self.source_url(company)
        jobs: list[JobRecord] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(source_url, wait_until="domcontentloaded", timeout=45000)
                self.final_url_after_redirect = page.url
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            self.final_url_after_redirect = page.url

            links = page.locator("a[href*='/Recruiting/Jobs/Details/'], a[href*='/recruiting/jobs/details/']")
            listings: list[tuple[str, str]] = []
            for index in range(links.count()):
                link = links.nth(index)
                try:
                    title = normalize_job_title(link.inner_text(timeout=2000))
                    detail_url = urljoin(source_url, link.get_attribute("href") or "")
                except Exception:
                    continue
                self.record_candidate(title, detail_url)
                if not is_valid_job_title(title):
                    self.reject_candidate(title, rejection_reason(title), detail_url)
                    continue
                listings.append((title, detail_url))

            seen: set[str] = set()
            for title, detail_url in listings:
                if detail_url in seen:
                    self.reject_candidate(title, "duplicate job URL", detail_url)
                    continue
                detail = fetch_detail(page, detail_url, title)
                location = detail.get("location") or extract_location(detail.get("description", ""))
                description = detail.get("description") or title
                pay_text = extract_pay_text(description)
                pay_min, pay_max = parse_pay(pay_text)
                job = JobRecord(
                    id=make_job_id(company, title, detail_url),
                    companyId=str(company.get("Company ID") or stable_company_id(company)),
                    companyName=str(company.get("Company Name") or ""),
                    title=title,
                    location=location or "Not listed",
                    workType=extract_work_type(" ".join([location, description])),
                    payMin=pay_min,
                    payMax=pay_max,
                    payText=pay_text,
                    postedDate=detail.get("postedDate", ""),
                    sourceUrl=detail_url,
                    jobPlatform="Paylocity",
                    description=description,
                    descriptionSnippet=description[:360].strip(),
                    collectedAt=datetime.now(timezone.utc).isoformat(),
                    rawData={"collector": self.__class__.__name__, "sourceType": source_type},
                )
                jobs.append(job)
                seen.add(detail_url)
                self.save_candidate(title, detail_url)
            self.flush_debug(company)
            browser.close()
        return jobs


def fetch_detail(page, detail_url: str, title: str) -> dict[str, str]:
    page.goto(detail_url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    text = clean_text(page.locator("body").inner_text(timeout=8000))
    location = ""
    escaped = re.escape(title)
    match = re.search(rf"Apply\s+{escaped}\s+(.+?)\s+Description\s+", text, flags=re.IGNORECASE)
    if match:
        location = clean_text(match.group(1))
    return {"title": title, "location": location, "postedDate": "", "description": text}


def extract_location(text: str) -> str:
    match = re.search(r"([A-Za-z .'-]+,\s*[A-Z]{2})\b", text)
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
    match = re.search(r"\$\s?\d{2,3}(?:,\d{3})?(?:\.\d{2})?\s*(?:-|to|–)\s*\$\s?\d{2,3}(?:,\d{3})?(?:\.\d{2})?", text)
    return clean_text(match.group(0)) if match else ""


def parse_pay(pay_text: str) -> tuple[int | None, int | None]:
    values = [int(float(value.replace(",", ""))) for value in re.findall(r"\$\s?(\d{2,3}(?:,\d{3})?(?:\.\d{2})?)", pay_text)]
    if len(values) >= 2:
        return min(values), max(values)
    if values:
        return values[0], values[0]
    return None, None


def clean_text(value: str) -> str:
    return " ".join((value or "").split())
