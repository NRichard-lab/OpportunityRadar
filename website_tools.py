from __future__ import annotations

import logging
import time
from collections import deque
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import feedparser
import requests
from bs4 import BeautifulSoup

from config import CAREERS_KEYWORDS, COMMON_FEED_PATHS, MAX_CRAWL_PAGES, POLITE_DELAY_SECONDS, REQUEST_TIMEOUT, USER_AGENT
from job_platforms import detect_job_platform
from search_tools import registered_domain


LOGGER = logging.getLogger(__name__)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def normalize_url(url: str) -> str:
    if not url:
        return ""
    return url if "://" in url else f"https://{url}"


def can_fetch(url: str, session: requests.Session) -> bool:
    parsed = urlparse(normalize_url(url))
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        response = session.get(robots_url, timeout=REQUEST_TIMEOUT)
        if response.status_code >= 400:
            return True
        parser.parse(response.text.splitlines())
        return parser.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def fetch_html(url: str, session: requests.Session) -> tuple[str, str]:
    normalized = normalize_url(url)
    if not can_fetch(normalized, session):
        raise PermissionError(f"robots.txt disallows {normalized}")
    time.sleep(POLITE_DELAY_SECONDS)
    response = session.get(normalized, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type and response.text[:100].lstrip().lower().startswith("<?xml"):
        raise ValueError(f"non-HTML content at {normalized}")
    return response.url, response.text


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


def find_careers_page(official_url: str, session: requests.Session) -> tuple[str, str, list[str]]:
    if not official_url:
        return "", "", ["no official website available"]

    notes: list[str] = []
    visited: set[str] = set()
    queue: deque[str] = deque([normalize_url(official_url)])
    platform_urls: list[str] = []

    while queue and len(visited) < MAX_CRAWL_PAGES:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

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
                platform_urls.append(href)
            if link_matches_careers(text, href):
                careers_matches.append((text, href))

        if platform_urls:
            return platform_urls[0], detect_job_platform(platform_urls[0]), notes
        if careers_matches:
            href = careers_matches[0][1]
            return href, detect_job_platform(href), notes

        for text, href in links:
            if href not in visited and is_same_registered_domain(official_url, href):
                queue.append(href)

    if platform_urls:
        return platform_urls[0], detect_job_platform(platform_urls[0]), notes

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
        response = session.get(feed_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
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
