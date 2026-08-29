from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from backend.outbound_security import install_playwright_url_guard, launch_playwright_chromium, safe_page_goto
from collectors.base import BaseCollector
from excel_tools import stable_company_id
from job_enrichment import extract_json_ld_pay_info, extract_pay_info
from job_validation import is_valid_job_title, normalize_job_title, rejection_reason
from job_tools import JobRecord, make_job_id


ACTION_LABELS = {
    "apply",
    "apply now",
    "view details",
    "view details (opens an external site)",
    "opens an external site",
    "learn more",
    "read more",
    "search jobs",
}

CARD_SELECTORS = [
    "article",
    "li",
    "tr",
    "[class*='job']",
    "[class*='career']",
    "[class*='opening']",
    "[class*='position']",
    "[class*='listing']",
    "[class*='card']",
    "[id*='job']",
    "[id*='opening']",
]


class GenericCollector(BaseCollector):
    requires_browser = True

    def collect(self, company: dict[str, Any]) -> list[JobRecord]:
        url, source_type = self.source_url(company)
        if not url:
            return []
        try:
            return self.collect_with_browser(company, url, source_type)
        except Exception as exc:
            self.reject_candidate(url, f"browser render failed; falling back to HTTP: {exc}", url, company=company)
            return self.collect_with_http(company, url, source_type)

    def collect_with_browser(self, company: dict[str, Any], url: str, source_type: str) -> list[JobRecord]:
        local_browser_path = Path(__file__).resolve().parents[1] / ".playwright-browsers"
        if local_browser_path.exists() and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browser_path)

        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = launch_playwright_chromium(playwright, headless=True)
            context = None
            try:
                context = browser.new_context(service_workers="block")
                install_playwright_url_guard(context)
                self._detail_context = context
                page = context.new_page()
                safe_page_goto(page, url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    pass
                self.final_url_after_redirect = page.url
                html = page.content()
                jobs = self.parse_listing_html(company, url, self.final_url_after_redirect or url, html, source_type)
            finally:
                self._detail_context = None
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                browser.close()

        self.flush_debug(company)
        return dedupe_jobs(jobs)

    def collect_with_http(self, company: dict[str, Any], url: str, source_type: str) -> list[JobRecord]:
        response = self.get(url)
        jobs = self.parse_listing_html(company, url, response.url, response.text, source_type)
        self.flush_debug(company)
        return dedupe_jobs(jobs)

    def parse_listing_html(
        self,
        company: dict[str, Any],
        board_url: str,
        final_url: str,
        html: str,
        source_type: str,
    ) -> list[JobRecord]:
        soup = BeautifulSoup(html, "html.parser")
        schema_jobs = self.extract_json_ld_jobs(company, final_url, soup, source_type)
        if schema_jobs:
            return schema_jobs

        jobs: list[JobRecord] = []
        seen_cards: set[int] = set()
        seen_hrefs: set[str] = set()
        for card in likely_job_cards(soup):
            identity = id(card)
            if identity in seen_cards:
                continue
            seen_cards.add(identity)
            card_text = clean_text(card.get_text(" ", strip=True))
            if not card_text or not card_has_job_signal(card_text, card):
                continue
            href = best_card_href(card, final_url)
            raw_title = extract_card_title(card)
            is_document_posting = is_job_document_url(href)
            self.record_candidate(raw_title or card_text[:120], href)

            detail_attempted = False
            detail_title = ""
            detail: dict[str, str] = {}
            title = raw_title
            if (is_action_label(title) or not is_valid_job_title(title)) and not is_document_posting:
                if href:
                    detail_attempted = True
                    detail = self.fetch_detail_page(href)
                    detail_title = detail.get("title", "")
                    title = detail_title

            if not is_valid_job_title(title):
                self.reject_candidate(
                    raw_title or card_text[:120],
                    rejection_reason(title or raw_title),
                    href,
                    surrounding_text=card_text,
                    detail_attempted=detail_attempted,
                    detail_title=detail_title,
                    company=company,
                    job_board_url=board_url,
                )
                continue
            if not href:
                self.reject_candidate(title, "missing source URL", "", surrounding_text=card_text, company=company, job_board_url=board_url)
                continue
            if href in seen_hrefs:
                self.reject_candidate(title, "duplicate job URL", href, surrounding_text=card_text, company=company, job_board_url=board_url)
                continue
            seen_hrefs.add(href)
            if not detail and is_document_posting:
                # The official careers card is the authoritative listing and
                # the linked PDF is its stable destination. Avoid decoding a
                # binary document as HTML or launching a PDF viewer.
                detail = {
                    "title": title,
                    "description": card_text,
                    "location": extract_document_location(card),
                    "postedDate": "",
                }
            elif not detail:
                detail_attempted = True
                detail = self.fetch_detail_page(href)

            description = detail.get("description") or card_text
            location = detail.get("location") or extract_location(card_text)
            pay_info = detail.get("payInfo") if isinstance(detail.get("payInfo"), dict) else {}
            if not pay_info or not pay_info.get("payText"):
                pay_info = extract_pay_info(" ".join([description, card_text]))
                pay_info["payExtractionSource"] = "detail_or_card_text"
            if pay_info.get("payText"):
                self.record_pay_extraction(str(pay_info.get("payExtractionSource") or "detail_or_card_text"), description or card_text, pay_info)
            pay_text = str(pay_info.get("payText") or "")
            posted_date = detail.get("postedDate") or extract_posted_date(card_text)
            if not any([description, location, posted_date, href, pay_text]):
                self.reject_candidate(title, "not enough posting evidence", href, surrounding_text=card_text, company=company, job_board_url=board_url)
                continue

            self.save_candidate(title, href)
            jobs.append(
                JobRecord(
                    id=make_job_id(company, title, href),
                    companyId=str(company.get("Company ID") or stable_company_id(company)),
                    companyName=str(company.get("Company Name") or ""),
                    title=title,
                    location=location,
                    payMin=pay_info.get("payMin"),
                    payMax=pay_info.get("payMax"),
                    payText=pay_text,
                    payPeriod=str(pay_info.get("payPeriod") or "unknown"),
                    payCurrency=str(pay_info.get("payCurrency") or "USD"),
                    postedDate=posted_date,
                    sourceUrl=href,
                    jobPlatform=str(company.get("Job Platform") or "Generic"),
                    description=description,
                    descriptionSnippet=description[:240],
                    collectedAt=datetime.now().astimezone().replace(microsecond=0).isoformat(),
                    rawData={
                        "collector": self.__class__.__name__,
                        "sourceType": source_type,
                        "finalUrl": final_url,
                        "officialJobDocument": is_document_posting,
                    },
                )
            )
        return jobs

    def extract_json_ld_jobs(self, company: dict[str, Any], page_url: str, soup: BeautifulSoup, source_type: str) -> list[JobRecord]:
        jobs: list[JobRecord] = []
        for posting in iter_job_posting_json_ld(soup):
            title = normalize_job_title(str(posting.get("title") or ""))
            source_url = str(posting.get("url") or page_url)
            self.record_candidate(title, source_url)
            if not is_valid_job_title(title):
                self.reject_candidate(title, rejection_reason(title), source_url, company=company)
                continue
            description = html_to_text(str(posting.get("description") or ""))
            location = json_ld_location(posting.get("jobLocation"))
            pay_info = extract_json_ld_pay_info(posting.get("baseSalary"))
            if pay_info.get("payText"):
                self.record_pay_extraction("json_ld", str(posting.get("baseSalary") or ""), pay_info)
            self.save_candidate(title, source_url)
            jobs.append(
                JobRecord(
                    id=make_job_id(company, title, source_url),
                    companyId=str(company.get("Company ID") or stable_company_id(company)),
                    companyName=str(company.get("Company Name") or ""),
                    title=title,
                    location=location,
                    workType=json_ld_employment_type(posting.get("employmentType")),
                    payMin=pay_info.get("payMin"),
                    payMax=pay_info.get("payMax"),
                    payText=str(pay_info.get("payText") or ""),
                    payPeriod=str(pay_info.get("payPeriod") or "unknown"),
                    payCurrency=str(pay_info.get("payCurrency") or "USD"),
                    postedDate=str(posting.get("datePosted") or ""),
                    sourceUrl=source_url,
                    jobPlatform=str(company.get("Job Platform") or "Generic"),
                    description=description,
                    descriptionSnippet=description[:240],
                    collectedAt=datetime.now().astimezone().replace(microsecond=0).isoformat(),
                    rawData={
                        "collector": self.__class__.__name__,
                        "sourceType": source_type,
                        "hiringOrganization": json_ld_org(posting.get("hiringOrganization")),
                        "validThrough": str(posting.get("validThrough") or ""),
                    },
                )
            )
        return jobs

    def fetch_detail_page(self, href: str) -> dict[str, str]:
        context = getattr(self, "_detail_context", None)
        if context is not None:
            detail = self.fetch_detail_page_with_browser(context, href)
            if detail:
                return detail
        try:
            response = self.get(href)
        except Exception:
            return {}
        soup = BeautifulSoup(response.text, "html.parser")
        schema_jobs = list(iter_job_posting_json_ld(soup))
        if schema_jobs:
            posting = schema_jobs[0]
            description = html_to_text(str(posting.get("description") or ""))
            pay_info = extract_json_ld_pay_info(posting.get("baseSalary"))
            return {
                "title": normalize_job_title(str(posting.get("title") or "")),
                "description": description,
                "location": json_ld_location(posting.get("jobLocation")),
                "payText": str(pay_info.get("payText") or ""),
                "payInfo": pay_info,
                "postedDate": str(posting.get("datePosted") or ""),
            }
        title = extract_detail_title(soup)
        text = clean_text(soup.get_text(" ", strip=True))
        pay_info = extract_pay_info(text)
        pay_info["payExtractionSource"] = "detail_page_text"
        return {
            "title": title,
            "description": text,
            "location": extract_location(text),
            "payText": str(pay_info.get("payText") or ""),
            "payInfo": pay_info,
            "postedDate": extract_posted_date(text),
        }

    def fetch_detail_page_with_browser(self, context, href: str) -> dict[str, str]:
        page = context.new_page()
        try:
            safe_page_goto(page, href, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            page.wait_for_timeout(1200)
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")
            schema_jobs = list(iter_job_posting_json_ld(soup))
            if schema_jobs:
                posting = schema_jobs[0]
                description = html_to_text(str(posting.get("description") or ""))
                pay_info = extract_json_ld_pay_info(posting.get("baseSalary"))
                if not pay_info.get("payText"):
                    text = clean_text(page.locator("body").inner_text(timeout=8000))
                    pay_info = extract_pay_info(text)
                    pay_info["payExtractionSource"] = "browser_detail_page_text"
                    description = description or text
                return {
                    "title": normalize_job_title(str(posting.get("title") or "")),
                    "description": description,
                    "location": json_ld_location(posting.get("jobLocation")),
                    "payText": str(pay_info.get("payText") or ""),
                    "payInfo": pay_info,
                    "postedDate": str(posting.get("datePosted") or ""),
                }
            text = clean_text(page.locator("body").inner_text(timeout=8000))
            pay_info = extract_pay_info(text)
            pay_info["payExtractionSource"] = "browser_detail_page_text"
            return {
                "title": extract_detail_title(soup),
                "description": text,
                "location": extract_location(text),
                "payText": str(pay_info.get("payText") or ""),
                "payInfo": pay_info,
                "postedDate": extract_posted_date(text),
            }
        except Exception:
            return {}
        finally:
            page.close()


def likely_job_cards(soup: BeautifulSoup) -> list[Tag]:
    cards: list[Tag] = []
    for selector in CARD_SELECTORS:
        for node in soup.select(selector):
            if isinstance(node, Tag):
                cards.append(node)
    cards.extend(job_document_cards(soup))
    return cards[:500]


def job_document_cards(soup: BeautifulSoup) -> list[Tag]:
    """Return compact careers-page cards that link an official job PDF."""

    cards: list[Tag] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if not is_job_document_url(href):
            continue
        signal = clean_text(
            f"{anchor.get_text(' ', strip=True)} {anchor.get('title') or ''} {href}"
        ).casefold()
        if not any(term in signal for term in ("job", "position", "career", "employment")):
            continue
        for parent in list(anchor.parents)[:6]:
            if not isinstance(parent, Tag):
                continue
            if parent.name not in {"article", "section", "li", "div", "td"}:
                continue
            if parent.select_one("h1, h2, h3, h4, h5, h6") is None:
                continue
            text = clean_text(parent.get_text(" ", strip=True))
            if 10 <= len(text) <= 2500:
                cards.append(parent)
                break
    return cards


def card_has_job_signal(text: str, card: Tag) -> bool:
    lowered = text.lower()
    hrefs = " ".join(str(anchor.get("href") or "") for anchor in card.find_all("a", href=True)).lower()
    return any(term in f"{lowered} {hrefs}" for term in ["job", "career", "opening", "position", "recruit", "department", "location"])


def extract_card_title(card: Tag) -> str:
    if any(is_job_document_url(str(anchor.get("href") or "")) for anchor in card.find_all("a", href=True)):
        # Careers cards commonly place a region/branch heading before the job
        # heading. The heading nearest the PDF action is the posting title.
        for node in reversed(card.select("h1, h2, h3, h4, h5, h6")):
            title = normalize_job_title(node.get_text(" ", strip=True))
            if title and not is_action_label(title):
                return title
    for selector in ["h1", "h2", "h3", "h4", "h5", "h6", "strong", "[class*='title']", "[class*='heading']", "[data-testid*='title']"]:
        node = card.select_one(selector)
        if node:
            title = normalize_job_title(node.get_text(" ", strip=True))
            if title:
                return title
    for anchor in card.find_all("a", href=True):
        text = normalize_job_title(anchor.get_text(" ", strip=True))
        if text and not is_action_label(text):
            return text
    return ""


def best_card_href(card: Tag, base_url: str) -> str:
    anchors = card.find_all("a", href=True)
    scored: list[tuple[int, str]] = []
    for anchor in anchors:
        href = urljoin(base_url, str(anchor.get("href") or ""))
        text = clean_text(anchor.get_text(" ", strip=True)).lower()
        value = f"{text} {href.lower()}"
        score = 0
        if any(term in value for term in ["job", "career", "opening", "position", "recruit"]):
            score += 10
        if is_action_label(text):
            score += 5
        if href:
            scored.append((score, href))
    if not scored:
        return ""
    return sorted(scored, key=lambda item: item[0], reverse=True)[0][1]


def is_job_document_url(url: str) -> bool:
    return urlsplit(str(url or "")).path.casefold().endswith(".pdf")


def extract_document_location(card: Tag) -> str:
    headings = [
        normalize_job_title(node.get_text(" ", strip=True))
        for node in card.select("h1, h2, h3, h4, h5, h6")
    ]
    headings = [heading for heading in headings if heading]
    return headings[-2] if len(headings) >= 2 else ""


def extract_detail_title(soup: BeautifulSoup) -> str:
    for selector in ["h1", "h2", "meta[property='og:title']", "title"]:
        node = soup.select_one(selector)
        if not node:
            continue
        value = node.get("content") if node.name == "meta" else node.get_text(" ", strip=True)
        title = normalize_job_title(str(value or ""))
        title = re.sub(r"\s*[-|]\s*(careers|jobs|employment).*$", "", title, flags=re.IGNORECASE)
        if title:
            return title
    return ""


def iter_job_posting_json_ld(soup: BeautifulSoup):
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            payload = json.loads(script.string or script.get_text() or "{}")
        except Exception:
            continue
        yield from walk_json_ld(payload)


def walk_json_ld(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from walk_json_ld(item)
    elif isinstance(value, dict):
        types = value.get("@type") or value.get("type") or []
        if isinstance(types, str):
            types = [types]
        if any(str(item).lower() == "jobposting" for item in types):
            yield value
        for key in ["@graph", "graph", "itemListElement"]:
            if key in value:
                yield from walk_json_ld(value[key])


def json_ld_location(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(filter(None, [json_ld_location(item) for item in value]))
    if not isinstance(value, dict):
        return ""
    address = value.get("address")
    if isinstance(address, dict):
        return clean_text(", ".join(str(address.get(key) or "") for key in ["addressLocality", "addressRegion", "addressCountry"] if address.get(key)))
    return clean_text(str(value.get("name") or ""))


def json_ld_salary(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    amount = value.get("value")
    currency = value.get("currency") or ""
    if isinstance(amount, dict):
        min_value = amount.get("minValue")
        max_value = amount.get("maxValue")
        unit = amount.get("unitText") or ""
        if min_value and max_value:
            return clean_text(f"{currency} {min_value}-{max_value} {unit}")
        if amount.get("value"):
            return clean_text(f"{currency} {amount.get('value')} {unit}")
    return ""


def json_ld_employment_type(value: Any) -> str:
    text = " ".join(value) if isinstance(value, list) else str(value or "")
    lowered = text.lower()
    if "remote" in lowered:
        return "Remote"
    return "Not Listed"


def json_ld_org(value: Any) -> str:
    return str(value.get("name") or "") if isinstance(value, dict) else ""


def is_action_label(value: str) -> bool:
    return normalize_job_title(value).lower() in ACTION_LABELS


def html_to_text(value: str) -> str:
    return clean_text(BeautifulSoup(value, "html.parser").get_text(" ", strip=True))


def extract_location(text: str) -> str:
    match = re.search(r"([A-Za-z .'-]+,\s*[A-Z]{2})\b", text)
    return clean_text(match.group(1)) if match else ""


def extract_pay_text(text: str) -> str:
    match = re.search(r"\$\s?\d[\d,]*(?:\.\d{2})?(?:\s*(?:-|to|–)\s*\$?\s?\d[\d,]*(?:\.\d{2})?)?", text)
    return clean_text(match.group(0)) if match else ""


def extract_posted_date(text: str) -> str:
    match = re.search(r"\b(posted|date posted)\s*:?\s*([A-Za-z0-9, /-]{4,40})", text, flags=re.IGNORECASE)
    return clean_text(match.group(2)) if match else ""


def looks_like_job_link(text: str, href: str) -> bool:
    value = f"{text} {href}".lower()
    if len(text) < 4:
        return False
    if any(blocked in value for blocked in ["login", "sign in", "privacy", "benefits", "culture"]):
        return False
    return any(term in value for term in ["job", "career", "opening", "position", "recruitment"])


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def dedupe_jobs(jobs: list[JobRecord]) -> list[JobRecord]:
    unique: dict[str, JobRecord] = {}
    for job in jobs[:100]:
        unique[job.sourceUrl or f"{job.companyId}:{job.title}:{job.location}"] = job
    return list(unique.values())
