from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from openpyxl import Workbook

from backend.database import ROOT_DIR, get_connection
from config import REQUEST_TIMEOUT, USER_AGENT
from job_platforms import detect_job_platform


OUTPUT_DIR = ROOT_DIR / "output"
LOG_DIR = ROOT_DIR / "logs"
DEBUG_DIR = LOG_DIR / "job_collection_debug"
DIAGNOSTIC_XLSX = OUTPUT_DIR / "job_collection_diagnostics.xlsx"
DIAGNOSTIC_JSON = LOG_DIR / "job_collection_diagnostics.json"
REJECTED_LABELS = {
    "apply", "login", "locations", "all job types", "all locations", "search jobs",
    "search", "next", "previous", "filter", "filters", "view all jobs",
}
SUPPORTED_PLATFORMS = {"Workday", "ADP", "Greenhouse", "Lever", "ICIMS", "Paylocity", "UKG", "SaaS HR", "Dayforce"}


class CollectionError(ValueError):
    pass


@dataclass
class Candidate:
    title: str
    detail_url: str
    location: str = ""
    department: str = ""
    pay_text: str = ""
    employment_type: str = ""
    posted_date: str = ""
    description: str = ""
    external_job_id: str = ""
    source_url: str = ""
    pay_min: float | None = None
    pay_max: float | None = None
    pay_currency: str = ""
    pay_period: str = ""
    pay_display: str = ""
    target_pay_min: float | None = None
    target_pay_max: float | None = None
    full_pay_min: float | None = None
    full_pay_max: float | None = None
    incentives_text: str = ""
    benefits_summary: str = ""
    benefit_tags: list[str] = field(default_factory=list)
    compensation_source_text: str = ""
    benefits_source_text: str = ""
    has_health_insurance: bool = False
    has_dental_insurance: bool = False
    has_vision_insurance: bool = False
    has_retirement: bool = False
    retirement_details: str = ""
    retirement_match_percent: float | None = None
    retirement_contribution_percent: float | None = None
    has_pto: bool = False
    pto_details: str = ""
    has_tuition_reimbursement: bool = False
    tuition_details: str = ""
    has_volunteer_time_off: bool = False
    has_donation_match: bool = False
    has_remote_hybrid: bool = False
    other_benefit_details: str = ""
    extraction_warning: str = ""

    @property
    def dedupe_key(self) -> str:
        if self.external_job_id:
            basis = f"id:{self.external_job_id.strip().lower()}"
        elif self.detail_url:
            basis = f"url:{self.detail_url.split('#')[0].rstrip('/').lower()}"
        else:
            normalized = lambda value: re.sub(r"[^a-z0-9]+", "", value.lower())
            basis = f"title-location:{normalized(self.title)}:{normalized(self.location)}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/json"})
    return session


def _public_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not any(term in url.lower() for term in ("/login", "/signin", "captcha"))


def _clean(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


PAY_RANGE_RE = re.compile(
    r"(?P<display>(?P<currency>\$|USD\s*)\s*(?P<minimum>\d[\d,]*(?:\.\d+)?)"
    r"\s*(?:-|–|—|to)\s*(?:\$|USD\s*)?\s*(?P<maximum>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*(?P<period>/\s*(?:hour|hr|week|month|year)|per\s+(?:hour|week|month|year)|hourly|weekly|monthly|annually|annual))?)",
    re.I,
)
PAY_SINGLE_RE = re.compile(
    r"(?P<display>(?P<currency>\$|USD\s*)\s*(?P<minimum>\d[\d,]*(?:\.\d+)?)"
    r"\s*(?P<period>/\s*(?:hour|hr|week|month|year)|per\s+(?:hour|week|month|year)|hourly|weekly|monthly|annually|annual))",
    re.I,
)
BENEFIT_PATTERNS = {
    "Health insurance": r"\b(?:medical|health)(?:\s+(?:insurance|coverage|plan|benefits?))?\b",
    "Dental insurance": r"\bdental(?:\s+(?:insurance|coverage|plan|benefits?))?\b",
    "Vision insurance": r"\bvision(?:\s+(?:insurance|coverage|plan|benefits?))?\b",
    "Retirement": r"\b(?:401\s*\(?k\)?|pension|retirement\s+(?:plan|match|benefit)|employer\s+match)\b",
    "Paid time off": r"\b(?:paid time off|PTO|vacation|sick leave|paid holidays?)\b",
    "Tuition assistance": r"\b(?:tuition reimbursement|tuition assistance|education assistance)\b",
    "Life insurance": r"\blife(?:\s+and\s+disability)?\s+insurance\b",
    "Disability coverage": r"\b(?:short[- ]term|long[- ]term)?\s*disability\s+(?:insurance|coverage|benefit)\b",
    "FSA/HSA": r"\b(?:flexible spending account|health savings account|FSA|HSA)\b",
    "Parental leave": r"\b(?:paid )?parental leave\b",
    "Remote/hybrid": r"\b(?:remote|hybrid|work from home|telecommut)\w*\b",
    "Employee assistance program": r"\b(?:employee assistance program|EAP)\b",
}


def _sentences(text: str) -> list[str]:
    return [value.strip() for value in re.split(r"(?<=[.!?])\s+|\s*[•|]\s*", text) if value.strip()]


def _pay_period(raw: str) -> str:
    value = raw.lower().replace(" ", "")
    if "hour" in value or "/hr" in value: return "hourly"
    if "week" in value: return "weekly"
    if "month" in value: return "monthly"
    if "year" in value or "annual" in value: return "annual"
    return "other"


def _section(text: str, starts: tuple[str, ...], stops: tuple[str, ...]) -> str:
    upper = text.upper()
    positions = [upper.find(heading) for heading in starts if upper.find(heading) >= 0]
    if not positions:
        return ""
    start = min(positions)
    stop_positions = [upper.find(heading, start + 1) for heading in stops if upper.find(heading, start + 1) >= 0]
    end = min(stop_positions) if stop_positions else min(len(text), start + 5000)
    return text[start:end].strip()


def _range_values(match: re.Match[str]) -> tuple[float, float, str, str, str]:
    minimum = float(match.group("minimum").replace(",", ""))
    maximum_text = match.groupdict().get("maximum")
    maximum = float(maximum_text.replace(",", "")) if maximum_text else minimum
    currency = "USD" if match.group("currency").strip().upper() in {"$", "USD"} else match.group("currency").strip().upper()
    period = _pay_period(match.groupdict().get("period") or "")
    return minimum, maximum, currency, period, match.group("display").strip()


def _extract_compensation_and_benefits(candidate: Candidate) -> None:
    text = candidate.description
    if not text:
        return
    compensation_source = _section(text, ("PAY RANGE", "COMPENSATION", "TOTAL REWARDS", "WHAT WE OFFER"), ("BENEFITS", "RESPONSIBILITIES", "WHAT YOU'LL DO", "WHAT YOU’LL DO", "IMPACT YOU'LL MAKE", "IMPACT YOU’LL MAKE"))
    compensation_search_text = compensation_source or text
    ranges = list(PAY_RANGE_RE.finditer(compensation_search_text))
    for index, match in enumerate(ranges):
        context = compensation_search_text[max(0, match.start() - 100):match.start()].lower()
        minimum, maximum, currency, period, display = _range_values(match)
        if "full" in context:
            candidate.full_pay_min, candidate.full_pay_max = minimum, maximum
        elif "target" in context or candidate.target_pay_min is None:
            candidate.target_pay_min, candidate.target_pay_max = minimum, maximum
            candidate.pay_currency, candidate.pay_period, candidate.pay_display = currency, period, display
    if not ranges:
        single = PAY_SINGLE_RE.search(compensation_search_text)
        if single:
            minimum, maximum, currency, period, display = _range_values(single)
            candidate.target_pay_min, candidate.target_pay_max = minimum, maximum
            candidate.pay_currency, candidate.pay_period, candidate.pay_display = currency, period, display
    candidate.pay_min, candidate.pay_max = candidate.target_pay_min, candidate.target_pay_max
    candidate.pay_text = candidate.pay_display
    incentive_sentences = [sentence for sentence in _sentences(compensation_search_text) if re.search(r"\b(?:bonuses?|incentives?|commissions?|differentials?|equity)\b", sentence, re.I)]
    candidate.incentives_text = " ".join(incentive_sentences[:5])
    if compensation_source:
        candidate.compensation_source_text = compensation_source
    elif candidate.pay_display or candidate.incentives_text:
        candidate.compensation_source_text = " ".join([candidate.pay_display, candidate.incentives_text]).strip()
    compensation_segments = [sentence for sentence in _sentences(text) if re.search(r"\b(?:salary|pay|compensation|bonuses?|incentives?|commissions?|differentials?|equity)\b|\$", sentence, re.I)]
    if not candidate.pay_display and compensation_segments and any("$" in segment or "USD" in segment.upper() for segment in compensation_segments):
        candidate.compensation_source_text = candidate.compensation_source_text or " ".join(compensation_segments[:5])
        candidate.extraction_warning = "Compensation was disclosed but the numeric range could not be confidently parsed"

    benefits_source = _section(text, ("BENEFITS", "TOTAL REWARDS", "WHAT WE OFFER"), ("IMPACT YOU'LL MAKE", "IMPACT YOU’LL MAKE", "RESPONSIBILITIES", "WHAT YOU'LL DO", "WHAT YOU’LL DO", "QUALIFICATIONS", "REQUIRED QUALIFICATIONS"))
    # Headings can occur in ordinary prose (for example, “outstanding benefits
    # package”) before the employer's real benefit disclosure.  Scan the full
    # public description so such a false heading cannot hide later benefits.
    benefit_search_text = text
    benefit_sentences: list[str] = []
    for tag, pattern in BENEFIT_PATTERNS.items():
        matches = [sentence for sentence in _sentences(benefit_search_text) if re.search(pattern, sentence, re.I)]
        if matches:
            candidate.benefit_tags.append(tag)
            benefit_sentences.extend(matches)
    candidate.benefit_tags = list(dict.fromkeys(candidate.benefit_tags))
    unique_sentences = list(dict.fromkeys(benefit_sentences))
    if unique_sentences:
        candidate.benefits_source_text = " ".join(unique_sentences[:12])
        candidate.benefits_summary = "; ".join(candidate.benefit_tags)
    candidate.has_health_insurance = "Health insurance" in candidate.benefit_tags
    candidate.has_dental_insurance = "Dental insurance" in candidate.benefit_tags
    candidate.has_vision_insurance = "Vision insurance" in candidate.benefit_tags
    candidate.has_retirement = "Retirement" in candidate.benefit_tags
    candidate.has_pto = "Paid time off" in candidate.benefit_tags
    candidate.has_tuition_reimbursement = "Tuition assistance" in candidate.benefit_tags
    candidate.has_remote_hybrid = bool(re.search(BENEFIT_PATTERNS["Remote/hybrid"], text, re.I))
    candidate.has_volunteer_time_off = bool(re.search(r"\bvolunteer time off\b", benefit_search_text, re.I))
    candidate.has_donation_match = bool(re.search(r"\bdonation match\b", benefit_search_text, re.I))
    retirement_sentences = [sentence for sentence in _sentences(benefit_search_text) if re.search(BENEFIT_PATTERNS["Retirement"], sentence, re.I)]
    candidate.retirement_details = " ".join(retirement_sentences[:2])
    match_percent = re.search(r"(?:401\s*\(?k\)?.{0,80}?match|match.{0,80}?401\s*\(?k\)?).{0,40}?(\d+(?:\.\d+)?)%", benefit_search_text, re.I)
    contribution_percent = re.search(r"(?:401\s*\(?k\)?.{0,100}?(\d+(?:\.\d+)?)%\s+(?:annual\s+)?contribution|(\d+(?:\.\d+)?)%\s+(?:annual\s+)?contribution.{0,80}?401\s*\(?k\)?)", benefit_search_text, re.I)
    if match_percent: candidate.retirement_match_percent = float(match_percent.group(1))
    if contribution_percent: candidate.retirement_contribution_percent = float(contribution_percent.group(1) or contribution_percent.group(2))
    pto_match = re.search(r"\b(?:PTO|paid time off).{0,100}?(?=(?:Tuition|401|Medical|Dental|Vision|$))", benefit_search_text, re.I)
    if pto_match: candidate.pto_details = _clean(pto_match.group(0))
    tuition_match = re.search(r"\b(?:Tuition Reimbursement|Tuition Assistance|Education Assistance).{0,80}?(?:Program)?", benefit_search_text, re.I)
    if tuition_match: candidate.tuition_details = _clean(tuition_match.group(0))
    other_parts = []
    if candidate.has_volunteer_time_off: other_parts.append("Volunteer time off")
    if candidate.has_donation_match: other_parts.append("Donation match")
    if re.search(r"\bExchange Program\b", benefit_search_text, re.I): other_parts.append("Exchange Program")
    candidate.other_benefit_details = "; ".join(other_parts)


def _candidate_from_link(text: str, href: str, source_url: str) -> Candidate | None:
    title = _clean(text)
    lower = title.lower()
    if not title or lower in REJECTED_LABELS or re.fullmatch(r"page\s+\d+\s+of\s+\d+", lower):
        return None
    if len(title) < 3 or len(title) > 240 or not _public_url(href):
        return None
    return Candidate(title=title, detail_url=href, source_url=source_url)


def _html_candidates(session: requests.Session, start_url: str, max_pages: int = 10) -> list[Candidate]:
    candidates: list[Candidate] = []
    next_url = start_url
    visited: set[str] = set()
    for _ in range(max_pages):
        if not next_url or next_url in visited:
            break
        visited.add(next_url)
        response = session.get(next_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        page_count = 0
        for anchor in soup.find_all("a", href=True):
            href = urljoin(response.url, str(anchor["href"]))
            text = anchor.get_text(" ", strip=True)
            haystack = f"{text} {href}".lower()
            if not any(term in haystack for term in ("job", "position", "opening", "career")):
                continue
            candidate = _candidate_from_link(text, href, response.url)
            if candidate:
                candidates.append(candidate); page_count += 1
        next_anchor = soup.find("a", rel=lambda value: value and "next" in value) or next(
            (a for a in soup.find_all("a", href=True) if _clean(a.get_text()).lower() in {"next", "next page", "load more", "more jobs", ">", "›"}), None
        )
        next_url = urljoin(response.url, next_anchor["href"]) if next_anchor else ""
        if page_count == 0 and not next_url:
            break
    return candidates


def _enrich_html_candidates(session: requests.Session, candidates: list[Candidate]) -> None:
    for candidate in candidates:
        try:
            response = session.get(candidate.detail_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            main = soup.find("main") or soup.find("article") or soup.body
            text = _clean(main.get_text(" ", strip=True) if main else "")
            candidate.description = text[:100_000]
            location_node = soup.find(attrs={"class": re.compile(r"location", re.I)})
            if location_node: candidate.location = _clean(location_node.get_text(" ", strip=True))[:300]
            category_node = soup.find(attrs={"class": re.compile(r"department|category", re.I)})
            if category_node: candidate.department = _clean(category_node.get_text(" ", strip=True))[:300]
            type_match = re.search(r"\b(full[- ]time|part[- ]time|contract|temporary|internship|seasonal)\b", text, re.I)
            if type_match: candidate.employment_type = type_match.group(1)
            pay_match = re.search(r"\$[\d,.]+(?:\s*(?:-|to)\s*\$[\d,.]+)?(?:\s*(?:per|/)(?:hour|year|annum))?", text, re.I)
            if pay_match: candidate.pay_text = pay_match.group(0)
            time_node = soup.find("time")
            if time_node: candidate.posted_date = _clean(time_node.get("datetime") or time_node.get_text(" ", strip=True))
        except Exception:
            continue


def _workday_candidates(session: requests.Session, board_url: str) -> list[Candidate]:
    parsed = urlparse(board_url)
    tenant = parsed.netloc.split(".")[0]
    site = next((part for part in parsed.path.split("/") if part), "")
    if not tenant or not site:
        raise CollectionError("The Workday URL does not contain a tenant and site name.")
    api_base = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}"
    endpoint = f"{api_base}/jobs"
    candidates: list[Candidate] = []
    offset, limit = 0, 20
    while True:
        response = session.post(endpoint, json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        postings = data.get("jobPostings") or []
        for item in postings:
            external_path = str(item.get("externalPath") or "")
            detail_url = urljoin(f"{parsed.scheme}://{parsed.netloc}/{site}/", external_path.lstrip("/"))
            candidate = Candidate(
                title=_clean(item.get("title")), detail_url=detail_url,
                location=_clean(item.get("locationsText")), posted_date=_clean(item.get("postedOn")),
                external_job_id=external_path.rsplit("/", 1)[-1], source_url=endpoint,
            )
            if external_path:
                try:
                    detail_response = session.get(f"{api_base}/{external_path.lstrip('/')}", timeout=REQUEST_TIMEOUT)
                    detail_response.raise_for_status()
                    info = detail_response.json().get("jobPostingInfo") or {}
                    candidate.title = _clean(info.get("title")) or candidate.title
                    candidate.location = _clean(info.get("location")) or candidate.location
                    candidate.employment_type = _clean(info.get("timeType"))
                    candidate.posted_date = _clean(info.get("postedOn")) or candidate.posted_date
                    candidate.description = BeautifulSoup(str(info.get("jobDescription") or ""), "html.parser").get_text(" ", strip=True)
                    candidate.external_job_id = _clean(info.get("jobReqId")) or candidate.external_job_id
                    pay_match = re.search(r"\$[\d,.]+(?:\s*(?:-|to)\s*\$[\d,.]+)?(?:\s*(?:per|/)(?:hour|year|annum))?", candidate.description, re.I)
                    if pay_match: candidate.pay_text = pay_match.group(0)
                except Exception:
                    pass
            candidates.append(candidate)
        offset += len(postings)
        total = int(data.get("total") or 0)
        if not postings or offset >= total:
            break
    return candidates


def _greenhouse_candidates(session: requests.Session, board_url: str) -> list[Candidate]:
    parts = [part for part in urlparse(board_url).path.split("/") if part]
    token = parts[0] if parts else ""
    if not token:
        raise CollectionError("The Greenhouse board token could not be identified.")
    response = session.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true", timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return [Candidate(title=_clean(job.get("title")), detail_url=str(job.get("absolute_url") or ""), location=_clean((job.get("location") or {}).get("name")), description=BeautifulSoup(str(job.get("content") or ""), "html.parser").get_text(" ", strip=True), external_job_id=str(job.get("id") or ""), source_url=response.url) for job in response.json().get("jobs", [])]


def _lever_candidates(session: requests.Session, board_url: str) -> list[Candidate]:
    parts = [part for part in urlparse(board_url).path.split("/") if part]
    site = parts[0] if parts else ""
    if not site:
        raise CollectionError("The Lever site name could not be identified.")
    response = session.get(f"https://api.lever.co/v0/postings/{site}?mode=json", timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return [Candidate(title=_clean(job.get("text")), detail_url=str(job.get("hostedUrl") or ""), location=_clean((job.get("categories") or {}).get("location")), department=_clean((job.get("categories") or {}).get("department")), employment_type=_clean((job.get("categories") or {}).get("commitment")), description=_clean(job.get("descriptionPlain")), external_job_id=str(job.get("id") or ""), source_url=response.url) for job in response.json()]


def _adp_candidates(session: requests.Session, board_url: str) -> tuple[list[Candidate], bool]:
    """Collect public ADP Workforce Now openings through its public listing API."""
    parsed = urlparse(board_url)
    query = parse_qs(parsed.query)
    cid = _clean((query.get("cid") or [""])[0])
    cc_id = _clean((query.get("ccId") or [""])[0])
    if not cid or not cc_id:
        raise CollectionError("The ADP Job Board URL must include cid and ccId.")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    endpoint = f"{origin}/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
    headers = {
        "Accept-Language": "en_US",
        "locale": "en_US",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": board_url,
        "x-forwarded-host": parsed.netloc,
    }
    candidates: list[Candidate] = []
    skip, page_size, total = 0, 20, None
    while True:
        response = session.get(endpoint, params={
            "cid": cid, "ccId": cc_id, "locale": "en_US", "lang": "en_US",
            "$skip": skip, "$top": page_size, "userQuery": "",
        }, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        postings = data.get("jobRequisitions") or []
        meta = data.get("meta") or {}
        if total is None:
            try:
                total = int(meta.get("totalNumber"))
            except (TypeError, ValueError):
                total = None
        for posting in postings:
            external_id = _clean(posting.get("itemID"))
            if not external_id:
                continue
            detail_response = session.get(
                f"{endpoint}/{external_id}",
                params={"cid": cid, "ccId": cc_id, "locale": "en_US", "lang": "en_US"},
                headers=headers, timeout=REQUEST_TIMEOUT)
            detail_response.raise_for_status()
            detail = detail_response.json()
            location_data = (detail.get("requisitionLocations") or posting.get("requisitionLocations") or [{}])[0]
            address = location_data.get("address") or {}
            state = _clean((address.get("countrySubdivisionLevel1") or {}).get("codeValue"))
            location = _clean(", ".join(part for part in (_clean(address.get("cityName")), state) if part))
            if not location:
                location = _clean((location_data.get("nameCode") or {}).get("shortName"))
            fields = (detail.get("customFieldGroup") or posting.get("customFieldGroup") or {}).get("stringFields") or []
            department = next((_clean(field.get("stringValue")) for field in fields if _clean((field.get("nameCode") or {}).get("codeValue")) == "JobClass"), "")
            public_job_id = next((_clean(field.get("stringValue")) for field in fields if _clean((field.get("nameCode") or {}).get("codeValue")) == "ExternalJobID"), external_id)
            compensation = detail.get("payGradeRange") or posting.get("payGradeRange") or {}
            minimum = (compensation.get("minimumRate") or {})
            maximum = (compensation.get("maximumRate") or {})
            pay_min, pay_max = minimum.get("amountValue"), maximum.get("amountValue")
            currency = _clean(minimum.get("currencyCode") or maximum.get("currencyCode"))
            salary_type = _clean((detail.get("workLevelCode") or posting.get("workLevelCode") or {}).get("shortName"))
            pay_type = next((_clean(field.get("shortName")) for field in (detail.get("customFieldGroup") or posting.get("customFieldGroup") or {}).get("codeFields") or [] if _clean((field.get("nameCode") or {}).get("codeValue")) == "SalaryType"), "")
            period_lookup = {"hourly": "hourly", "weekly": "weekly", "monthly": "monthly", "annually": "annual", "annual": "annual"}
            pay_period = period_lookup.get(pay_type.lower(), "")
            pay_display = ""
            if pay_min is not None:
                pay_display = f"{currency + ' ' if currency else ''}{pay_min:,.2f}" + (f" – {currency + ' ' if currency else ''}{pay_max:,.2f}" if pay_max is not None and pay_max != pay_min else "") + (f" {pay_type.lower()}" if pay_type else "")
            detail_query = parse_qs(parsed.query)
            detail_query["jobId"] = [public_job_id]
            detail_url = urlunparse(parsed._replace(query=urlencode(detail_query, doseq=True)))
            description = BeautifulSoup(str(detail.get("requisitionDescription") or ""), "html.parser").get_text(" ", strip=True)
            candidates.append(Candidate(
                title=_clean(detail.get("requisitionTitle") or posting.get("requisitionTitle")),
                detail_url=detail_url, location=location, department=department,
                employment_type=salary_type, posted_date=_clean(detail.get("postDate") or posting.get("postDate")),
                description=description, external_job_id=public_job_id, source_url=response.url,
                pay_min=pay_min, pay_max=pay_max, pay_currency=currency, pay_period=pay_period,
                pay_display=pay_display, pay_text=pay_display,
            ))
        skip += len(postings)
        if not postings or (total is not None and skip >= total):
            return candidates, True
        if len(postings) < page_size:
            return candidates, True


def _dayforce_detail_candidate(session: requests.Session, detail_url: str) -> Candidate:
    """Read the complete public posting embedded in a Dayforce detail page."""
    response = session.get(detail_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    next_data = soup.find("script", id="__NEXT_DATA__")
    if not next_data or not next_data.string:
        raise CollectionError(f"Dayforce detail data was not available for {detail_url}")
    data = json.loads(next_data.string)
    queries = (((data.get("props") or {}).get("pageProps") or {}).get("dehydratedState") or {}).get("queries") or []
    posting = next((
        (query.get("state") or {}).get("data") for query in queries
        if isinstance((query.get("state") or {}).get("data"), dict)
        and (query.get("state") or {}).get("data", {}).get("jobPostingId")
    ), None)
    if not posting:
        raise CollectionError(f"Dayforce posting data was not found for {detail_url}")
    content = posting.get("jobPostingContent") or {}
    description = BeautifulSoup(
        " ".join(str(content.get(field) or "") for field in ("jobDescriptionHeader", "jobDescription", "jobDescriptionFooter")),
        "html.parser",
    ).get_text(" ", strip=True)
    locations = posting.get("postingLocations") or []
    location = "; ".join(_clean(item.get("formattedAddress")) for item in locations if _clean(item.get("formattedAddress")))
    attributes = {_clean(item.get("name")): item.get("value") for item in posting.get("jobPostingAttributes") or []}
    pay_min = attributes.get("HiringMinRate")
    pay_max = attributes.get("HiringMaxRate")
    pay_period = _pay_period(_clean(attributes.get("PayType"))) if attributes.get("PayType") else ""
    currency = _clean(posting.get("isoCurrencyRegion"))
    pay_display = ""
    if isinstance(pay_min, (int, float)):
        pay_display = f"{currency + ' ' if currency else ''}{pay_min:,.2f}"
        if isinstance(pay_max, (int, float)) and pay_max != pay_min:
            pay_display += f" – {currency + ' ' if currency else ''}{pay_max:,.2f}"
        if pay_period:
            pay_display += f" {pay_period}"
    department_match = re.search(
        r"\bDepartment\s*:\s*:?(.*?)(?=\b(?:Job Code|Status|Reports? to|Pay Range|Who We Are)\s*:)",
        description,
        re.I,
    )
    employment_match = re.search(r"\b(Full[- ]Time|Part[- ]Time|Temporary|Seasonal|Contract)\b", f"{posting.get('jobTitle', '')} {description}", re.I)
    return Candidate(
        title=_clean(posting.get("jobTitle")), detail_url=response.url,
        location=location, department=_clean(department_match.group(1)) if department_match else "",
        employment_type=_clean(employment_match.group(1)) if employment_match else "",
        posted_date=_clean(posting.get("postingStartTimestampUTC")), description=description,
        external_job_id=_clean(posting.get("jobReqId") or posting.get("jobPostingId")),
        source_url=detail_url, pay_min=pay_min if isinstance(pay_min, (int, float)) else None,
        pay_max=pay_max if isinstance(pay_max, (int, float)) else None,
        pay_currency=currency, pay_period=pay_period, pay_display=pay_display, pay_text=pay_display,
        target_pay_min=pay_min if isinstance(pay_min, (int, float)) else None,
        target_pay_max=pay_max if isinstance(pay_max, (int, float)) else None,
    )


def _dayforce_candidates(session: requests.Session, board_url: str) -> tuple[list[Candidate], bool]:
    """Render public Dayforce cards, paginate, then read public detail pages."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CollectionError("Dayforce collection requires the Playwright browser package.") from exc

    response = session.get(board_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    canonical_url = response.url
    job_urls: list[str] = []
    edge_path = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    with sync_playwright() as playwright:
        launch_options: dict[str, object] = {"headless": True}
        if edge_path.exists():
            launch_options["executable_path"] = str(edge_path)
        browser = playwright.chromium.launch(**launch_options)
        try:
            page = browser.new_page()
            page.goto(canonical_url, wait_until="domcontentloaded", timeout=60_000)
            page.locator("a[href*='/jobs/']").first.wait_for(state="visible", timeout=30_000)
            pagination = page.locator("[aria-label^='Page '][aria-label*=' of ']")
            pagination_label = (pagination.first.get_attribute("aria-label") or "") if pagination.count() else ""
            page_match = re.search(r"Page\s+\d+\s+of\s+(\d+)", pagination_label, re.I)
            total_pages = int(page_match.group(1)) if page_match else 1
            for page_number in range(1, total_pages + 1):
                if page_number > 1:
                    parsed = urlparse(canonical_url)
                    query = parse_qs(parsed.query)
                    query["page"] = [str(page_number)]
                    page.goto(urlunparse(parsed._replace(query=urlencode(query, doseq=True))), wait_until="domcontentloaded", timeout=60_000)
                    page.locator("a[href*='/jobs/']").first.wait_for(state="visible", timeout=30_000)
                hrefs = page.locator("a[href*='/jobs/']").evaluate_all(
                    "elements => elements.map(element => element.href).filter(Boolean)"
                )
                page_urls = list(dict.fromkeys(str(href).split("#")[0] for href in hrefs))
                if not page_urls:
                    raise CollectionError(f"Dayforce page {page_number} did not expose public job cards.")
                job_urls.extend(page_urls)
        finally:
            browser.close()
    unique_urls = list(dict.fromkeys(job_urls))
    with ThreadPoolExecutor(max_workers=8) as executor:
        candidates = list(executor.map(lambda url: _dayforce_detail_candidate(_session(), url), unique_urls))
    return candidates, True


def _saashr_candidates(session: requests.Session, board_url: str) -> tuple[list[Candidate], bool]:
    candidates: list[Candidate] = []
    complete = False
    for page in range(1, 26):
        parsed = urlparse(board_url)
        query = parse_qs(parsed.query)
        query["page"] = [str(page)]
        page_url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
        page_candidates = _html_candidates(session, page_url, max_pages=1)
        if not page_candidates:
            complete = True
            break
        candidates.extend(page_candidates)
    return candidates, complete


def _self_hosted_candidates(session: requests.Session, board_url: str) -> tuple[list[Candidate], bool]:
    """Read the saved public listing page and its public detail pages only."""
    response = session.get(board_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    candidates: list[Candidate] = []
    soup = BeautifulSoup(response.text, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (TypeError, json.JSONDecodeError):
            continue
        values = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and isinstance(data.get("@graph"), list): values.extend(data["@graph"])
        for item in values:
            if not isinstance(item, dict) or "jobposting" not in str(item.get("@type", "")).lower(): continue
            location = item.get("jobLocation") or {}
            if isinstance(location, list): location = location[0] if location else {}
            address = location.get("address") if isinstance(location, dict) else {}
            if not isinstance(address, dict): address = {}
            location_text = ", ".join(value for value in (str(address.get("addressLocality") or ""), str(address.get("addressRegion") or "")) if value)
            candidates.append(Candidate(title=_clean(item.get("title")), detail_url=str(item.get("url") or board_url), location=location_text, employment_type=_clean(item.get("employmentType")), posted_date=_clean(item.get("datePosted")), description=BeautifulSoup(str(item.get("description") or ""), "html.parser").get_text(" ", strip=True), external_job_id=_clean(item.get("identifier")), source_url=response.url))
    html_candidates: list[Candidate] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(response.url, str(anchor["href"]))
        title = _clean(anchor.get_text(" ", strip=True))
        path = urlparse(href).path.lower()
        is_detail_link = bool(re.search(r"/(?:jobs?|positions?|openings?)(?:/|$)|[?&](?:job|jobid)=", f"{path}?{urlparse(href).query}", re.I))
        if not is_detail_link or title.lower().startswith("skip ") or title.lower() in REJECTED_LABELS:
            continue
        candidate = _candidate_from_link(title, href, response.url)
        if candidate:
            html_candidates.append(candidate)
    _enrich_html_candidates(session, html_candidates)
    candidates.extend(html_candidates)
    # A static, paginated listing is complete after the public next/load-more
    # links end. JavaScript-only Load More requires review before stale cleanup.
    complete = "load more" not in soup.get_text(" ", strip=True).lower()
    return candidates, complete


def collect_candidates(board_url: str, job_board_type: str = "") -> tuple[str, bool, list[Candidate], list[str], bool]:
    if not _public_url(board_url):
        raise CollectionError("A public Verified Job Board URL is required.")
    platform = job_board_type if job_board_type and job_board_type != "Needs Review" else (detect_job_platform(board_url) or "Generic")
    session = _session()
    notes: list[str] = []
    browser_used = False
    complete = platform in {"Workday", "Greenhouse", "Lever"}
    if platform == "Workday": candidates = _workday_candidates(session, board_url)
    elif platform == "ADP": candidates, complete = _adp_candidates(session, board_url)
    elif platform == "Greenhouse": candidates = _greenhouse_candidates(session, board_url)
    elif platform == "Lever": candidates = _lever_candidates(session, board_url)
    elif platform == "SaaS HR": candidates, complete = _saashr_candidates(session, board_url)
    elif platform == "Dayforce":
        browser_used = True
        candidates, complete = _dayforce_candidates(session, board_url)
    elif platform == "Self-Hosted / In-House": candidates, complete = _self_hosted_candidates(session, board_url)
    else:
        candidates = _html_candidates(session, board_url, max_pages=25 if platform in SUPPORTED_PLATFORMS else 10)
        _enrich_html_candidates(session, candidates)
        notes.append(f"{platform} public HTML collector used; browser automation was not required.")
    unique: dict[str, Candidate] = {}
    for candidate in candidates:
        _extract_compensation_and_benefits(candidate)
        unique.setdefault(candidate.dedupe_key, candidate)
    return platform, browser_used, list(unique.values()), notes, complete


def validate_candidate(candidate: Candidate) -> str:
    title = _clean(candidate.title)
    if not title:
        return "Missing job title"
    if title.lower() in REJECTED_LABELS or re.fullmatch(r"page\s+\d+\s+of\s+\d+", title.lower()):
        return "Navigation, filter, or page-control label"
    if len(title) < 3:
        return "Job title is too short"
    if not _public_url(candidate.detail_url):
        return "Missing or non-public job detail URL"
    if not candidate.location and not candidate.description:
        return "Insufficient public job detail to validate listing"
    if candidate.extraction_warning:
        return candidate.extraction_warning
    return ""


def _save_job(conn, company_id: int, platform: str, candidate: Candidate, validation_source: str = "deterministic") -> str:
    existing = conn.execute("SELECT id FROM jobs WHERE company_id = ? AND (dedupe_key = ? OR (? <> '' AND detail_url = ?))", (company_id, candidate.dedupe_key, candidate.detail_url, candidate.detail_url)).fetchone()
    values = (
        candidate.title, candidate.location, candidate.department, candidate.employment_type,
        candidate.pay_text, candidate.pay_min, candidate.pay_max, candidate.pay_currency,
        candidate.pay_period, candidate.pay_display, candidate.target_pay_min,
        candidate.target_pay_max, candidate.full_pay_min, candidate.full_pay_max,
        candidate.incentives_text, candidate.benefits_summary,
        json.dumps(candidate.benefit_tags, ensure_ascii=False), candidate.compensation_source_text,
        candidate.benefits_source_text, int(candidate.has_health_insurance),
        int(candidate.has_dental_insurance), int(candidate.has_vision_insurance),
        int(candidate.has_retirement), candidate.retirement_details,
        candidate.retirement_match_percent, candidate.retirement_contribution_percent,
        int(candidate.has_pto), candidate.pto_details,
        int(candidate.has_tuition_reimbursement), candidate.tuition_details,
        int(candidate.has_volunteer_time_off), int(candidate.has_donation_match),
        int(candidate.has_remote_hybrid), candidate.other_benefit_details,
        candidate.detail_url, candidate.description,
        candidate.posted_date, platform, candidate.external_job_id, candidate.dedupe_key,
        validation_source, datetime.now(timezone.utc).isoformat(),
    )
    if existing:
        conn.execute("""UPDATE jobs SET title=?, location=?, department=?, employment_type=?, pay_text=?, pay_min=?, pay_max=?, pay_currency=?, pay_period=?, pay_display=?, target_pay_min=?, target_pay_max=?, full_pay_min=?, full_pay_max=?, incentives_text=?, benefits_summary=?, benefit_tags=?, compensation_source_text=?, benefits_source_text=?, has_health_insurance=?, has_dental_insurance=?, has_vision_insurance=?, has_retirement=?, retirement_details=?, retirement_match_percent=?, retirement_contribution_percent=?, has_pto=?, pto_details=?, has_tuition_reimbursement=?, tuition_details=?, has_volunteer_time_off=?, has_donation_match=?, has_remote_hybrid=?, other_benefit_details=?, detail_url=?, description=?, posted_date=?, source_platform=?, external_job_id=?, dedupe_key=?, validation_source=?, status='Open', updated_at=? WHERE id=?""", (*values, existing["id"]))
        return "updated"
    placeholders = ",".join("?" for _ in range(len(values) + 1))
    conn.execute(f"""INSERT INTO jobs(title,location,department,employment_type,pay_text,pay_min,pay_max,pay_currency,pay_period,pay_display,target_pay_min,target_pay_max,full_pay_min,full_pay_max,incentives_text,benefits_summary,benefit_tags,compensation_source_text,benefits_source_text,has_health_insurance,has_dental_insurance,has_vision_insurance,has_retirement,retirement_details,retirement_match_percent,retirement_contribution_percent,has_pto,pto_details,has_tuition_reimbursement,tuition_details,has_volunteer_time_off,has_donation_match,has_remote_hybrid,other_benefit_details,detail_url,description,posted_date,source_platform,external_job_id,dedupe_key,validation_source,company_id,updated_at) VALUES({placeholders})""", (*values[:-1], company_id, values[-1]))
    return "new"


def _write_diagnostics(report: dict[str, object], candidates: list[Candidate], rejected: list[dict[str, object]]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True); LOG_DIR.mkdir(parents=True, exist_ok=True); DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, object]] = []
    if DIAGNOSTIC_JSON.exists():
        try: reports = json.loads(DIAGNOSTIC_JSON.read_text(encoding="utf-8"))
        except Exception: reports = []
    reports.append(report)
    DIAGNOSTIC_JSON.write_text(json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    (DEBUG_DIR / f"{stamp}_candidates.json").write_text(json.dumps([asdict(c) for c in candidates], indent=2, ensure_ascii=False), encoding="utf-8")
    (DEBUG_DIR / f"{stamp}_rejections.json").write_text(json.dumps(rejected, indent=2, ensure_ascii=False), encoding="utf-8")
    workbook = Workbook(); sheet = workbook.active; sheet.title = "Collection Diagnostics"
    headers = ["Company", "Job Board URL", "Collector", "Browser Used", "Candidates", "Rejected", "Saved", "Duration Seconds", "Status", "Error", "Notes", "Candidate Samples", "Source URLs", "Rejection Reasons"]
    sheet.append(headers)
    for item in reports:
        sheet.append([item.get(key, "") for key in ("company", "job_board_url", "collector", "browser_used", "candidate_count", "rejected_count", "saved_count", "duration_seconds", "status", "error", "notes", "candidate_samples", "source_urls", "rejection_reasons")])
    sheet.freeze_panes = "A2"; sheet.auto_filter.ref = sheet.dimensions
    workbook.save(DIAGNOSTIC_XLSX)


def run_collection(company_id: int, debug: bool = False) -> dict[str, object]:
    started = time.monotonic(); started_at = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        if not company: raise CollectionError("Company not found.")
        if not company["verified_job_board_url"]:
            raise CollectionError("Company must have a Verified Job Board URL.")
        company_data = dict(company)
    platform = company_data.get("job_board_type") or (detect_job_platform(company_data["verified_job_board_url"]) or "Generic")
    candidates: list[Candidate] = []; rejected: list[dict[str, object]] = []; saved = 0; updated = 0; removed = 0; tracked_removed = 0; browser_used = False; notes: list[str] = []; error = ""; status = "Completed"; complete = False
    try:
        platform, browser_used, candidates, notes, complete = collect_candidates(company_data["verified_job_board_url"], company_data.get("job_board_type") or "")
        with get_connection() as conn:
            for candidate in candidates:
                reason = validate_candidate(candidate)
                raw = conn.execute("""INSERT INTO raw_job_candidates(company_id,source_url,payload_json,review_status,rejection_reason,external_job_id,detail_url,title,location,dedupe_key) VALUES(?,?,?,?,?,?,?,?,?,?)""", (company_id, candidate.source_url or company_data["verified_job_board_url"], json.dumps(asdict(candidate), ensure_ascii=False), "Needs Review" if reason else "Validated", reason, candidate.external_job_id, candidate.detail_url, candidate.title, candidate.location, candidate.dedupe_key))
                if reason: rejected.append({"candidate_id": raw.lastrowid, "title": candidate.title, "detail_url": candidate.detail_url, "reason": reason})
                else:
                    outcome = _save_job(conn, company_id, platform, candidate)
                    if outcome == "new": saved += 1
                    else: updated += 1
            if complete:
                public_keys = {candidate.dedupe_key for candidate in candidates}
                public_urls = {candidate.detail_url.split("#")[0].rstrip("/").lower() for candidate in candidates if candidate.detail_url}
                public_ids = {candidate.external_job_id.lower() for candidate in candidates if candidate.external_job_id}
                existing_jobs = conn.execute("SELECT id,detail_url,external_job_id,dedupe_key FROM jobs WHERE company_id=? AND status='Open'", (company_id,)).fetchall()
                for job in existing_jobs:
                    present = (
                        (job["dedupe_key"] and job["dedupe_key"] in public_keys)
                        or (job["detail_url"] and job["detail_url"].split("#")[0].rstrip("/").lower() in public_urls)
                        or (job["external_job_id"] and job["external_job_id"].lower() in public_ids)
                    )
                    if present:
                        continue
                    if conn.execute("SELECT 1 FROM applications WHERE job_id=?", (job["id"],)).fetchone():
                        conn.execute("UPDATE jobs SET status='No Longer Posted',updated_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), job["id"]))
                        tracked_removed += 1
                    else:
                        conn.execute("DELETE FROM jobs WHERE id=?", (job["id"],))
                        removed += 1
            else:
                notes.append("Collection completeness could not be proven; stale-job removal was skipped.")
    except Exception as exc:
        status = "Failed"; error = str(exc)
    duration = round(time.monotonic() - started, 3); finished_at = datetime.now(timezone.utc).isoformat()
    report = {"company": company_data["name"], "job_board_url": company_data["verified_job_board_url"], "collector": platform, "browser_used": browser_used, "collection_complete": complete, "candidate_count": len(candidates), "rejected_count": len(rejected), "saved_count": saved, "updated_count": updated, "removed_count": removed, "tracked_no_longer_posted_count": tracked_removed, "duration_seconds": duration, "status": status, "error": error, "notes": "; ".join(notes), "candidate_samples": " | ".join(c.title[:160] for c in candidates[:20]), "source_urls": " | ".join(dict.fromkeys((c.detail_url or c.source_url) for c in candidates[:20])), "rejection_reasons": " | ".join(dict.fromkeys(str(r["reason"]) for r in rejected[:20]))}
    with get_connection() as conn:
        conn.execute("""INSERT INTO job_collection_runs(company_id,collector,job_board_url,browser_used,candidate_count,rejected_count,saved_count,duration_seconds,status,error,notes,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (company_id, platform, company_data["verified_job_board_url"], int(browser_used), len(candidates), len(rejected), saved, duration, status, error, report["notes"], started_at, finished_at))
        conn.execute("""UPDATE companies SET last_collector=?,last_collection_at=?,last_raw_count=?,last_saved_count=?,last_review_count=?,last_collection_error=?,last_collection_status=? WHERE id=?""", (platform, finished_at, len(candidates), saved, len(rejected), error, status, company_id))
    if debug: _write_diagnostics(report, candidates, rejected)
    if error: raise CollectionError(error)
    return report


def _fetch_full_description(session: requests.Session, detail_url: str, platform: str) -> str:
    parsed = urlparse(detail_url)
    if platform == "Workday":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2:
            tenant, site = parsed.netloc.split(".")[0], parts[0]
            external_path = "/".join(parts[1:])
            api_url = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}/{external_path}"
            response = session.get(api_url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            html = str((response.json().get("jobPostingInfo") or {}).get("jobDescription") or "")
            if html:
                return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    response = session.get(detail_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    main = soup.find("main") or soup.find("article") or soup.body
    return _clean(main.get_text(" ", strip=True) if main else "")


REPROCESS_RESPONSE_FIELDS = (
    "id", "title", "target_pay_min", "target_pay_max", "full_pay_min", "full_pay_max",
    "pay_currency", "pay_period", "incentives_text", "has_health_insurance",
    "has_dental_insurance", "has_vision_insurance", "has_retirement",
    "retirement_details", "retirement_match_percent", "retirement_contribution_percent",
    "has_pto", "pto_details", "has_tuition_reimbursement", "tuition_details",
    "has_volunteer_time_off", "has_donation_match", "has_remote_hybrid",
    "other_benefit_details", "compensation_source_text", "benefits_source_text",
)


def _verify_saved_extraction(conn, job_id: int, candidate: Candidate) -> dict[str, object]:
    row = conn.execute(
        f"SELECT {','.join(REPROCESS_RESPONSE_FIELDS)} FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if not row:
        raise CollectionError("Reprocessed job was not found after saving.")
    saved = dict(row)
    comparisons = {
        "target_pay_min": candidate.target_pay_min,
        "target_pay_max": candidate.target_pay_max,
        "full_pay_min": candidate.full_pay_min,
        "full_pay_max": candidate.full_pay_max,
        "incentives_text": candidate.incentives_text,
        "retirement_match_percent": candidate.retirement_match_percent,
        "retirement_contribution_percent": candidate.retirement_contribution_percent,
        "compensation_source_text": candidate.compensation_source_text,
        "benefits_source_text": candidate.benefits_source_text,
    }
    for field_name, expected in comparisons.items():
        if expected not in (None, "") and saved[field_name] != expected:
            raise CollectionError(f"Saved job verification failed for {field_name}.")
    for field_name in (
        "has_health_insurance", "has_dental_insurance", "has_vision_insurance",
        "has_retirement", "has_pto", "has_tuition_reimbursement",
        "has_volunteer_time_off", "has_donation_match", "has_remote_hybrid",
    ):
        if getattr(candidate, field_name) and not saved[field_name]:
            raise CollectionError(f"Saved job verification failed for {field_name}.")
    return saved


def reprocess_saved_jobs(job_id: int | None = None) -> dict[str, object]:
    with get_connection() as conn:
        if job_id is None:
            rows = conn.execute("SELECT * FROM jobs WHERE detail_url<>'' ORDER BY id").fetchall()
        else:
            rows = conn.execute("SELECT * FROM jobs WHERE id=? AND detail_url<>''", (job_id,)).fetchall()
    session = _session(); updated = 0; review = 0; failed = 0; updated_jobs: list[dict[str, object]] = []
    for row in rows:
        data = dict(row)
        try:
            description = _fetch_full_description(session, data["detail_url"], data["source_platform"])
            if not description:
                description = data["description"]
            candidate = Candidate(
                title=data["title"], detail_url=data["detail_url"], location=data["location"],
                department=data["department"], employment_type=data["employment_type"],
                posted_date=data["posted_date"], description=description,
                external_job_id=data.get("external_job_id", ""), source_url=data["detail_url"],
            )
            _extract_compensation_and_benefits(candidate)
            with get_connection() as conn:
                if candidate.extraction_warning:
                    conn.execute("""INSERT INTO raw_job_candidates(company_id,source_url,payload_json,review_status,rejection_reason,external_job_id,detail_url,title,location,dedupe_key) VALUES(?,?,?,?,?,?,?,?,?,?)""", (data["company_id"], data["detail_url"], json.dumps(asdict(candidate), ensure_ascii=False), "Needs Review", candidate.extraction_warning, candidate.external_job_id, candidate.detail_url, candidate.title, candidate.location, candidate.dedupe_key))
                    review += 1
                else:
                    _save_job(conn, data["company_id"], data["source_platform"] or detect_job_platform(data["detail_url"]) or "Generic", candidate, data.get("validation_source") or "deterministic")
                    saved_job = _verify_saved_extraction(conn, data["id"], candidate)
                    updated_jobs.append(saved_job)
                    updated += 1
        except Exception:
            failed += 1
    return {"examined": len(rows), "updated": updated, "needs_review": review, "failed": failed, "updated_jobs": updated_jobs}


def approve_candidate(candidate_id: int) -> dict[str, object]:
    with get_connection() as conn:
        row = conn.execute("SELECT raw_job_candidates.*, companies.name AS company_name FROM raw_job_candidates JOIN companies ON companies.id=raw_job_candidates.company_id WHERE raw_job_candidates.id=?", (candidate_id,)).fetchone()
        if not row: raise CollectionError("Candidate not found.")
        payload = json.loads(row["payload_json"])
        candidate = Candidate(title=str(payload.get("title") or row["title"]), detail_url=str(payload.get("detail_url") or row["detail_url"]))
        for field_name in Candidate.__dataclass_fields__:
            if field_name in payload:
                setattr(candidate, field_name, payload[field_name])
        platform = detect_job_platform(row["source_url"], candidate.detail_url) or "Generic"
        _save_job(conn, row["company_id"], platform, candidate, "manual_review")
        conn.execute("UPDATE raw_job_candidates SET review_status='Approved', rejection_reason='' WHERE id=?", (candidate_id,))
        return {"candidate_id": candidate_id, "status": "Approved", "validation_source": "manual_review"}
