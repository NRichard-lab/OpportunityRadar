from __future__ import annotations

import logging
import time
from collections import deque
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import feedparser
import requests
from bs4 import BeautifulSoup

from backend.outbound_security import SSRFProtectedSession
from config import CAREERS_KEYWORDS, COMMON_FEED_PATHS, MAX_CRAWL_PAGES, POLITE_DELAY_SECONDS, REQUEST_TIMEOUT, USER_AGENT
from job_platforms import detect_job_platform
from search_tools import registered_domain, request_with_limited_retries, validate_and_canonicalize_url


LOGGER = logging.getLogger(__name__)


def make_session() -> requests.Session:
    session = SSRFProtectedSession()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def normalize_url(url: str) -> str:
    if not url:
        return ""
    return url if "://" in url else f"https://{url}"


def can_fetch(url: str, session: requests.Session) -> bool:
    normalized = normalize_url(url)
    parsed_url = urlparse(normalized)
    origin = f"{parsed_url.scheme.lower()}://{parsed_url.netloc.lower()}"
    cache = getattr(session, "_opportunity_radar_robots_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(session, "_opportunity_radar_robots_cache", cache)

    if origin not in cache:
        robots_url = f"{origin}/robots.txt"
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            response = request_with_limited_retries(session, robots_url, timeout=REQUEST_TIMEOUT)
            if response.status_code >= 400:
                response.close()
                cache[origin] = None
            else:
                parser.parse(response.text.splitlines())
                response.close()
                cache[origin] = parser
        except Exception:
            cache[origin] = None

    parser = cache[origin]
    return True if parser is None else parser.can_fetch(USER_AGENT, normalized)


def fetch_html(url: str, session: requests.Session) -> tuple[str, str]:
    normalized = normalize_url(url)
    if not can_fetch(normalized, session):
        raise PermissionError(f"robots.txt disallows {normalized}")
    time.sleep(POLITE_DELAY_SECONDS)
    final_url, response = validate_and_canonicalize_url(
        normalized,
        session,
        require_html=False,
        reject_disallowed=False,
    )
    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type and response.text[:100].lstrip().lower().startswith("<?xml"):
        response.close()
        raise ValueError(f"non-HTML content at {normalized}")
    html = response.text
    response.close()
    return final_url, html


def is_same_registered_domain(source_url: str, target_url: str) -> bool:
    return registered_domain(source_url) == registered_domain(target_url)


def link_matches_careers(text: str, href: str) -> bool:
    haystack = f"{text} {href}".lower()
    return any(keyword in haystack for keyword in CAREERS_KEYWORDS)


def extract_links(base_url: str, html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute = urldefrag(urljoin(base_url, href))[0]
        text = anchor.get_text(" ", strip=True)
        links.append((text, absolute))
    return links


def extract_embedded_urls(base_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for iframe in soup.find_all("iframe"):
        source = str(iframe.get("src") or iframe.get("data-src") or "").strip()
        if not source or source.startswith(("javascript:", "data:")):
            continue
        absolute = urldefrag(urljoin(base_url, source))[0]
        if absolute not in urls:
            urls.append(absolute)
    return urls


def find_careers_page(
    official_url: str,
    session: requests.Session,
    *,
    initial_html: str = "",
    initial_final_url: str = "",
    cancelled: object | None = None,
) -> tuple[str, str, list[str]]:
    if not official_url:
        return "", "", ["no official website available"]

    notes: list[str] = []
    visited: set[str] = set()
    first_url = normalize_url(initial_final_url or official_url)
    queue: deque[str] = deque([first_url])
    platform_urls: list[str] = []
    cached_html = str(initial_html or "")

    while queue and len(visited) < MAX_CRAWL_PAGES:
        if cancelled is not None and getattr(cancelled, "is_set", lambda: False)():
            raise InterruptedError("Cancelled by user.")
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        if cached_html:
            final_url, html = first_url, cached_html
            cached_html = ""
        else:
            try:
                final_url, html = fetch_html(url, session)
            except Exception as exc:
                notes.append(f"could not inspect {url}: {exc}")
                continue

        links = extract_links(final_url, html)
        careers_matches: list[tuple[str, str]] = []
        for embedded_url in extract_embedded_urls(final_url, html):
            if detect_job_platform(embedded_url):
                platform_urls.append(embedded_url)
        for text, href in links:
            platform = detect_job_platform(href)
            if platform:
                if link_matches_careers(text, ""):
                    platform_urls.append(href)
                continue
            if link_matches_careers(text, href):
                careers_matches.append((text, href))

        if platform_urls:
            platform_url = platform_urls[0]
            return platform_url, detect_job_platform(platform_url), notes
        if careers_matches:
            href = careers_matches[0][1]
            return href, detect_job_platform(href), notes

        for text, href in links:
            if href not in visited and is_same_registered_domain(first_url, href):
                queue.append(href)

    if platform_urls:
        platform_url = platform_urls[0]
        return platform_url, detect_job_platform(platform_url), notes

    notes.append(f"no careers link found after inspecting {len(visited)} page(s)")
    return "", "", notes


def discover_declared_feeds(page_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    feeds: list[str] = []
    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel", [])).lower()
        feed_type = (link.get("type") or "").lower()
        href = link.get("href")
        if href and "alternate" in rel and feed_type in {"application/rss+xml", "application/atom+xml"}:
            feeds.append(urljoin(page_url, href))
    return feeds


def validate_feed(feed_url: str, session: requests.Session) -> bool:
    try:
        if not can_fetch(feed_url, session):
            return False
        time.sleep(POLITE_DELAY_SECONDS)
        _, response = validate_and_canonicalize_url(
            feed_url,
            session,
            require_html=False,
            reject_disallowed=False,
        )
        parsed = feedparser.parse(response.content)
        response.close()
        return not parsed.bozo and bool(parsed.feed) and (bool(parsed.entries) or bool(parsed.version))
    except Exception:
        return False


def find_feed(official_url: str, careers_url: str, session: requests.Session) -> tuple[str, bool, list[str]]:
    notes: list[str] = []
    candidates: list[str] = []
    pages_to_inspect = [url for url in [official_url, careers_url] if url]

    for page_url in pages_to_inspect:
        try:
            final_url, html = fetch_html(page_url, session)
            candidates.extend(discover_declared_feeds(final_url, html))
        except Exception as exc:
            notes.append(f"could not inspect feeds on {page_url}: {exc}")

    bases = []
    for page_url in pages_to_inspect:
        parsed = urlparse(normalize_url(page_url))
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in bases:
            bases.append(base)
    for base in bases:
        candidates.extend(urljoin(base, path) for path in COMMON_FEED_PATHS)

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if validate_feed(candidate, session):
            return candidate, True, notes

    return "", False, notes
