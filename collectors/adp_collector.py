from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.outbound_security import install_playwright_url_guard, launch_playwright_chromium, safe_page_goto
from collectors.base import BaseCollector
from excel_tools import stable_company_id
from job_validation import is_valid_job_title, normalize_job_title, rejection_reason
from job_tools import JobRecord, make_job_id


class ADPCollector(BaseCollector):
    """Collect public postings from ADP Workforce Now job boards."""

    requires_browser = True

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        url, source_type = self.source_url(company)
        if not url:
            return []

        local_browser_path = Path(__file__).resolve().parents[1] / ".playwright-browsers"
        if local_browser_path.exists() and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browser_path)

        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        jobs: list[JobRecord] = []
        with sync_playwright() as playwright:
            browser = launch_playwright_chromium(playwright, headless=True)
            context = browser.new_context(
                user_agent="FinancialJobsRadar/1.0 public ADP job listing collector",
                service_workers="block",
            )
            install_playwright_url_guard(context)
            page = context.new_page()
            try:
                safe_page_goto(page, url, wait_until="domcontentloaded", timeout=45000)
                self.final_url_after_redirect = page.url
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except PlaywrightTimeoutError:
                    pass
                self.final_url_after_redirect = page.url
                page.wait_for_timeout(2500)

                candidates = self.find_candidates(page, url)
                for candidate in candidates:
                    title = candidate["title"]
                    candidate_url = candidate["url"]
                    detail = self.extract_detail(page, candidate)
                    source_url = candidate_url or detail.get("detailUrl", "")
                    if not self.is_valid_job_record(title, source_url, detail):
                        self.reject_candidate(title, "not enough posting evidence", source_url)
                        continue
                    job = self.build_job(company, title, source_url, detail, source_type)
                    jobs.append(job)
                    self.save_candidate(title, source_url)
            finally:
                browser.close()

        self.flush_debug(company)
        return dedupe_jobs(jobs)

    def find_candidates(self, page, board_url: str) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        locator = page.locator("a, button, [role='button'], [role='link'], tr, li, article, div")
        count = min(locator.count(), 1200)
        for index in range(count):
            element = locator.nth(index)
            try:
                if not element.is_visible(timeout=300):
                    continue
                raw_text = normalize_job_title(element.inner_text(timeout=700))
                href = element.get_attribute("href") or ""
                role = element.get_attribute("role") or ""
                tag = (element.evaluate("el => el.tagName") or "").lower()
            except Exception:
                continue

            if not raw_text:
                continue
            url = href if href.startswith("http") else ""
            row_data = parse_adp_row_text(raw_text)
            title = row_data["title"] or raw_text
            if len(re.findall(r"\d+\+?\s+days?\s+ago", raw_text.lower())) > 1:
                self.reject_candidate(raw_text, "aggregate postings block", url)
                continue
            if not url and row_data["title"]:
                url = f"{board_url}#job={slug(row_data['title'])}"
            self.record_candidate(raw_text, url)
            if not is_valid_job_title(title):
                self.reject_candidate(raw_text, rejection_reason(title), url)
                continue
            if not self.looks_like_adp_posting(raw_text, url, tag, role):
                self.reject_candidate(raw_text, "not an ADP posting row/link", url)
                continue
            candidates.append({"index": index, "title": title, "url": url, "tag": tag, "rowData": row_data})

        unique: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            key = candidate["url"] or candidate["title"].lower()
            unique[key] = candidate
        return list(unique.values())[:100]

    def extract_detail(self, page, candidate: dict[str, Any]) -> dict[str, str]:
        before_url = page.url
        detail_text = ""
        detail_url = candidate["url"] or before_url
        try:
            target = page.locator("a, button, [role='button'], [role='link'], tr, li, article, div").nth(candidate["index"])
            target.scroll_into_view_if_needed(timeout=3000)
            target.click(timeout=8000, no_wait_after=False)
            page.wait_for_timeout(1800)
            detail_url = page.url or detail_url
            detail_text = clean_text(page.locator("body").inner_text(timeout=5000))
            if page.url != before_url:
                page.go_back(wait_until="domcontentloaded", timeout=10000)
                page.wait_for_timeout(1200)
        except Exception:
            detail_text = ""
        detail = parse_detail_text(detail_text, detail_url)
        detail["rowData"] = candidate.get("rowData", {})
        return detail

    def looks_like_adp_posting(self, text: str, url: str, tag: str, role: str) -> bool:
        lowered_url = url.lower()
        lowered_text = text.lower()
        if "workforcenow.adp.com" in lowered_url and any(token in lowered_url for token in ["requisition", "recruitment", "job"]):
            return True
        if re.search(r"\b[A-Z][A-Za-z .,&/-]+\s+[A-Z][A-Za-z .'-]+,\s?[A-Z]{2},\s?US\s+", text):
            return True
        if re.search(r"\b\d+\+?\s+days?\s+ago\b", lowered_text):
            return True
        if tag in {"tr", "li", "article", "button", "a"} or role in {"button", "link"}:
            return bool(re.search(r"\b(manager|specialist|representative|teller|analyst|engineer|administrator|officer|assistant|consultant|coordinator|supervisor|developer|architect|accountant|banker)\b", lowered_text))
        return False

    def is_valid_job_record(self, title: str, source_url: str, detail: dict[str, str]) -> bool:
        if not is_valid_job_title(title):
            return False
        if not source_url:
            return False
        row_data = detail.get("rowData", {})
        row_location = row_data.get("location", "") if isinstance(row_data, dict) else ""
        evidence = " ".join([detail.get("description", ""), detail.get("location", ""), row_location, source_url]).lower()
        return any(term in evidence for term in ["job", "requisition", "description", "location", "posted", "workforcenow", "responsibilities", "qualifications"])

    def build_job(
        self,
        company: dict[str, Any],
        title: str,
        source_url: str,
        detail: dict[str, str],
        source_type: str,
    ) -> JobRecord:
        row_data = detail.get("rowData", {})
        location = detail.get("location") or row_data.get("location", "")
        description = detail.get("description", "") or clean_text(f"{title} {location} {row_data.get('postedDate', '')} {row_data.get('workType', '')}")
        return JobRecord(
            id=make_job_id(company, title, source_url),
            companyId=str(company.get("Company ID") or stable_company_id(company)),
            companyName=str(company.get("Company Name") or ""),
            title=title,
            location=location,
            workType=detail.get("workType") or row_data.get("workType", "Not Listed") or "Not Listed",
            payMin=parse_pay(detail.get("payText", ""))[0],
            payMax=parse_pay(detail.get("payText", ""))[1],
            payText=detail.get("payText", ""),
            postedDate=detail.get("postedDate") or row_data.get("postedDate", ""),
            sourceUrl=source_url,
            jobPlatform="ADP Workforce Now",
            description=description,
            descriptionSnippet=description[:240],
            collectedAt=datetime.now().astimezone().replace(microsecond=0).isoformat(),
            rawData={"collector": self.__class__.__name__, "sourceType": source_type, "detailUrl": detail.get("detailUrl", "")},
        )


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_detail_text(text: str, detail_url: str) -> dict[str, str]:
    cleaned = clean_text(text)
    return {
        "description": cleaned,
        "location": extract_labeled_value(cleaned, ["Location", "Job Location"]),
        "workType": extract_work_type(cleaned),
        "payText": extract_pay_text(cleaned),
        "postedDate": extract_labeled_value(cleaned, ["Posted", "Date Posted", "Posted Date"]),
        "detailUrl": detail_url,
    }


def parse_adp_row_text(text: str) -> dict[str, str]:
    cleaned = clean_text(text)
    match = re.match(
        r"(?P<title>.+)\s+(?P<location>[A-Z][A-Za-z .'-]+,\s?[A-Z]{2},\s?US)\s+(?P<posted>\d+\+?\s+days?\s+ago)(?P<work>.*)$",
        cleaned,
    )
    if not match:
        return {"title": "", "location": "", "postedDate": "", "workType": "Not Listed"}
    return {
        "title": normalize_job_title(match.group("title")),
        "location": clean_text(match.group("location")),
        "postedDate": clean_text(match.group("posted")),
        "workType": extract_work_type(match.group("work")),
    }


def extract_labeled_value(text: str, labels: list[str]) -> str:
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*:?\s*([A-Za-z0-9, .\-/]+)", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()[:120]
    return ""


def extract_work_type(text: str) -> str:
    lowered = text.lower()
    if "hybrid" in lowered:
        return "Hybrid"
    if "remote" in lowered:
        return "Remote"
    if "onsite" in lowered or "on-site" in lowered:
        return "Onsite"
    return "Not Listed"


def extract_pay_text(text: str) -> str:
    match = re.search(r"\$\s?\d[\d,]*(?:\.\d{2})?(?:\s?[-–to]+\s?\$\s?\d[\d,]*(?:\.\d{2})?)?", text, re.IGNORECASE)
    return match.group(0) if match else ""


def parse_pay(pay_text: str) -> tuple[int | None, int | None]:
    values = [int(value.replace(",", "")) for value in re.findall(r"\$?\s?(\d[\d,]*)", pay_text or "")]
    if not values:
        return None, None
    if len(values) == 1:
        return values[0], None
    return min(values), max(values)


def dedupe_jobs(jobs: list[JobRecord]) -> list[JobRecord]:
    unique: dict[str, JobRecord] = {}
    for job in jobs:
        unique[job.sourceUrl or job.id] = job
    return list(unique.values())


def slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "posting"
