from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from backend.outbound_security import install_playwright_url_guard, launch_playwright_chromium, safe_page_goto
from collectors.base import BaseCollector
from job_tools import JobRecord, make_job_id
from job_validation import is_valid_job_title, normalize_job_title, rejection_reason


class WorkdayCollector(BaseCollector):
    requires_browser = True

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        local_browser_path = Path(__file__).resolve().parents[1] / ".playwright-browsers"
        if local_browser_path.exists() and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browser_path)

        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        board_url, source_type = self.source_url(company)
        if not board_url:
            return []

        jobs: list[JobRecord] = []
        with sync_playwright() as playwright:
            browser = launch_playwright_chromium(playwright, headless=True)
            context = browser.new_context(
                user_agent=(
                    "FinancialJobsRadar/1.0 "
                    "(public job listing collector; no applications or form submissions)"
                ),
                service_workers="block",
            )
            install_playwright_url_guard(context)
            page = context.new_page()
            try:
                safe_page_goto(page, board_url, wait_until="domcontentloaded", timeout=45000)
                self.final_url_after_redirect = page.url
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            self.final_url_after_redirect = page.url

            try:
                page.wait_for_selector("a[href*='/job/'], li:has(a[href*='/job/'])", timeout=20000)
            except PlaywrightTimeoutError:
                self.flush_debug(company)
                browser.close()
                return []

            seen_urls: set[str] = set()
            for _page_number in range(1, 11):
                rows = page.locator("li:has(a[href*='/job/'])")
                count = rows.count()
                for index in range(count):
                    row = rows.nth(index)
                    try:
                        anchor = row.locator("a[href*='/job/']").first
                        title = normalize_job_title(anchor.inner_text(timeout=3000))
                        href = anchor.get_attribute("href") or ""
                        detail_url = urljoin(board_url, href)
                        row_text = clean_text(row.inner_text(timeout=3000))
                    except Exception:
                        continue

                    self.record_candidate(title or row_text, detail_url)
                    if detail_url in seen_urls:
                        self.reject_candidate(title or row_text, "duplicate job URL", detail_url)
                        continue
                    if not is_valid_job_title(title):
                        self.reject_candidate(title or row_text, rejection_reason(title), detail_url)
                        continue

                    location = extract_workday_location(row_text)
                    posted_date = extract_posted_date(row_text)
                    detail = self.fetch_detail(context, detail_url)
                    description = detail.get("description") or row_text
                    pay_text = extract_pay_text(description)
                    pay_min, pay_max = parse_pay(pay_text)
                    work_type = extract_work_type(" ".join([location, description]))
                    snippet = description[:360].strip()

                    job = JobRecord(
                        id=make_job_id(company, title, detail_url),
                        companyId=str(company.get("Company ID") or ""),
                        companyName=str(company.get("Company Name") or ""),
                        title=title,
                        location=location or "Not listed",
                        workType=work_type,
                        payMin=pay_min,
                        payMax=pay_max,
                        payText=pay_text,
                        postedDate=posted_date,
                        sourceUrl=detail_url,
                        jobPlatform="Workday",
                        description=description,
                        descriptionSnippet=snippet,
                        collectedAt=datetime.now(timezone.utc).isoformat(),
                        rawData={
                            "sourceType": source_type,
                            "rowText": row_text,
                            "detailTitle": detail.get("title", ""),
                        },
                    )
                    jobs.append(job)
                    seen_urls.add(detail_url)
                    self.save_candidate(title, detail_url)

                if not click_next_page(page):
                    break

            browser.close()

        self.flush_debug(company)
        return jobs

    def fetch_detail(self, context, detail_url: str) -> dict[str, str]:
        page = context.new_page()
        try:
            safe_page_goto(page, detail_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            title = ""
            for selector in ["h1", "[data-automation-id='jobPostingHeader']"]:
                try:
                    text = clean_text(page.locator(selector).first.inner_text(timeout=2000))
                    if text:
                        title = text
                        break
                except Exception:
                    continue
            description = ""
            for selector in [
                "[data-automation-id='jobPostingDescription']",
                "[data-automation-id='jobPostingPage']",
                "main",
                "body",
            ]:
                try:
                    text = clean_text(page.locator(selector).first.inner_text(timeout=4000))
                    if len(text) > len(description):
                        description = text
                except Exception:
                    continue
            return {"title": title, "description": description}
        finally:
            page.close()


def extract_workday_location(row_text: str) -> str:
    match = re.search(r"\blocations?\s+(.+?)\s+posted on\b", row_text, flags=re.IGNORECASE)
    if match:
        return clean_text(match.group(1))
    match = re.search(r"\blocation\s+(.+?)\s+posted\b", row_text, flags=re.IGNORECASE)
    if match:
        return clean_text(match.group(1))
    return ""


def extract_posted_date(row_text: str) -> str:
    match = re.search(r"\bposted on\s+(.+?)(?:\s+job requisition|\s+R-\d+|$)", row_text, flags=re.IGNORECASE)
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
    patterns = [
        r"\$\s?\d{2,3}(?:,\d{3})?(?:\.\d{2})?\s*(?:-|to|–)\s*\$\s?\d{2,3}(?:,\d{3})?(?:\.\d{2})?",
        r"\$\s?\d{2,3}(?:\.\d{2})?\s*/\s*hour\s*(?:-|to|–)\s*\$\s?\d{2,3}(?:\.\d{2})?\s*/\s*hour",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(0))
    return ""


def parse_pay(pay_text: str) -> tuple[int | None, int | None]:
    values = []
    for value in re.findall(r"\$\s?(\d{2,3}(?:,\d{3})?(?:\.\d{2})?)", pay_text):
        number = float(value.replace(",", ""))
        values.append(int(number))
    if len(values) >= 2:
        return min(values), max(values)
    if len(values) == 1:
        return values[0], values[0]
    return None, None


def clean_text(value: str) -> str:
    return " ".join((value or "").split())


def click_next_page(page) -> bool:
    next_button = page.locator("button[aria-label='next'], [role='button'][aria-label='next']").first
    try:
        if next_button.count() == 0 or not next_button.is_visible(timeout=1000):
            return False
        disabled = next_button.get_attribute("disabled")
        aria_disabled = next_button.get_attribute("aria-disabled")
        if disabled is not None or str(aria_disabled).lower() == "true":
            return False
        next_button.scroll_into_view_if_needed(timeout=2000)
        next_button.click(timeout=5000)
        page.wait_for_load_state("networkidle", timeout=10000)
        page.wait_for_timeout(1000)
        return True
    except Exception:
        return False
