from __future__ import annotations

from urllib.parse import urlparse


PLATFORM_DOMAIN_MAP = {
    "dayforcehcm.com": "Dayforce",
    "myworkdayjobs.com": "Workday",
    "greenhouse.io": "Greenhouse",
    "lever.co": "Lever",
    "icims.com": "ICIMS",
    "paylocity.com": "Paylocity",
    "ukg.com": "UKG",
    "ultipro.com": "UKG",
    "adp.com": "ADP",
    "bamboohr.com": "BambooHR",
    "smartrecruiters.com": "SmartRecruiters",
    "jobvite.com": "Jobvite",
    "paycomonline.net": "Paycom",
    "applicantpro.com": "ApplicantPro",
    "oraclecloud.com": "Oracle Recruiting",
    "successfactors.com": "SAP SuccessFactors",
    "saashr.com": "SaaS HR",
}


def detect_job_platform(*urls: str | None) -> str:
    """Return a known job platform name when any URL points to a mapped vendor."""
    for raw_url in urls:
        if not raw_url:
            continue
        parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        candidate = f"{host}{path}"
        for domain, platform in PLATFORM_DOMAIN_MAP.items():
            if domain in candidate:
                return platform
    return ""
