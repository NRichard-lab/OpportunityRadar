from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
import tldextract
from bs4 import BeautifulSoup

from config import DISALLOWED_RESULT_DOMAINS, OFFICIAL_WEBSITE_SEARCH_PHRASES, REQUEST_TIMEOUT


LOGGER = logging.getLogger(__name__)

STATE_NAMES = {
    "CO": "colorado",
    "WA": "washington",
}


@dataclass
class SearchCandidate:
    url: str
    title: str = ""
    snippet: str = ""


def normalize_company_name(name: str) -> str:
    cleaned = name.lower()
    cleaned = re.sub(r"\b(credit union|federal credit union|bank|national association|n\.a\.|na)\b", "", cleaned)
    return re.sub(r"[^a-z0-9]+", "", cleaned)


def company_identity_tokens(name: str) -> list[str]:
    ignored = {"credit", "union", "federal", "bank", "national", "association", "the", "and"}
    return [
        word
        for word in re.findall(r"[a-z0-9]+", name.lower())
        if word not in ignored and len(word) >= 3
    ]


def domain_matches_company(company: str, url: str) -> bool:
    domain_text = normalize_company_name(registered_domain(url).split(".")[0])
    normalized_company = normalize_company_name(company)
    if not domain_text or not normalized_company:
        return False
    if domain_text == normalized_company:
        return True
    return any(token == domain_text or token in domain_text for token in company_identity_tokens(company))


def registered_domain(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    extracted = tldextract.extract(parsed.netloc)
    if not extracted.domain or not extracted.suffix:
        return parsed.netloc.lower()
    return f"{extracted.domain}.{extracted.suffix}".lower()


def site_root(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return f"{parsed.scheme}://{parsed.netloc}/"


def resolve_site_root(url: str, session: requests.Session) -> str:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return site_root(response.url)
    except Exception:
        return site_root(url)


def is_disallowed_result(url: str) -> bool:
    domain = registered_domain(url)
    return any(domain == blocked or domain.endswith(f".{blocked}") for blocked in DISALLOWED_RESULT_DOMAINS)


def likely_official_domain_guesses(company: str) -> list[str]:
    tokens = company_identity_tokens(company)
    guesses: list[str] = []
    candidates = []
    company_lower = company.lower()
    if tokens and "credit union" in company_lower:
        candidates.append(tokens[0])
        candidates.append("".join(tokens))
        candidates.append(f"{tokens[0]}cu")
    elif tokens and "bank" in company_lower:
        candidates.append(f"e{tokens[0]}")
    for candidate in dict.fromkeys(candidates):
        for suffix in ["org", "com", "net"]:
            guesses.append(f"https://www.{candidate}.{suffix}/")
            guesses.append(f"https://{candidate}.{suffix}/")
    return guesses


def search_web(company: str, city: str = "", state: str = "", max_results: int = 8) -> list[SearchCandidate]:
    try:
        from ddgs import DDGS
    except ImportError as exc:
        LOGGER.warning("ddgs is not installed: %s", exc)
        return []

    candidates: list[SearchCandidate] = []
    seen: set[str] = set()

    location = " ".join(part for part in [city, state] if part).strip()
    phrase_templates = list(OFFICIAL_WEBSITE_SEARCH_PHRASES)
    if location:
        phrase_templates = [
            "{company} {location} official website",
            "{company} {location} careers",
            *phrase_templates,
        ]

    for phrase_template in phrase_templates:
        phrase = phrase_template.format(company=company, location=location)
        try:
            with DDGS(timeout=REQUEST_TIMEOUT) as ddgs:
                for result in ddgs.text(phrase, max_results=max_results):
                    url = result.get("href") or result.get("url") or ""
                    if not url or url in seen or is_disallowed_result(url):
                        continue
                    seen.add(url)
                    candidates.append(
                        SearchCandidate(
                            url=url,
                            title=result.get("title", ""),
                            snippet=result.get("body", ""),
                        )
                    )
        except Exception as exc:
            LOGGER.warning("Search failed for '%s': %s", phrase, exc)

    return candidates


def evaluate_official_website(
    company: str,
    url: str,
    session: requests.Session,
    city: str = "",
    state: str = "",
    require_location: bool = False,
) -> tuple[str, str]:
    """Return confidence and notes for a possible official website."""
    confidence = "Low"
    notes: list[str] = []

    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        response.raise_for_status()
        domain_related = domain_matches_company(company, response.url)
        if domain_related:
            confidence = "Medium"
            notes.append("domain resembles company name")
        soup = BeautifulSoup(response.text, "html.parser")
        page_text = " ".join(
            value.strip()
            for value in [soup.title.string if soup.title else "", soup.get_text(" ", strip=True)[:5000]]
            if value
        ).lower()
        company_words = company_identity_tokens(company)
        matched_words = sum(1 for word in company_words if word in page_text)
        finance_terms = ["bank", "credit union", "checking", "savings", "loans", "mortgage", "online banking"]
        finance_matches = sum(1 for term in finance_terms if term in page_text)

        if matched_words:
            notes.append("company name appears on homepage")
        if finance_matches:
            notes.append("financial institution terms appear on homepage")
        location_terms = [city.lower().strip(), STATE_NAMES.get(state.upper().strip(), "")]
        location_terms = [term for term in location_terms if term]
        location_matches = any(term in page_text for term in location_terms)
        if location_matches:
            notes.append("location context appears on homepage")

        if domain_related and matched_words and finance_matches:
            confidence = "High"
        elif domain_related and (matched_words or finance_matches):
            confidence = "Medium"
        elif not domain_related and not finance_matches and not matched_words:
            confidence = "Low"
        if require_location and confidence == "High" and location_terms and not location_matches:
            confidence = "Medium"
            notes.append("location context was not verified")
    except Exception as exc:
        domain_related = domain_matches_company(company, url)
        if domain_related:
            confidence = "Medium"
            notes.append("domain resembles company name")
        notes.append(f"could not fully verify homepage: {exc}")

    return confidence, "; ".join(notes)


def choose_official_website(
    company: str,
    known_website: str,
    session: requests.Session,
    city: str = "",
    state: str = "",
) -> tuple[str, str, str]:
    if known_website:
        confidence, notes = evaluate_official_website(company, known_website, session)
        return known_website, confidence, f"used known website; {notes}".strip("; ")

    best_guess = ("", "Low", "")
    score_by_confidence = {"Low": 1, "Medium": 2, "High": 3}
    for guess in likely_official_domain_guesses(company):
        if is_disallowed_result(guess):
            continue
        confidence, notes = evaluate_official_website(company, guess, session)
        if score_by_confidence[confidence] > score_by_confidence[best_guess[1]]:
            best_guess = (resolve_site_root(guess, session), confidence, notes)
        if confidence == "High":
            return best_guess[0], confidence, f"verified likely official domain; {notes}"
    if best_guess[1] == "Medium":
        return best_guess[0], best_guess[1], f"verified likely official domain; {best_guess[2]}"

    candidates = search_web(company, city=city, state=state)
    if not candidates:
        return "", "Low", "no search candidates found"

    best_url = ""
    best_confidence = "Low"
    best_notes = ""

    for candidate in candidates:
        candidate_url = site_root(candidate.url)
        confidence, notes = evaluate_official_website(
            company,
            candidate_url,
            session,
            city=city,
            state=state,
            require_location=True,
        )
        resolved_url = resolve_site_root(candidate_url, session)
        if score_by_confidence[confidence] > score_by_confidence[best_confidence]:
            best_url = resolved_url
            best_confidence = confidence
            best_notes = notes or f"selected from search result: {candidate.title}"
            if confidence == "High":
                break
        elif not best_url:
            best_url = resolved_url
            best_notes = notes or f"selected from search result: {candidate.title}"

    return best_url, best_confidence, best_notes
