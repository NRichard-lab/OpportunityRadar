from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from backend.outbound_security import install_playwright_url_guard, launch_playwright_chromium, safe_page_goto
from collectors.base import BaseCollector
from excel_tools import stable_company_id
from job_tools import JobRecord, make_job_id
from job_validation import is_valid_job_title, normalize_job_title, rejection_reason


class PaycorCollector(BaseCollector):
    requires_browser = True

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        local_browser_path = Path(__file__).resolve().parents[1] / ".playwright-browsers"
        if local_browser_path.exists() and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browser_path)

        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        source_url, source_type = self.source_url(company)
        if not source_url:
            return []
        source_url = self.resolve_embedded_job_board_url(source_url, "Paycor")

        jobs: list[JobRecord] = []
        with sync_playwright() as playwright:
            browser = launch_playwright_chromium(playwright, headless=True)
            context = browser.new_context(service_workers="block")
            install_playwright_url_guard(context)
            page = context.new_page()
            try:
                safe_page_goto(page, source_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass

            frame = find_paycor_frame(page)
            if frame is None:
                self.reject_candidate(source_url, "Paycor iframe/listing frame not found", source_url)
                self.flush_debug(company)
                browser.close()
                return []

            links = frame.locator("a[href*='JobIntroduction.action']")
            link_count = links.count()
            listing_rows: list[dict[str, str]] = []
            for index in range(link_count):
                link = links.nth(index)
                try:
                    title = normalize_job_title(link.inner_text(timeout=2000))
                    href = link.get_attribute("href") or ""
                    detail_url = urljoin(frame.url, href)
                except Exception:
                    continue
                self.record_candidate(title, detail_url)
                if not is_valid_job_title(title):
                    self.reject_candidate(title, rejection_reason(title), detail_url)
                    continue
                listing_rows.append({"title": title, "url": detail_url})

            seen_urls: set[str] = set()
            for listing in listing_rows:
                if listing["url"] in seen_urls:
                    self.reject_candidate(listing["title"], "duplicate job URL", listing["url"])
                    continue
                detail = fetch_paycor_detail(page, source_url, listing["url"])
                description = detail.get("description") or listing["title"]
                title = normalize_job_title(listing["title"])
                if not is_valid_job_title(title):
                    self.reject_candidate(title, rejection_reason(title), listing["url"])
                    continue
                location = detail.get("location") or extract_location(description)
                pay_text = extract_pay_text(description)
                pay_min, pay_max = parse_pay(pay_text)
                work_type = extract_work_type(" ".join([location, description]))
                job = JobRecord(
                    id=make_job_id(company, title, listing["url"]),
                    companyId=str(company.get("Company ID") or stable_company_id(company)),
                    companyName=str(company.get("Company Name") or ""),
                    title=title,
                    location=location or "Not listed",
                    workType=work_type,
                    payMin=pay_min,
                    payMax=pay_max,
                    payText=pay_text,
                    postedDate=detail.get("postedDate", ""),
                    sourceUrl=listing["url"],
                    jobPlatform="Paycor",
                    description=description,
                    descriptionSnippet=description[:360].strip(),
                    collectedAt=datetime.now(timezone.utc).isoformat(),
                    rawData={"collector": self.__class__.__name__, "sourceType": source_type},
                )
                jobs.append(job)
                seen_urls.add(listing["url"])
                self.save_candidate(title, listing["url"])

            self.flush_debug(company)
            browser.close()
        return jobs


def find_paycor_frame(page):
    for frame in page.frames:
        if "recruitingbypaycor.com" in frame.url:
            return frame
    return None


def fetch_paycor_detail(page, list_url: str, detail_url: str) -> dict[str, str]:
    safe_page_goto(page, list_url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    frame = find_paycor_frame(page)
    if frame is None:
        return {"description": ""}
    links = frame.locator("a[href*='JobIntroduction.action']")
    for index in range(links.count()):
        link = links.nth(index)
        try:
            if (link.get_attribute("href") or "") == detail_url:
                link.click(timeout=5000)
                page.wait_for_timeout(1200)
                frame = find_paycor_frame(page) or frame
                break
        except Exception:
            continue
    text = clean_text(frame.locator("body").inner_text(timeout=8000))
    title = ""
    try:
        heading = frame.locator("h1, h2, h3").first
        if heading.count():
            title = normalize_job_title(heading.inner_text(timeout=1000))
    except Exception:
        pass
    if not title:
        title = infer_title_from_detail(text)
    return {
        "title": title,
        "location": extract_location(text, title),
        "postedDate": extract_posted_date(text),
        "description": text,
    }


def infer_title_from_detail(text: str) -> str:
    parts = text.split(" ")
    if not parts:
        return ""
    for state_index, token in enumerate(parts[:12]):
        if re.fullmatch(r"[A-Z]{2}", token.strip(",")):
            return normalize_job_title(" ".join(parts[: max(1, state_index - 2)]))
    return normalize_job_title(" ".join(parts[:6]))


def extract_location(text: str, title: str = "") -> str:
    if title and text.lower().startswith(title.lower()):
        remainder = text[len(title):].strip(" -")
        for marker in [" 1st Advantage Federal Credit Union", " 1st Advantage", " is currently"]:
            marker_index = remainder.find(marker)
            if marker_index > 0:
                candidate = clean_text(remainder[:marker_index].strip(" -"))
                if candidate:
                    return candidate
    else:
        remainder = text
    match = re.search(r"([A-Za-z .'-]+,\s*(?:[A-Z]{2}|Virginia|Washington|Oregon|Idaho))\b", remainder)
    return clean_text(match.group(1)) if match else ""


def extract_posted_date(text: str) -> str:
    match = re.search(r"\bposted\s+(.+?)(?:\s+apply|\s+description|$)", text, flags=re.IGNORECASE)
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
    match = re.search(
        r"\$\s?\d{2,3}(?:,\d{3})?(?:\.\d{2})?\s*(?:-|to|–)\s*\$\s?\d{2,3}(?:,\d{3})?(?:\.\d{2})?",
        text,
        flags=re.IGNORECASE,
    )
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
