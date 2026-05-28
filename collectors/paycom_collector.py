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


class PaycomCollector(BaseCollector):
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
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass

            links = page.locator("a[href*='/jobs/']")
            listings: list[dict[str, str]] = []
            seen: set[str] = set()
            for index in range(links.count()):
                link = links.nth(index)
                try:
                    raw_text = link.inner_text(timeout=2000)
                    text = clean_text(raw_text)
                    href = urljoin(source_url, link.get_attribute("href") or "")
                except Exception:
                    continue
                parts = [part.strip() for part in raw_text.splitlines() if part.strip()]
                title = normalize_job_title(parts[0] if parts else text)
                self.record_candidate(title or text, href)
                if not href or href in seen:
                    self.reject_candidate(title or text, "missing or duplicate job URL", href)
                    continue
                if not is_valid_job_title(title):
                    self.reject_candidate(title or text, rejection_reason(title), href)
                    continue
                location = next((part for part in parts if re.search(r",\s*[A-Z]{2}\b", part)), "")
                listings.append({"title": title, "url": href, "text": text, "location": location})
                seen.add(href)

            for listing in listings:
                description = fetch_detail(page, listing["url"]) or listing["text"]
                pay_text = extract_pay_text(description or listing["text"])
                pay_min, pay_max = parse_pay(pay_text)
                job = JobRecord(
                    id=make_job_id(company, listing["title"], listing["url"]),
                    companyId=str(company.get("Company ID") or stable_company_id(company)),
                    companyName=str(company.get("Company Name") or ""),
                    title=listing["title"],
                    location=listing["location"] or "Not listed",
                    workType=extract_work_type(" ".join([listing["text"], description])),
                    payMin=pay_min,
                    payMax=pay_max,
                    payText=pay_text,
                    postedDate="",
                    sourceUrl=listing["url"],
                    jobPlatform="Paycom",
                    description=description,
                    descriptionSnippet=description[:360].strip(),
                    collectedAt=datetime.now(timezone.utc).isoformat(),
                    rawData={"collector": self.__class__.__name__, "sourceType": source_type},
                )
                jobs.append(job)
                self.save_candidate(listing["title"], listing["url"])
            self.flush_debug(company)
            browser.close()
        return jobs


def fetch_detail(page, detail_url: str) -> str:
    page.goto(detail_url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    return clean_text(page.locator("body").inner_text(timeout=8000))


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
    match = re.search(r"\$\s?\d{2,3}(?:,\d{3})?(?:\.\d{2})?\s*(?:-|to|–)\s*\$?\s?\d{2,3}(?:,\d{3})?(?:\.\d{2})?", text)
    return clean_text(match.group(0)) if match else ""


def parse_pay(pay_text: str) -> tuple[int | None, int | None]:
    values = [int(float(value.replace(",", ""))) for value in re.findall(r"\$?\s?(\d{2,3}(?:,\d{3})?(?:\.\d{2})?)", pay_text)]
    if len(values) >= 2:
        return min(values), max(values)
    if values:
        return values[0], values[0]
    return None, None


def clean_text(value: str) -> str:
    return " ".join((value or "").split())
