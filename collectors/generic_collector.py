from __future__ import annotations

import json
import hashlib
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
from job_evidence import (
    careers_page_context_reason,
    evaluate_generic_candidate,
    navigation_chrome_reason,
    non_job_destination_reason,
    non_job_text_reason,
    page_job_list_structure_reason,
    page_looks_like_soft_404,
)
from job_validation import is_valid_job_title, normalize_job_title, rejection_reason
from job_tools import CollectionNotAuthoritative, JobRecord, make_job_id


# Ceiling for Playwright context actions that do not pass an explicit timeout=
# (e.g. page.content()). Generous enough never to bite a healthy slow page.
BROWSER_ACTION_TIMEOUT_MS = 60000


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
        except CollectionNotAuthoritative:
            # Browser reached the page but it did not render (SPA shell). The
            # static fallback would only fetch the same shell, so do not retry
            # it -- propagate the non-authoritative result and retain existing jobs.
            raise
        except Exception as exc:
            self.reject_candidate(url, f"browser render failed; falling back to HTTP: {exc}", url, company=company)
            # The browser did NOT complete. Anything the static fallback scrapes
            # is incomplete by construction and is never authoritative -- even if
            # it finds some postings. Hand any partial finds back for additive
            # retention and report the refresh as incomplete.
            partial_jobs = self.collect_with_http(company, url, source_type)
            raise CollectionNotAuthoritative(
                f"Browser render failed for {url}; static fallback is incomplete "
                f"({len(partial_jobs)} partial listing(s)).",
                partial_jobs=partial_jobs,
            ) from exc

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
                # Give every context action a ceiling so a call without an
                # explicit timeout= (notably page.content()) cannot hang the
                # owning maintenance thread if the renderer wedges. Calls that
                # pass their own timeout= still override this.
                context.set_default_timeout(BROWSER_ACTION_TIMEOUT_MS)
                context.set_default_navigation_timeout(BROWSER_ACTION_TIMEOUT_MS)
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
                try:
                    visible_text = clean_text(page.locator("body").inner_text(timeout=5000))
                except Exception:
                    visible_text = ""
                jobs = self.parse_listing_html(company, url, self.final_url_after_redirect or url, html, source_type)
                if not jobs:
                    landing = self.final_url_after_redirect or url
                    uncertain = self.zero_result_uncertainty(landing, html, visible_text)
                    if uncertain:
                        raise CollectionNotAuthoritative(uncertain)
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

    def zero_result_uncertainty(self, landing_url: str, html: str, visible_text: str) -> str:
        """Explain why an empty generic parse must not be treated as a real zero.

        A generic page that yields nothing is only an authoritative "no
        openings" when the page says so outright, or when it rendered a job
        list that is genuinely empty. An error page, an unrendered app shell,
        or a page we simply did not understand is uncertain, and an uncertain
        parse must never prune a company's existing jobs.
        """
        soft_404 = page_looks_like_soft_404(visible_text)
        if soft_404:
            return (
                f"{landing_url} returned an error/placeholder page (\"{soft_404}\"); "
                f"refusing to record an authoritative zero-job result."
            )
        if page_states_no_openings(visible_text):
            return ""
        if looks_like_unrendered_spa(html, visible_text):
            return (
                f"{landing_url} loaded but rendered no listing content "
                f"(single-page-app shell); refusing to record an authoritative "
                f"zero-job result."
            )
        if not getattr(self, "_page_job_list_reason", ""):
            return (
                f"{landing_url} rendered no recognizable job-list structure and did "
                f"not state that there are no openings; the zero-job result is "
                f"uncertain and is not authoritative."
            )
        return ""

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
        self._careers_context_reason = careers_page_context_reason(final_url, soup)
        self._page_job_list_reason = page_job_list_structure_reason(soup)
        schema_jobs = self.extract_json_ld_jobs(company, final_url, soup, source_type)
        if schema_jobs:
            return schema_jobs

        static_jobs = self.extract_static_heading_jobs(company, final_url, soup, source_type)
        if static_jobs:
            return static_jobs

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

            # Cheap structural rejections first: site chrome and destinations
            # that cannot be a posting are discarded before any detail fetch,
            # so a footer link never costs a request and never reaches storage.
            chrome_reason = navigation_chrome_reason(card)
            if chrome_reason:
                self.reject_candidate(
                    raw_title or card_text[:120], chrome_reason, href,
                    surrounding_text=card_text, company=company, job_board_url=board_url,
                )
                continue
            destination_reason = non_job_destination_reason(href, page_url=final_url)
            if destination_reason:
                self.reject_candidate(
                    raw_title or card_text[:120], destination_reason, href,
                    surrounding_text=card_text, company=company, job_board_url=board_url,
                )
                continue

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

            # Positive evidence is required before anything is stored. A card
            # that merely clears the title blocklist is not a posting: the page
            # must identify it as one, through a recognized ATS job-detail URL,
            # a requisition identifier, or placement in a verified job list that
            # publishes job metadata.
            evidence_text = " ".join(
                part for part in [card_text, description, location, posted_date, pay_text] if part
            )
            verdict = evaluate_generic_candidate(
                title=title,
                href=href,
                node=card,
                text=evidence_text,
                page_url=final_url,
                document_posting=is_document_posting,
                careers_context=bool(getattr(self, "_careers_context_reason", "")),
            )
            if not verdict.accepted:
                self.reject_candidate(
                    title, verdict.reason, href,
                    surrounding_text=card_text,
                    detail_attempted=detail_attempted,
                    detail_title=detail_title,
                    company=company,
                    job_board_url=board_url,
                )
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
                        "jobEvidence": list(verdict.signals),
                    },
                )
            )
        return jobs

    def extract_json_ld_jobs(self, company: dict[str, Any], page_url: str, soup: BeautifulSoup, source_type: str) -> list[JobRecord]:
        jobs: list[JobRecord] = []
        for posting in iter_job_posting_json_ld(soup):
            title = normalize_job_title(str(posting.get("title") or ""))
            source_url = structured_posting_url(posting, page_url, soup, title)
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
                        "structuredSource": True,
                        "sourceType": source_type,
                        "identifier": json_ld_identifier(posting.get("identifier")),
                        "hiringOrganization": json_ld_org(posting.get("hiringOrganization")),
                        "validThrough": str(posting.get("validThrough") or ""),
                    },
                )
            )
        return jobs

    def extract_static_heading_jobs(
        self,
        company: dict[str, Any],
        page_url: str,
        soup: BeautifulSoup,
        source_type: str,
    ) -> list[JobRecord]:
        """Extract real postings published inline below an explicit openings heading."""

        jobs: list[JobRecord] = []
        company_id = str(company.get("Company ID") or stable_company_id(company))
        for section_heading in static_openings_headings(soup):
            heading_name = section_heading.name
            before = len(jobs)
            for title_heading, body_nodes in static_posting_groups(section_heading, heading_name):
                raw_title = normalize_job_title(title_heading.get_text(" ", strip=True))
                title, location = split_static_title_location(raw_title)
                body_text = clean_text(" ".join(node.get_text(" ", strip=True) for node in body_nodes))
                source_url = static_posting_url(page_url, raw_title)
                self.record_candidate(title or raw_title, source_url)
                evidence = f"{raw_title} {body_text}".casefold()
                if not is_valid_job_title(title) or not any(
                    term in evidence for term in ("opening", "position", "responsibil", "qualification", "pay:", "salary")
                ):
                    self.reject_candidate(
                        title or raw_title,
                        rejection_reason(title or raw_title),
                        source_url,
                        surrounding_text=body_text,
                        company=company,
                        job_board_url=page_url,
                    )
                    continue
                pay_info = extract_pay_info(body_text)
                if pay_info.get("payText"):
                    self.record_pay_extraction("static careers section", body_text, pay_info)
                self.save_candidate(title, source_url)
                jobs.append(
                    JobRecord(
                        id=make_job_id(company, title, source_url),
                        companyId=company_id,
                        companyName=str(company.get("Company Name") or ""),
                        title=title,
                        location=location,
                        payMin=pay_info.get("payMin"),
                        payMax=pay_info.get("payMax"),
                        payText=str(pay_info.get("payText") or ""),
                        payPeriod=str(pay_info.get("payPeriod") or "unknown"),
                        payCurrency=str(pay_info.get("payCurrency") or "USD"),
                        sourceUrl=source_url,
                        jobPlatform=str(company.get("Job Platform") or "Company Careers Site"),
                        description=body_text,
                        descriptionSnippet=body_text[:360],
                        collectedAt=datetime.now().astimezone().replace(microsecond=0).isoformat(),
                        rawData={
                            "collector": self.__class__.__name__,
                            "sourceType": source_type,
                            "officialCareersPageListing": True,
                            "sectionHeading": clean_text(section_heading.get_text(" ", strip=True)),
                        },
                    )
                )
            if len(jobs) == before:
                # No <hN>-structured postings under this heading. Many small
                # employers instead publish a bare list of links (often to a PDF
                # job description) with an email/"to apply" instruction.
                jobs.extend(
                    self.extract_static_openings_link_rows(company, page_url, section_heading, source_type)
                )
            if jobs:
                break
        return jobs

    def extract_static_openings_link_rows(
        self,
        company: dict[str, Any],
        page_url: str,
        section_heading: Tag,
        source_type: str,
    ) -> list[JobRecord]:
        # A bare run of links only means "these are the openings" on a page that
        # is itself a careers page. On a marketing page the same shape is a menu,
        # so refuse to read it as a posting list at all.
        careers_reason = getattr(self, "_careers_context_reason", "")
        if not careers_reason:
            return []
        rows, section_text = static_openings_link_rows(section_heading)
        if not rows or not section_has_apply_signal(section_text):
            return []
        company_id = str(company.get("Company ID") or stable_company_id(company))
        jobs: list[JobRecord] = []
        seen: set[str] = set()
        for anchor, anchor_text, href in rows:
            raw_title = normalize_job_title(anchor_text)
            title, location = split_static_title_location(raw_title)
            title = title or raw_title
            destination = urljoin(page_url, href) if href else static_posting_url(page_url, raw_title)
            self.record_candidate(title, destination)
            row_reason = (
                navigation_chrome_reason(anchor)
                or non_job_text_reason(title)
                or non_job_destination_reason(destination, page_url=page_url)
            )
            if row_reason:
                self.reject_candidate(
                    title, row_reason, destination,
                    surrounding_text=section_text[:400], company=company, job_board_url=page_url,
                )
                continue
            if not is_valid_job_title(title) or destination in seen:
                self.reject_candidate(
                    title, rejection_reason(title), destination,
                    surrounding_text=section_text[:400], company=company, job_board_url=page_url,
                )
                continue
            is_document = is_job_document_url(destination)
            seen.add(destination)
            self.save_candidate(title, destination)
            jobs.append(
                JobRecord(
                    id=make_job_id(company, title, destination),
                    companyId=company_id,
                    companyName=str(company.get("Company Name") or ""),
                    title=title,
                    location=location,
                    sourceUrl=destination,
                    jobPlatform=str(company.get("Job Platform") or "Company Careers Site"),
                    description=f"Listed under “{clean_text(section_heading.get_text(' ', strip=True))}” on the official careers page.",
                    descriptionSnippet="",
                    collectedAt=datetime.now().astimezone().replace(microsecond=0).isoformat(),
                    rawData={
                        "collector": self.__class__.__name__,
                        "sourceType": source_type,
                        "officialCareersPageListing": True,
                        "officialJobDocument": is_document,
                        "sectionHeading": clean_text(section_heading.get_text(" ", strip=True)),
                        "jobEvidence": ["openings heading link list", careers_reason],
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


def structured_posting_url(posting: dict[str, Any], page_url: str, soup: BeautifulSoup, title: str) -> str:
    explicit_url = clean_text(str(posting.get("url") or ""))
    if explicit_url:
        return urljoin(page_url, explicit_url)
    identifier = json_ld_identifier(posting.get("identifier"))
    if identifier:
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "")
            if re.search(rf"/jobs/{re.escape(identifier)}(?:[./?]|$)", href, flags=re.IGNORECASE):
                return urljoin(page_url, href)
    fragment_basis = identifier or hashlib.sha256(title.casefold().encode("utf-8")).hexdigest()[:16]
    fragment = re.sub(r"[^A-Za-z0-9_-]+", "-", fragment_basis).strip("-")
    return f"{page_url.split('#', 1)[0]}#job-{fragment}"


def json_ld_identifier(value: Any) -> str:
    if isinstance(value, dict):
        return clean_text(str(value.get("value") or value.get("name") or ""))
    return clean_text(str(value or ""))


def static_openings_headings(soup: BeautifulSoup) -> list[Tag]:
    matches: list[Tag] = []
    pattern = re.compile(
        r"^(?:available|current|open)\s+(?:job\s+)?(?:positions|openings|opportunities)$|^employment\s+opportunities$",
        flags=re.IGNORECASE,
    )
    for heading in soup.select("h1, h2, h3, h4, h5, h6"):
        if isinstance(heading, Tag) and pattern.fullmatch(clean_text(heading.get_text(" ", strip=True))):
            matches.append(heading)
    return matches


_HEADING_RANK = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_APPLY_SIGNALS = (
    "to apply",
    "how to apply",
    "forward your resume",
    "submit your resume",
    "send your resume",
    "email your resume",
    "send us your resume",
    "apply in person",
    "apply now",
    "apply online",
    "application",
    "resume and cover letter",
    "mailto:",
    "we are hiring",
    "now hiring",
    "career opportunit",
)


def section_has_apply_signal(section_text: str) -> bool:
    low = clean_text(section_text or "").casefold()
    return any(signal in low for signal in _APPLY_SIGNALS)


def static_openings_link_rows(section_heading: Tag) -> tuple[list[tuple[Tag, str, str]], str]:
    """Anchor ``(anchor, text, href)`` rows that follow an openings heading, until
    the next same-or-higher heading, plus the collected text for an apply check."""

    rank = _HEADING_RANK.get(str(section_heading.name or "").lower(), 6)
    rows: list[tuple[Tag, str, str]] = []
    text_parts: list[str] = []
    for sibling in section_heading.next_siblings:
        if not isinstance(sibling, Tag):
            continue
        if _HEADING_RANK.get(sibling.name or "", 99) <= rank:
            break
        text_parts.append(sibling.get_text(" ", strip=True))
        for anchor in sibling.find_all("a"):
            href = str(anchor.get("href") or "").strip()
            label = clean_text(anchor.get_text(" ", strip=True))
            if not label or href.lower().startswith(("mailto:", "tel:", "#", "javascript:")):
                continue
            rows.append((anchor, label, href))
    return rows, " ".join(part for part in text_parts if part)


def static_posting_groups(section_heading: Tag, heading_name: str) -> list[tuple[Tag, list[Tag]]]:
    groups: list[tuple[Tag, list[Tag]]] = []
    current_heading: Tag | None = None
    current_body: list[Tag] = []
    for sibling in section_heading.next_siblings:
        if not isinstance(sibling, Tag):
            continue
        if sibling.name == heading_name:
            if current_heading is not None:
                groups.append((current_heading, current_body))
            current_heading = sibling
            current_body = []
            continue
        if current_heading is not None:
            current_body.append(sibling)
    if current_heading is not None:
        groups.append((current_heading, current_body))
    return groups


def split_static_title_location(raw_title: str) -> tuple[str, str]:
    match = re.match(r"^(.*?)\s+[\-\u2013\u2014]\s+(.+?\s+Location)$", raw_title, flags=re.IGNORECASE)
    if not match:
        return raw_title, ""
    return normalize_job_title(match.group(1)), clean_text(match.group(2))


def static_posting_url(page_url: str, title: str) -> str:
    digest = hashlib.sha256(title.casefold().encode("utf-8")).hexdigest()[:16]
    return f"{page_url.split('#', 1)[0]}#position-{digest}"


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


# Deliberately specific: a match here green-lights treating "zero jobs" as an
# authoritative result (existing jobs may be pruned), so generic phrases like
# "no results found" or a bare "check back" are excluded -- they also appear on
# unrendered search-UI shells.
_EXPLICIT_NO_OPENINGS = (
    "no current openings",
    "no current job openings",
    "no open positions",
    "no open positions at this time",
    "no openings at this time",
    "no job openings at this time",
    "no positions available at this time",
    "there are currently no openings",
    "there are no open positions",
    "we do not have any openings",
    "we don't have any openings",
    "no vacancies at this time",
    "0 jobs found",
    "0 open positions",
)

_SPA_SHELL_MARKERS = (
    "${",  # unrendered JS template interpolation (e.g. Phenom "${pageStateData...}")
    "{{",  # unrendered mustache/angular/vue interpolation
    "pagestatedata",  # Phenom People client bundle
    "please enable javascript",
    "enable javascript to run this app",
    "you need to enable javascript",
    "we're sorry but",  # common Vue "doesn't work properly without JavaScript" shell
    "doesn't work properly without javascript",
    "this application requires javascript",
)

_EMPTY_FRAMEWORK_ROOT = re.compile(
    r'<div[^>]+id=["\'](?:root|app|__next|main|react-root|ember-app)["\'][^>]*>\s*</div>',
    re.IGNORECASE,
)


def page_states_no_openings(visible_text: str) -> bool:
    """True when the rendered page explicitly says there are no openings.

    That IS an authoritative zero -- the page rendered and told us so.
    """
    low = clean_text(visible_text or "").lower()
    return any(phrase in low for phrase in _EXPLICIT_NO_OPENINGS)


def looks_like_unrendered_spa(html: str, visible_text: str = "") -> bool:
    """Heuristic: the document loaded but its client framework never rendered.

    Only consulted when zero listings were parsed AND the page did not state an
    explicit "no openings" message, so a genuine empty-but-rendered careers page
    is still treated as an authoritative zero.
    """
    low = (html or "").lower()
    if any(marker in low for marker in _SPA_SHELL_MARKERS):
        return True
    script_count = low.count("<script")
    stripped = clean_text(re.sub(r"<[^>]+>", " ", low))
    if _EMPTY_FRAMEWORK_ROOT.search(low) and script_count >= 2:
        return True
    # Almost no human-visible text but a heavy script payload.
    text_sample = visible_text if visible_text else stripped
    if script_count >= 3 and len(clean_text(text_sample)) < 400:
        return True
    return False


def dedupe_jobs(jobs: list[JobRecord]) -> list[JobRecord]:
    unique: dict[str, JobRecord] = {}
    for job in jobs[:100]:
        unique[job.sourceUrl or f"{job.companyId}:{job.title}:{job.location}"] = job
    return list(unique.values())
