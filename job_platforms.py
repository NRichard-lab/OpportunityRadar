from __future__ import annotations

import re
from urllib.parse import urlparse, urlsplit, urlunsplit


PLATFORM_DOMAIN_MAP = {
    "workforcenow.adp.com": "ADP Workforce Now",
    "recruiting.adp.com": "ADP Recruiting",
    "myworkdayjobs.com": "Workday",
    "wd1.myworkdayjobs.com": "Workday",
    "wd5.myworkdayjobs.com": "Workday",
    "greenhouse.io": "Greenhouse",
    "boards.greenhouse.io": "Greenhouse",
    "lever.co": "Lever",
    "jobs.lever.co": "Lever",
    "icims.com": "ICIMS",
    "paylocity.com": "Paylocity",
    "ukg.com": "UKG",
    "ultipro.com": "UKG",
    "adp.com": "ADP Workforce Now",
    "bamboohr.com": "BambooHR",
    "smartrecruiters.com": "SmartRecruiters",
    "jobvite.com": "Jobvite",
    "paycomonline.net": "Paycom",
    "recruitingbypaycor.com": "Paycor",
    "applicantpro.com": "ApplicantPro",
    "saashr.com": "UKG Ready",
    "oraclecloud.com": "Oracle Recruiting",
    "successfactors.com": "SAP SuccessFactors",
    "isolvedhire.com": "isolved Hire",
    "jazzhr.com": "JazzHR",
    "clearcompany.com": "ClearCompany",
    "hrmdirect.com": "ClearCompany",
    "paycor.com": "Paycor",
    "app.joinhandshake.com": "Handshake",
    "jobs.dayforcehcm.com": "Dayforce",
    "csod.com": "Cornerstone",
}


DAYFORCE_HOST = "jobs.dayforcehcm.com"
DAYFORCE_BOARD_PATH = re.compile(
    r"^/(?P<culture>[A-Za-z]{2,3}(?:-[A-Za-z]{2})?)/"
    r"(?P<namespace>[A-Za-z0-9_-]+)/(?P<board>[A-Za-z0-9_-]+)"
    r"(?:/jobs/[0-9]+)?/?$",
    flags=re.IGNORECASE,
)


def hostname_matches_domain(hostname: str, domain: str) -> bool:
    """Return whether ``hostname`` is ``domain`` or one of its subdomains."""
    normalized_host = str(hostname or "").strip().lower().rstrip(".")
    normalized_domain = str(domain or "").strip().lower().rstrip(".")
    if not normalized_host or not normalized_domain:
        return False
    return normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}")


def detect_job_platform(*urls: str | None) -> str:
    """Return a known job platform name when any URL points to a mapped vendor."""
    for raw_url in urls:
        if not raw_url:
            continue
        parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
        host = (parsed.hostname or "").lower().rstrip(".")
        for domain, platform in PLATFORM_DOMAIN_MAP.items():
            if hostname_matches_domain(host, domain):
                return platform
    return ""


def canonical_job_board_url(url: str) -> str:
    """Return a Dayforce tenant board root for a verified root or job-detail URL.

    Other providers and unrecognized Dayforce paths are returned unchanged. This
    keeps discovery from persisting one currently open posting as the board URL.
    """
    value = str(url or "").strip()
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or host != DAYFORCE_HOST:
        return value
    if parsed.username is not None or parsed.password is not None:
        return value
    try:
        port = parsed.port
    except ValueError:
        return value
    if port not in {None, 443}:
        return value
    match = DAYFORCE_BOARD_PATH.fullmatch(parsed.path)
    if not match:
        return value
    board_path = "/{culture}/{namespace}/{board}".format(**match.groupdict())
    return urlunsplit(("https", DAYFORCE_HOST, board_path, "", ""))
