from __future__ import annotations

import re


REJECTED_EXACT_TITLES = {
    "general employment application",
    "skip to content",
    "careers",
    "career",
    "search open positions",
    "search jobs",
    "view open positions",
    "view current openings",
    "current openings",
    "job openings",
    "apply now",
    "apply",
    "learn more",
    "read more",
    "view details",
    "view details (opens an external site)",
    "opens an external site",
    "join our team",
    "home",
    "menu",
    "privacy",
    "privacy policy",
    "terms",
    "terms of use",
    "accessibility",
    "sign in",
    "login",
    "log in",
    "remote work",
    "benefits",
    "culture",
    "locations",
    "equal opportunity",
    "talent community",
    "open positions all departments",
    "search open positions",
    "job alerts",
    "create alert",
    "subscribe",
    "share",
    "back",
    "next",
    "previous",
    "filter",
    "sort",
}

REJECTED_TITLE_PARTS = {
    "view details",
    "opens an external site",
    "skip navigation",
    "current openings",
    "equal opportunity employer",
    "join our talent community",
    "cookie",
    "captcha",
    "forgot password",
    "create account",
    "job alerts",
    "powered by",
    "remote work",
    "equal opportunity",
    "talent community",
    "search jobs",
    "search open positions",
    "open positions",
    "apply now",
    "learn more",
    "read more",
    "job alerts",
    "create alert",
    "accessibility",
    "locations",
    "benefits",
    "culture",
    "remote work",
}

GENERIC_ONLY_WORDS = {
    "apply",
    "career",
    "careers",
    "job",
    "jobs",
    "menu",
    "opening",
    "openings",
    "position",
    "positions",
    "search",
    "view",
    "details",
    "external",
    "site",
    "apply",
    "learn",
    "more",
    "read",
    "alerts",
    "alert",
    "subscribe",
    "filter",
    "sort",
    "share",
    "back",
    "next",
    "previous",
}

ROLE_NOUNS = {
    "accountant",
    "consultant",
    "teller",
    "banker",
    "representative",
    "receptionist",
    "specialist",
    "manager",
    "analyst",
    "engineer",
    "administrator",
    "coordinator",
    "processor",
    "officer",
    "associate",
    "assistant",
    "director",
    "supervisor",
    "advisor",
    "clerk",
    "technician",
    "architect",
    "developer",
    "lead",
    "senior",
    "branch",
    "member",
    "service",
    "loan",
    "mortgage",
    "compliance",
    "risk",
    "operations",
    "finance",
    "accounting",
    "it",
    "systems",
    "security",
    "infrastructure",
    "core",
    "data",
}


def normalize_job_title(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", title or "").strip()
    cleaned = re.sub(r"^(job title|title)\s*[:\-]\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned[:180]


def is_valid_job_title(title: str) -> bool:
    normalized = normalize_job_title(title)
    lowered = normalized.lower()
    if not normalized or len(normalized) < 4:
        return False
    if lowered in REJECTED_EXACT_TITLES:
        return False
    if any(part in lowered for part in REJECTED_TITLE_PARTS):
        return False
    if re.fullmatch(r"\d+\+?\s+days?\s+ago", lowered):
        return False
    words = re.findall(r"[a-z0-9]+", lowered)
    if not words:
        return False
    if all(word in GENERIC_ONLY_WORDS for word in words):
        return False
    if len(words) <= 2 and any(word in GENERIC_ONLY_WORDS for word in words):
        return False
    if len(normalized) > 140 and any(term in lowered for term in ["apply", "join", "search", "opening"]):
        return False
    if job_title_score(normalized) < 1:
        return False
    return True


def rejection_reason(title: str) -> str:
    normalized = normalize_job_title(title)
    lowered = normalized.lower()
    if not normalized:
        return "blank title"
    if len(normalized) < 4:
        return "too short"
    if lowered in REJECTED_EXACT_TITLES:
        return "navigation/button label"
    if any(part in lowered for part in REJECTED_TITLE_PARTS):
        return "boilerplate text"
    if re.fullmatch(r"\d+\+?\s+days?\s+ago", lowered):
        return "posted date text"
    words = re.findall(r"[a-z0-9]+", lowered)
    if not words:
        return "no title words"
    if all(word in GENERIC_ONLY_WORDS for word in words):
        return "generic navigation words only"
    if len(words) <= 2 and any(word in GENERIC_ONLY_WORDS for word in words):
        return "short generic title"
    if len(normalized) > 140 and any(term in lowered for term in ["apply", "join", "search", "opening"]):
        return "long UI block"
    if job_title_score(normalized) < 1:
        return "not enough real job title signal"
    return "unknown"


def job_title_score(title: str) -> int:
    normalized = normalize_job_title(title)
    lowered = normalized.lower()
    words = re.findall(r"[a-z0-9]+", lowered)
    score = 0
    if any(word in ROLE_NOUNS for word in words):
        score += 2
    if "member service" in lowered:
        score += 2
    if any(part in lowered for part in REJECTED_TITLE_PARTS):
        score -= 4
    if any(word in GENERIC_ONLY_WORDS for word in words):
        score -= 1
    if len(words) >= 3:
        score += 1
    return score
