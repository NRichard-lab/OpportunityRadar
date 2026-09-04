"""Positive-evidence gating for generic careers-page parsing.

The generic collector walks a rendered page and treats promising-looking
elements as postings. Historically the only barrier between a page element and
a stored job was :func:`job_validation.is_valid_job_title` -- a blocklist of
navigation labels plus a small heuristic score. Anything that cleared the
blocklist became a job, which is how site chrome ("Open an Account", "Find a
Branch/ATM"), soft-404 copy ("Oops. We're still building this path"), raw
contact addresses, and ordinary marketing calls to action were imported as
openings.

This module inverts that contract. A generic-page element is only a job when
the page gives *positive* evidence that it is one, and the element is not
recognizable site chrome. Blocklists remain as a cheap first pass, but they are
never the only thing standing between a page and a stored posting.

Nothing here applies to structured collectors: an ATS API response already
identifies each item as a job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

from job_platforms import hostname_matches_domain


# ---------------------------------------------------------------------------
# Site chrome
# ---------------------------------------------------------------------------

# Landmark elements that never contain a company's own postings.
NAVIGATION_TAGS = {"nav", "header", "footer", "aside"}

# ARIA landmark roles for the same regions.
NAVIGATION_ROLES = {
    "navigation",
    "banner",
    "contentinfo",
    "menu",
    "menubar",
    "menuitem",
    "search",
    "dialog",
    "alertdialog",
    "toolbar",
}

# Class/id tokens that identify chrome. Matched as whole tokens so a legitimate
# "job-listing-header" row is not mistaken for the page header.
NAVIGATION_TOKEN = re.compile(
    r"(?:^|[-_\s])(?:"
    r"nav|navbar|navigation|mainnav|topnav|subnav|menu|submenu|megamenu|dropdown"
    r"|header|masthead|footer|breadcrumb|breadcrumbs|social|share|sidebar|widget"
    r"|utility|topbar|toolbar|skip|skiplink|cookie|consent|banner|modal|popup"
    r"|offcanvas|drawer|hamburger|sitemap|pagination|pager"
    r")(?:$|[-_\s])",
    flags=re.IGNORECASE,
)

# How far up the tree to look for a chrome ancestor.
CHROME_ANCESTOR_DEPTH = 12


def _attribute_tokens(node: Any) -> str:
    """Whitespace-padded class/id/role text for token matching."""
    if node is None or not hasattr(node, "get"):
        return ""
    parts: list[str] = []
    class_value = node.get("class") or []
    if isinstance(class_value, str):
        parts.append(class_value)
    else:
        parts.extend(str(item) for item in class_value)
    for name in ("id", "role", "aria-label", "data-testid"):
        value = node.get(name)
        if value:
            parts.append(str(value))
    return f" {' '.join(parts)} "


def navigation_chrome_reason(node: Any) -> str:
    """Return why ``node`` sits in site chrome, or an empty string."""
    current = node
    for _ in range(CHROME_ANCESTOR_DEPTH):
        if current is None or not hasattr(current, "name"):
            break
        name = str(getattr(current, "name", "") or "").lower()
        if name in NAVIGATION_TAGS:
            return f"inside <{name}> site chrome"
        role = str((current.get("role") if hasattr(current, "get") else "") or "").strip().lower()
        if role in NAVIGATION_ROLES:
            return f"inside role=\"{role}\" landmark"
        tokens = _attribute_tokens(current)
        match = NAVIGATION_TOKEN.search(tokens)
        if match:
            return f"inside \"{match.group(0).strip()}\" navigation/chrome container"
        current = getattr(current, "parent", None)
    return ""


# ---------------------------------------------------------------------------
# Destinations that cannot be a job posting
# ---------------------------------------------------------------------------

NON_JOB_SCHEMES = {"mailto", "tel", "sms", "javascript", "callto", "fax"}

SOCIAL_AND_AGGREGATOR_HOSTS = (
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "pinterest.com",
    "threads.net",
    "snapchat.com",
    "reddit.com",
    "nextdoor.com",
    "yelp.com",
    "glassdoor.com",
    "indeed.com",
    "ziprecruiter.com",
    "simplyhired.com",
    "monster.com",
    "careerbuilder.com",
)

# Whole path segments that identify a non-posting page. Segment matching avoids
# rejecting a real posting slug such as "/careers/business-banker-1042".
NON_JOB_SEGMENTS: dict[str, str] = {}


def _register_segments(reason: str, segments: Iterable[str]) -> None:
    for segment in segments:
        NON_JOB_SEGMENTS[segment] = reason


_register_segments(
    "account/product application link",
    (
        "open-an-account",
        "openanaccount",
        "open-account",
        "openaccount",
        "account-opening",
        "apply-for-a-loan",
        "apply-for-loan",
        "loan-application",
        "mortgage-application",
        "card-application",
        "switch-kit",
    ),
)
_register_segments(
    "branch/ATM link",
    (
        "location",
        "locations",
        "branch",
        "branches",
        "atm",
        "atms",
        "branch-locator",
        "locator",
        "find-a-branch",
        "find-a-location",
        "find-an-atm",
        "hours",
        "directions",
    ),
)
_register_segments(
    "login/registration link",
    (
        "login",
        "log-in",
        "signin",
        "sign-in",
        "signon",
        "sign-on",
        "logon",
        "logout",
        "sign-out",
        "register",
        "registration",
        "enroll",
        "enrollment",
        "password",
        "forgot-password",
        "reset-password",
        "online-banking",
        "onlinebanking",
        "mobile-banking",
        "account-login",
    ),
)
_register_segments(
    "contact/email link",
    (
        "contact",
        "contact-us",
        "contactus",
        "email-us",
        "feedback",
        "appointment",
        "schedule-appointment",
        "book-appointment",
    ),
)
_register_segments(
    "privacy/legal/accessibility page link",
    (
        "privacy",
        "privacy-policy",
        "privacy-notice",
        "legal",
        "terms",
        "terms-of-use",
        "terms-and-conditions",
        "disclosure",
        "disclosures",
        "disclaimer",
        "accessibility",
        "sitemap",
        "site-map",
        "cookies",
        "cookie-policy",
        "security",
        "fraud",
        "patriot-act",
        "esign",
        "eeo",
        "equal-opportunity",
    ),
)
_register_segments(
    "marketing/product page link",
    (
        "rates",
        "products",
        "personal",
        "business",
        "commercial",
        "checking",
        "savings",
        "loans",
        "mortgage",
        "mortgages",
        "credit-cards",
        "creditcards",
        "insurance",
        "investments",
        "wealth",
        "promotions",
        "offers",
        "calculators",
        "financial-education",
        "resources",
        "news",
        "newsroom",
        "press",
        "blog",
        "events",
        "community",
        "about",
        "about-us",
        "aboutus",
        "history",
        "leadership",
        "board-of-directors",
        "testimonials",
        "faq",
        "faqs",
        "help",
        "support",
    ),
)

# Path suffixes stripped before segment comparison.
_PAGE_SUFFIX = re.compile(r"\.(?:html?|aspx?|php|jsp|cfm)$", flags=re.IGNORECASE)

# Segments that identify a careers area; they outrank the marketing list above
# so "/about/careers/teller-1042" is still a candidate.
CAREERS_SEGMENTS = {
    "career",
    "careers",
    "job",
    "jobs",
    "employment",
    "opening",
    "openings",
    "opportunity",
    "opportunities",
    "join-our-team",
    "join-us",
    "work-with-us",
    "workhere",
    "work-here",
    "hiring",
    "recruiting",
    "recruitment",
    "vacancy",
    "vacancies",
    "positions",
    "apply",
}


def _path_segments(path: str) -> list[str]:
    segments: list[str] = []
    for raw in str(path or "").split("/"):
        cleaned = _PAGE_SUFFIX.sub("", raw.strip().lower())
        if cleaned:
            segments.append(cleaned)
    return segments


def non_job_destination_reason(url: str, *, page_url: str = "") -> str:
    """Return why ``url`` cannot be a job posting, or an empty string."""
    value = str(url or "").strip()
    if not value:
        return ""
    if value.startswith("#"):
        return "in-page anchor, not a posting URL"
    scheme = urlsplit(value).scheme.lower()
    if scheme in NON_JOB_SCHEMES:
        return f"{scheme}: link, not a posting URL"

    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host:
        for social in SOCIAL_AND_AGGREGATOR_HOSTS:
            if hostname_matches_domain(host, social):
                return f"social/aggregator link ({social})"

    segments = _path_segments(parsed.path)
    if not segments:
        page_host = (urlsplit(str(page_url or "")).hostname or "").lower().rstrip(".")
        if host and page_host and host != page_host:
            return ""
        return "site root, not a posting URL"
    if any(segment in CAREERS_SEGMENTS for segment in segments):
        return ""
    for segment in segments:
        reason = NON_JOB_SEGMENTS.get(segment)
        if reason:
            return reason
    return ""


# ---------------------------------------------------------------------------
# Text that is never a posting title
# ---------------------------------------------------------------------------

NON_JOB_TEXT_PHRASES = (
    "open an account",
    "open your account",
    "find a branch",
    "find an atm",
    "branch locator",
    "atm locator",
    "locations & hours",
    "locations and hours",
    "contact us",
    "email us",
    "call us",
    "apply for a loan",
    "get started",
    "get pre-approved",
    "schedule an appointment",
    "make an appointment",
    "routing number",
    "online banking",
    "mobile banking",
    "enroll now",
    "make a payment",
    "view rates",
    "compare rates",
    "download the app",
    "see all locations",
    "switch to",
    "join now",
    "become a member",
    "refer a friend",
    "sign up for",
    "follow us",
    "read our blog",
    "watch the video",
)

SOFT_404_PHRASES = (
    "still building this path",
    "page not found",
    "page cannot be found",
    "page you requested",
    "page you are looking for",
    "page you're looking for",
    "404 error",
    "error 404",
    "under construction",
    "coming soon",
    "temporarily unavailable",
    "no longer available",
    "we couldn't find",
    "we could not find",
    "oops",
    "whoops",
    "something went wrong",
    "access denied",
    "check back soon",
)

# First-person/marketing sentence shapes. Real posting titles are noun phrases.
_SENTENCE_SHAPE = re.compile(
    r"\b(?:we're|we are|we've|we have|you'll|you will|you can|let's|our team is|"
    r"thanks for|sorry|please try|click here)\b",
    flags=re.IGNORECASE,
)

_EMAIL_ONLY = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_PHONE_ONLY = re.compile(r"^[-+().\s\d]{7,}$")
_URL_ONLY = re.compile(r"^(?:https?://|www\.)\S+$", flags=re.IGNORECASE)


def _phrase_pattern(phrases: Iterable[str]) -> re.Pattern[str]:
    """Compile ``phrases`` as whole-word alternatives.

    Plain substring matching turns short phrases into traps: "oops" is inside
    "Troops" and "Coops", so a real posting title was rejected as error copy and
    a page listing it was demoted to non-authoritative. Word boundaries are only
    added where the phrase actually starts or ends on a word character, so
    punctuated phrases such as "404 error" still match.
    """
    alternatives = []
    for phrase in sorted(set(phrases), key=len, reverse=True):
        escaped = re.escape(phrase)
        prefix = r"\b" if phrase[:1].isalnum() else ""
        suffix = r"\b" if phrase[-1:].isalnum() else ""
        alternatives.append(f"{prefix}{escaped}{suffix}")
    return re.compile("|".join(alternatives), flags=re.IGNORECASE)


_SOFT_404_PATTERN = _phrase_pattern(SOFT_404_PHRASES)
_NON_JOB_TEXT_PATTERN = _phrase_pattern(NON_JOB_TEXT_PHRASES)


def _matched_phrase(pattern: re.Pattern[str], value: str) -> str:
    """Return the most specific phrase ``pattern`` finds in ``value``.

    Regex alternation returns the leftmost match, which on "Oops. We're still
    building this path" is the near-useless "oops". The phrase is quoted back to
    an operator as the reason a page was rejected, so prefer the longest match.
    """
    matches = [match.group(0) for match in pattern.finditer(value)]
    if not matches:
        return ""
    return max(matches, key=len).casefold()


def non_job_text_reason(text: str) -> str:
    """Return why ``text`` is page furniture rather than a posting title."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ""
    if _EMAIL_ONLY.match(value):
        return "email address, not a job title"
    if _URL_ONLY.match(value):
        return "bare URL, not a job title"
    if _PHONE_ONLY.match(value) and any(char.isdigit() for char in value):
        return "phone number, not a job title"
    soft_404 = _matched_phrase(_SOFT_404_PATTERN, value)
    if soft_404:
        return f"error/placeholder page text (\"{soft_404}\")"
    marketing = _matched_phrase(_NON_JOB_TEXT_PATTERN, value)
    if marketing:
        return f"marketing call to action (\"{marketing}\")"
    if _SENTENCE_SHAPE.search(value):
        return "marketing sentence, not a job title"
    return ""


def page_looks_like_soft_404(visible_text: str) -> str:
    """Return the soft-404 phrase a rendered page shows, or an empty string."""
    normalized = re.sub(r"\s+", " ", str(visible_text or "")).strip()
    if not normalized:
        return ""
    # Only the leading copy: a long careers page that happens to mention
    # "coming soon" further down is not a soft 404.
    return _matched_phrase(_SOFT_404_PATTERN, normalized[:800])


# ---------------------------------------------------------------------------
# Positive evidence
# ---------------------------------------------------------------------------

# Vendor job-detail URL shapes. A match is strong evidence on its own: the link
# points at a specific requisition on a recognized applicant tracking system.
ATS_JOB_DETAIL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("greenhouse.io", re.compile(r"/jobs/\d+", re.I)),
    ("lever.co", re.compile(r"/[^/]+/[0-9a-f]{8}-[0-9a-f-]{27,}", re.I)),
    ("myworkdayjobs.com", re.compile(r"/job/", re.I)),
    ("icims.com", re.compile(r"/jobs/\d+", re.I)),
    ("applicantpro.com", re.compile(r"/jobs/\d+", re.I)),
    ("isolvedhire.com", re.compile(r"/jobs/\d+", re.I)),
    ("ultipro.com", re.compile(r"/opportunitydetail", re.I)),
    ("workforcenow.adp.com", re.compile(r"/recruitment/recruitment\.html|jobid=", re.I)),
    ("recruiting.adp.com", re.compile(r"/srccar/public/", re.I)),
    ("paycomonline.net", re.compile(r"/viewjobdetails|/jobs/\d+", re.I)),
    ("recruitingbypaycor.com", re.compile(r"/career/jobintroduction", re.I)),
    ("paylocity.com", re.compile(r"/details/\d+", re.I)),
    ("jobs.dayforcehcm.com", re.compile(r"/jobs/\d+", re.I)),
    ("csod.com", re.compile(r"/requisition/\d+|/jobdetails", re.I)),
    ("hrmdirect.com", re.compile(r"/employment/job-opening\.php", re.I)),
    ("applytojob.com", re.compile(r"/apply/", re.I)),
    ("jazzhr.com", re.compile(r"/apply/", re.I)),
    ("smartrecruiters.com", re.compile(r"/\d{9,}", re.I)),
    ("jobvite.com", re.compile(r"/job/", re.I)),
    ("bamboohr.com", re.compile(r"/careers/\d+", re.I)),
    ("successfactors.com", re.compile(r"/job/", re.I)),
    ("oraclecloud.com", re.compile(r"/job/", re.I)),
    ("saashr.com", re.compile(r"/ta/|/jobdetail", re.I)),
)

# Query parameters that carry a requisition identifier.
JOB_ID_QUERY_KEYS = (
    "jobid",
    "job_id",
    "reqid",
    "req_id",
    "requisitionid",
    "requisition_id",
    "postingid",
    "posting_id",
    "jobcode",
    "job_code",
    "jvi",
    "jobreqid",
    "opportunityid",
    "vacancyid",
)

# Path segments after which an identifier-shaped segment counts as a job id.
JOB_PATH_ANCHORS = {
    "job",
    "jobs",
    "career",
    "careers",
    "opening",
    "openings",
    "opportunity",
    "opportunities",
    "position",
    "positions",
    "posting",
    "postings",
    "requisition",
    "requisitions",
    "vacancy",
    "vacancies",
    "apply",
    "employment",
}

_IDENTIFIER_SEGMENT = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
_SLUG_SEGMENT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){1,}$")


def ats_job_detail_reason(url: str) -> str:
    """Return the ATS whose job-detail URL shape ``url`` matches."""
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return ""
    target = f"{parsed.path}?{parsed.query}"
    for domain, pattern in ATS_JOB_DETAIL_PATTERNS:
        if hostname_matches_domain(host, domain) and pattern.search(target):
            return domain
    return ""


def _segments_after_a_job_anchor(url: str) -> list[str]:
    """Path segments that directly follow a job/careers anchor segment."""
    segments = _path_segments(urlsplit(str(url or "").strip()).path)
    following: list[str] = []
    for index, segment in enumerate(segments[:-1]):
        if segment not in JOB_PATH_ANCHORS:
            continue
        candidate = segments[index + 1]
        if candidate in JOB_PATH_ANCHORS or candidate in CAREERS_SEGMENTS:
            continue
        following.append(candidate)
    return following


def job_identifier_in_url(url: str) -> str:
    """Return a requisition identifier carried by ``url``, or an empty string.

    Only a *numbered* identifier counts -- a requisition query parameter, or a
    digit-bearing segment after a job anchor such as ``/careers/teller-1042``.
    A digit-free slug is deliberately excluded: ``/careers/employee-benefits``
    and ``/careers/how-to-apply`` have exactly the same shape as a real posting
    slug, so treating one as an identifier would readmit ordinary careers-section
    links as jobs. Those go through :func:`descriptive_job_slug_in_url`, which
    only corroborates other evidence.
    """
    parsed = urlsplit(str(url or "").strip())
    lowered_query = {key.lower(): values for key, values in parse_qs(parsed.query).items()}
    for key in JOB_ID_QUERY_KEYS:
        for value in lowered_query.get(key, []):
            candidate = str(value or "").strip()
            if candidate:
                return candidate

    for segment in _segments_after_a_job_anchor(url):
        if _IDENTIFIER_SEGMENT.match(segment):
            return segment
    return ""


def descriptive_job_slug_in_url(url: str) -> str:
    """Return a digit-free posting-shaped slug after a job anchor, or "".

    ``/careers/commercial-lender`` is plausibly a posting and ``/careers/
    employee-benefits`` plausibly is not, and the URL alone cannot tell them
    apart. This is therefore weak evidence: it is only accepted alongside a
    verified job-list structure, or published job metadata on a confirmed
    careers page.
    """
    for segment in _segments_after_a_job_anchor(url):
        if _IDENTIFIER_SEGMENT.match(segment):
            continue
        if _SLUG_SEGMENT.match(segment):
            return segment
    return ""


# Labelled posting metadata. The label makes the value a published job
# attribute rather than an incidental word in surrounding copy.
_METADATA_LABELS = re.compile(
    r"\b(location|locations|department|division|business unit|job type|employment type|"
    r"job category|category|schedule|shift|hours|status|posted|date posted|posting date|"
    r"closing date|apply by|salary|pay|pay range|pay rate|compensation|wage|rate of pay|"
    r"req(?:uisition)?\s*(?:#|no\.?|number|id)|job\s*(?:#|no\.?|number|id)|fte|reports to)"
    r"\s*[:\-–]",
    flags=re.IGNORECASE,
)

_EMPLOYMENT_TYPE = re.compile(
    r"\b(full[\s-]?time|part[\s-]?time|temporary|seasonal|internship|intern|per[\s-]diem|"
    r"contract|contingent|casual|exempt|non[\s-]?exempt|hourly|salaried)\b",
    flags=re.IGNORECASE,
)

_CITY_STATE = re.compile(r"\b[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+)*,\s*(?:[A-Z]{2}\b|[A-Z][a-z]+\b)")

_EXPLICIT_PAY = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d{2})?\s*(?:-|to|–)?\s*(?:\$\s?\d[\d,]*(?:\.\d{2})?)?\s*"
    r"(?:per\s+(?:hour|hr|year|yr|week|month)|/\s*(?:hour|hr|year|yr)|annually|hourly)",
    flags=re.IGNORECASE,
)


def job_metadata_signals(text: str) -> tuple[str, ...]:
    """Return the posting-metadata signals present in ``text``."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ()
    signals: list[str] = []
    label_match = _METADATA_LABELS.search(value)
    if label_match:
        signals.append(f"labelled metadata ({label_match.group(1).lower()})")
    if _EMPLOYMENT_TYPE.search(value):
        signals.append("employment type")
    if _CITY_STATE.search(value):
        signals.append("city/state location")
    if _EXPLICIT_PAY.search(value):
        signals.append("published pay")
    return tuple(signals)


# Container attributes that identify a rendered list of postings.
_JOB_LIST_SUBJECT = re.compile(
    r"job|opening|position|vacanc|career|requisition|posting", flags=re.IGNORECASE
)
_JOB_LIST_SHAPE = re.compile(
    r"list|listing|results|result|grid|table|board|feed|collection|container|wrapper"
    r"|items|item|rows|row|card|cards|tile|tiles|entry|entries|post|posts",
    flags=re.IGNORECASE,
)
_JOB_TABLE_HEADER_TITLE = re.compile(r"\b(job title|position|title|opening|role)\b", flags=re.IGNORECASE)
_JOB_TABLE_HEADER_DETAIL = re.compile(
    r"\b(location|department|division|category|type|status|posted|closing|salary|pay)\b",
    flags=re.IGNORECASE,
)

JOB_LIST_ANCESTOR_DEPTH = 10


def job_list_container_reason(node: Any) -> str:
    """Return why ``node`` sits inside a verified job-list structure."""
    current = node
    for _ in range(JOB_LIST_ANCESTOR_DEPTH):
        if current is None or not hasattr(current, "name"):
            break
        if hasattr(current, "get"):
            tokens = _attribute_tokens(current)
            if _JOB_LIST_SUBJECT.search(tokens) and _JOB_LIST_SHAPE.search(tokens):
                return "job-list container attributes"
            for name in current.attrs if hasattr(current, "attrs") else {}:
                lowered_name = str(name).lower()
                if lowered_name.startswith("data-") and _JOB_LIST_SUBJECT.search(lowered_name):
                    return f"job-list data attribute ({lowered_name})"
        if str(getattr(current, "name", "") or "").lower() == "table":
            header = _table_header_text(current)
            if header and _JOB_TABLE_HEADER_TITLE.search(header) and _JOB_TABLE_HEADER_DETAIL.search(header):
                return "job table header row"
        current = getattr(current, "parent", None)
    return ""


def _table_header_text(table: Any) -> str:
    try:
        cells = table.select("thead th, thead td, tr:first-child th")
    except Exception:
        return ""
    return " ".join(cell.get_text(" ", strip=True) for cell in cells)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceVerdict:
    """Outcome of evidence gating for one generic-page candidate."""

    accepted: bool
    reason: str = ""
    signals: tuple[str, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.accepted


def evaluate_generic_candidate(
    *,
    title: str,
    href: str,
    node: Any = None,
    text: str = "",
    page_url: str = "",
    schema_backed: bool = False,
    document_posting: bool = False,
    careers_context: bool = False,
) -> EvidenceVerdict:
    """Decide whether a generic-page element may be stored as a job.

    A candidate is accepted only when the page positively identifies it as a
    posting *and* it is not recognizable site chrome. Strong evidence (an ATS
    job-detail URL, a requisition identifier, or JobPosting schema markup)
    stands alone; otherwise the element must sit in a verified job-list
    structure and carry published job metadata.
    """

    chrome = navigation_chrome_reason(node) if node is not None else ""
    if chrome:
        return EvidenceVerdict(False, chrome)

    text_reason = non_job_text_reason(title)
    if text_reason:
        return EvidenceVerdict(False, text_reason)

    destination_reason = non_job_destination_reason(href, page_url=page_url)
    if destination_reason:
        return EvidenceVerdict(False, destination_reason)

    signals: list[str] = []
    if schema_backed:
        signals.append("JobPosting schema markup")

    ats = ats_job_detail_reason(href)
    if ats:
        signals.append(f"{ats} job-detail URL")

    identifier = job_identifier_in_url(href)
    if identifier:
        signals.append("job identifier in URL")

    if signals:
        return EvidenceVerdict(True, "strong job evidence", tuple(signals))

    list_reason = job_list_container_reason(node) if node is not None else ""
    metadata = job_metadata_signals(text)
    if list_reason and metadata:
        return EvidenceVerdict(True, "job-list structure with published metadata", (list_reason, *metadata))

    # A digit-free posting-shaped slug is only worth anything with corroboration:
    # on its own it cannot be told apart from "/careers/employee-benefits".
    slug = descriptive_job_slug_in_url(href)
    if slug:
        if list_reason:
            return EvidenceVerdict(
                True,
                "posting slug inside a verified job list",
                (f"posting-shaped URL slug (\"{slug}\")", list_reason, *metadata),
            )
        if metadata and careers_context:
            return EvidenceVerdict(
                True,
                "posting slug with published metadata on a careers page",
                (f"posting-shaped URL slug (\"{slug}\")", *metadata),
            )

    # An employer that publishes each opening as a job-description document is
    # still publishing a posting, provided the link sits in a job-list
    # structure on a confirmed careers page rather than loose in the body.
    if document_posting and (list_reason or careers_context):
        signals = ["official job description document"]
        if list_reason:
            signals.append(list_reason)
        signals.extend(metadata)
        return EvidenceVerdict(True, "official job document on a careers page", tuple(signals))

    missing = []
    if not list_reason:
        missing.append("no verified job-list structure")
    if not metadata:
        missing.append("no published job metadata")
    return EvidenceVerdict(
        False,
        "no positive job evidence (" + "; ".join(missing) + ")",
    )


# ---------------------------------------------------------------------------
# Careers-page context
# ---------------------------------------------------------------------------

_CAREERS_TITLE = re.compile(
    r"\b(careers?|jobs?|employment|openings?|opportunit(?:y|ies)|join our team|work (?:with|for) us|hiring)\b",
    flags=re.IGNORECASE,
)


def careers_page_context_reason(page_url: str, soup: Any = None) -> str:
    """Return why ``page_url`` is a confirmed careers page, or an empty string.

    Bare link-list parsing -- where a run of anchors under an "Open Positions"
    heading becomes the posting list -- is only safe on a page that is itself a
    careers page. On a general marketing page the same shape is a menu.
    """

    segments = _path_segments(urlsplit(str(page_url or "")).path)
    for segment in segments:
        if segment in CAREERS_SEGMENTS:
            return f"careers URL segment (\"{segment}\")"

    if soup is None:
        return ""
    for selector in ("h1", "title"):
        try:
            node = soup.select_one(selector)
        except Exception:
            node = None
        if node is None:
            continue
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if _CAREERS_TITLE.search(text):
            return f"careers page {selector} (\"{text[:80]}\")"
    return ""


def page_job_list_structure_reason(soup: Any) -> str:
    """Return why a page contains a rendered job-list structure, or "".

    Used to decide whether an empty generic parse is a trustworthy "this board
    has no openings" or merely a page we failed to understand.
    """
    if soup is None:
        return ""
    try:
        nodes = soup.select("[class], [id], table")
    except Exception:
        return ""
    for node in nodes[:2000]:
        if str(getattr(node, "name", "") or "").lower() == "table":
            header = _table_header_text(node)
            if header and _JOB_TABLE_HEADER_TITLE.search(header) and _JOB_TABLE_HEADER_DETAIL.search(header):
                return "job table header row"
            continue
        tokens = _attribute_tokens(node)
        if _JOB_LIST_SUBJECT.search(tokens) and _JOB_LIST_SHAPE.search(tokens):
            if navigation_chrome_reason(node):
                continue
            return "job-list container attributes"
    return ""
