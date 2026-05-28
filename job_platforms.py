from __future__ import annotations

from urllib.parse import urlparse


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
    "oraclecloud.com": "Oracle Recruiting",
    "successfactors.com": "SAP SuccessFactors",
    "isolvedhire.com": "isolved Hire",
    "jazzhr.com": "JazzHR",
    "clearcompany.com": "ClearCompany",
    "paycor.com": "Paycor",
    "indeed.com/cmp/": "Indeed Company Jobs",
    "app.joinhandshake.com": "Handshake",
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
