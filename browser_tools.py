from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from backend.outbound_security import install_playwright_url_guard, launch_playwright_chromium, safe_page_goto
from job_platforms import detect_job_platform


JOB_BOARD_PHRASES = [
    "Apply Now",
    "View Open Positions",
    "Current Openings",
    "Search Jobs",
    "Search and Apply",
    "Job Openings",
    "See Open Positions",
    "Join Our Team",
    "Careers",
    "Jobs",
    "Employment",
    "Work With Us",
    "Job Opportunities",
    "Talent Community",
]

LOW_VALUE_TERMS = [
    "login",
    "log in",
    "sign in",
    "application status",
    "benefits",
    "culture",
    "internship",
    "volunteer",
    "loan",
    "mortgage application",
    "membership application",
    "account application",
    "online banking",
    "credit card application",
]

HIGH_VALUE_TERMS = [
    "open positions",
    "current openings",
    "job openings",
    "search jobs",
    "search and apply",
    "see open positions",
    "view open positions",
]


@dataclass
class BrowserCandidate:
    index: int
    text: str
    href: str
    tag: str
    score: int


def discover_job_board_with_browser(careers_url: str, company_name: str) -> dict[str, str | None]:
    if not careers_url:
        return not_found("No careers URL provided for browser automation.")

    local_browser_path = Path(__file__).resolve().parent / ".playwright-browsers"
    if local_browser_path.exists() and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browser_path)

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "final_url": None,
            "platform": None,
            "status": "Needs Review",
            "notes": "Playwright is not installed. Run: pip install playwright; python -m playwright install chromium.",
            "clicked_text": None,
        }

    with sync_playwright() as playwright:
        browser = launch_playwright_chromium(playwright, headless=True)
        try:
            return _discover_with_launched_browser(
                browser,
                careers_url,
                PlaywrightTimeoutError,
            )
        finally:
            # Releasing this leased browser is mandatory even when Chromium crashes
            # during context creation or navigation.
            browser.close()


def _discover_with_launched_browser(browser, careers_url: str, playwright_timeout_error) -> dict[str, str | None]:
    context = browser.new_context(
        user_agent=(
            "OpportunityRadar/1.0 "
            "(public job board URL discovery; no applications or form submissions)"
        ),
        service_workers="block",
    )
    install_playwright_url_guard(context)
    page = context.new_page()
    try:
        safe_page_goto(page, careers_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=10000)
    except playwright_timeout_error:
        pass

    iframe_url = find_platform_iframe(page)
    if iframe_url:
        platform = detect_job_platform(iframe_url)
        return {
            "final_url": iframe_url,
            "platform": platform or None,
            "status": "Completed",
            "notes": f"Found embedded public job board iframe for {platform or 'job board'}.",
            "clicked_text": "Embedded job board iframe",
        }

    candidates = choose_candidates(page, careers_url)
    if not candidates:
        return not_found("No job board button/link found with browser automation.")

    last_error = ""
    for candidate in candidates[:6]:
        before_pages = len(context.pages)
        candidate_page = None
        try:
            candidate_page = context.new_page()
            safe_page_goto(candidate_page, careers_url, wait_until="domcontentloaded", timeout=30000)
            candidate_page.wait_for_load_state("networkidle", timeout=7000)
            before_pages = len(context.pages)
            locator = candidate_page.locator("a, button, [role='button'], [role='link']").nth(candidate.index)
            locator.scroll_into_view_if_needed(timeout=3000)
            with candidate_page.expect_popup(timeout=5000) as popup_info:
                try:
                    locator.click(timeout=8000, no_wait_after=False)
                except playwright_timeout_error:
                    raise
            final_page = popup_info.value
            final_page.wait_for_load_state("domcontentloaded", timeout=12000)
        except playwright_timeout_error:
            final_page = context.pages[-1] if len(context.pages) > before_pages else candidate_page
        except Exception as exc:
            last_error = str(exc)
            try:
                if candidate_page is not None:
                    candidate_page.close()
            except Exception:
                pass
            continue

        try:
            final_page.wait_for_timeout(1500)
        except Exception:
            pass
        final_url = final_page.url or candidate.href or careers_url
        if candidate.href and final_url.rstrip("/") == careers_url.rstrip("/"):
            final_url = candidate.href
        rejected_reason = job_board_rejection_reason(final_url)
        if rejected_reason:
            last_error = f"Rejected {final_url}: {rejected_reason}"
            continue
        platform = detect_job_platform(final_url)
        if platform or score_candidate(candidate.text, final_url, candidate.tag) >= 45:
            notes = (
                f"Clicked {candidate.text} and landed on {platform}."
                if platform
                else f"Clicked {candidate.text} and landed on a public careers/jobs URL."
            )
            return {
                "final_url": final_url,
                "platform": platform or None,
                "status": "Completed",
                "notes": notes,
                "clicked_text": candidate.text,
            }

    return {
        "final_url": None,
        "platform": None,
        "status": "Needs Review",
        "notes": f"Browser automation found candidates but no public job board navigation completed. Last error: {last_error}",
        "clicked_text": None,
    }


def choose_candidate(page, base_url: str) -> BrowserCandidate | None:
    candidates = choose_candidates(page, base_url)
    return candidates[0] if candidates else None


def choose_candidates(page, base_url: str) -> list[BrowserCandidate]:
    candidates: list[BrowserCandidate] = []
    locator = page.locator(
        "a, button, [role='button'], [role='link'], "
        "a[href*='jobs'], a[href*='careers'], a[href*='employment'], "
        "a[href*='recruit'], a[href*='workday'], a[href*='adp'], "
        "a[href*='paylocity'], a[href*='icims'], a[href*='greenhouse'], a[href*='lever']"
    )
    count = locator.count()

    for index in range(count):
        element = locator.nth(index)
        try:
            visible = element.is_visible(timeout=500)
            text = clean_text(element.inner_text(timeout=1000))
            aria = clean_text(element.get_attribute("aria-label") or "")
            title = clean_text(element.get_attribute("title") or "")
            href = element.get_attribute("href") or ""
            tag = (element.evaluate("el => el.tagName") or "").lower()
            button_type = (element.get_attribute("type") or "").lower()
        except Exception:
            continue

        label = clean_text(" ".join(part for part in [text, aria, title] if part))
        full_href = urljoin(base_url, href) if href else ""
        if not visible or (not label and not full_href):
            continue
        if full_href and job_board_rejection_reason(full_href):
            continue
        if button_type == "submit":
            continue
        if any(term in f"{label} {full_href}".lower() for term in LOW_VALUE_TERMS):
            continue
        if not detect_job_platform(full_href) and not any(
            phrase.lower() in f"{label} {full_href}".lower() for phrase in JOB_BOARD_PHRASES
        ):
            continue

        score = score_candidate(label, full_href, tag)
        candidates.append(BrowserCandidate(index=index, text=label or full_href, href=full_href, tag=tag, score=score))

    return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)


def find_platform_iframe(page) -> str:
    iframe_locator = page.locator("iframe")
    count = iframe_locator.count()
    for index in range(count):
        try:
            src = iframe_locator.nth(index).get_attribute("src") or ""
        except Exception:
            continue
        if src and detect_job_platform(src) and not job_board_rejection_reason(src):
            return src
    return ""


def score_candidate(label: str, href: str, tag: str) -> int:
    value = 0
    lower_label = label.lower()
    lower_href = href.lower()
    if tag == "a" and href:
        value += 10
    if detect_job_platform(href):
        value += 60
    if any(term in lower_label for term in HIGH_VALUE_TERMS):
        value += 35
    if lower_label in HIGH_VALUE_TERMS:
        value += 30
    if "apply now" in lower_label:
        value += 16
    if "careers" in lower_label:
        value += 8
    if any(term in lower_href for term in ["job", "career", "recruit", "opening", "employment"]):
        value += 12
    if len(label) > 90:
        value -= 24
    return value


def clean_text(value: str) -> str:
    return " ".join(value.split())


def job_board_rejection_reason(url: str) -> str:
    # Import lazily because job_board_discovery invokes this browser module.
    from job_board_discovery import rejection_reason

    return rejection_reason(url)


def not_found(notes: str) -> dict[str, str | None]:
    return {
        "final_url": None,
        "platform": None,
        "status": "Needs Review",
        "notes": notes,
        "clicked_text": None,
    }
