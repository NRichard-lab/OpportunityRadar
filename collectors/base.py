from __future__ import annotations

import time
from pathlib import Path
from typing import Any
import requests

from backend.outbound_security import SSRFProtectedSession
from job_platforms import detect_job_platform


class BaseCollector:
    requires_browser = False

    def __init__(self, delay_seconds: float = 1.0, debug: bool = False, debug_dir: Path | None = None) -> None:
        self.delay_seconds = delay_seconds
        self.debug = debug
        self.debug_dir = debug_dir
        self.candidate_count = 0
        self.rejected_count = 0
        self.saved_count = 0
        self.debug_lines: list[str] = []
        self.candidate_samples: list[dict[str, str]] = []
        self.rejection_samples: list[dict[str, str]] = []
        self.rejected_candidates: list[dict[str, Any]] = []
        self.selection_reason = ""
        self.final_url_after_redirect = ""
        self.last_pay_extraction: dict[str, Any] = {}
        self.session = SSRFProtectedSession()
        self.session.headers.update({"User-Agent": "FinancialJobsRadar/1.0 public job listing collector"})

    def collect(self, company: dict[str, Any]):
        raise NotImplementedError

    def get(self, url: str) -> requests.Response:
        time.sleep(max(0, self.delay_seconds))
        response = self.session.get(
            url,
            timeout=20,
            allow_redirects=True,
        )
        response.raise_for_status()
        self.final_url_after_redirect = response.url
        return response

    def source_url(self, company: dict[str, Any]) -> tuple[str, str]:
        job_board_url = str(company.get("Job Board URL") or "").strip()
        if job_board_url:
            return job_board_url, "Job Board URL"
        rss_url = str(company.get("Jobs RSS Feed URL") or "").strip()
        feed_found = str(company.get("Feed Found") or "").strip().lower() in {"true", "yes", "1"}
        if rss_url and feed_found:
            return rss_url, "RSS Feed"
        return "", "None"

    def resolve_embedded_job_board_url(self, source_url: str, expected_platform: str) -> str:
        if detect_job_platform(source_url).casefold() == expected_platform.casefold():
            return source_url
        try:
            from website_tools import extract_embedded_urls

            response = self.get(source_url)
            for embedded_url in extract_embedded_urls(response.url, response.text):
                if detect_job_platform(embedded_url).casefold() == expected_platform.casefold():
                    return embedded_url
        except Exception:
            pass
        return source_url

    def record_candidate(self, text: str, url: str = "") -> None:
        self.candidate_count += 1
        if len(self.candidate_samples) < 20:
            self.candidate_samples.append({"text": text, "url": url})
        if self.debug:
            self.debug_lines.append(f"CANDIDATE\t{text}\t{url}")

    def reject_candidate(
        self,
        text: str,
        reason: str,
        url: str = "",
        surrounding_text: str = "",
        detail_attempted: bool = False,
        detail_title: str = "",
        company: dict[str, Any] | None = None,
        job_board_url: str = "",
    ) -> None:
        self.rejected_count += 1
        if len(self.rejection_samples) < 20:
            self.rejection_samples.append({"text": text, "url": url, "reason": reason})
        self.rejected_candidates.append(
            {
                "companyId": str((company or {}).get("Company ID") or ""),
                "companyName": str((company or {}).get("Company Name") or ""),
                "jobBoardUrl": job_board_url or str((company or {}).get("Job Board URL") or ""),
                "finalUrlAfterRedirect": self.final_url_after_redirect,
                "collectorUsed": self.__class__.__name__,
                "candidateText": text,
                "candidateHref": url,
                "rejectionReason": reason,
                "surroundingTextSnippet": surrounding_text[:700],
                "detailPageAttempted": bool(detail_attempted),
                "detailPageTitleFound": detail_title,
            }
        )
        if self.debug:
            self.debug_lines.append(f"REJECTED\t{reason}\t{text}\t{url}")

    def save_candidate(self, text: str, url: str = "") -> None:
        self.saved_count += 1
        if self.debug:
            self.debug_lines.append(f"SAVED\t{text}\t{url}")

    def record_pay_extraction(self, source: str, candidate_text: str, pay_info: dict[str, Any]) -> None:
        if not pay_info.get("payText"):
            return
        self.last_pay_extraction = {
            "source": source,
            "candidateText": candidate_text[:700],
            "pattern": str(pay_info.get("payPatternMatched") or ""),
            "payMin": pay_info.get("payMin"),
            "payMax": pay_info.get("payMax"),
            "payPeriod": str(pay_info.get("payPeriod") or "unknown"),
        }
        if self.debug:
            self.debug_lines.append(
                f"PAY\t{source}\t{pay_info.get('payText')}\t{pay_info.get('payMin')}\t{pay_info.get('payMax')}\t{pay_info.get('payPeriod')}"
            )

    def flush_debug(self, company: dict[str, Any]) -> None:
        if not self.debug or not self.debug_dir:
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        name = "".join(char.lower() if char.isalnum() else "-" for char in str(company.get("Company Name") or "company"))
        while "--" in name:
            name = name.replace("--", "-")
        path = self.debug_dir / f"{name.strip('-') or 'company'}-{self.__class__.__name__}.txt"
        path.write_text("\n".join(self.debug_lines) + "\n", encoding="utf-8")


def pick_collector(
    company: dict[str, Any],
    delay_seconds: float = 1.0,
    debug: bool = False,
    debug_dir: Path | None = None,
) -> BaseCollector:
    from collectors.adp_collector import ADPCollector
    from collectors.generic_collector import GenericCollector
    from collectors.greenhouse_collector import GreenhouseCollector
    from collectors.icims_collector import ICIMSCollector
    from collectors.lever_collector import LeverCollector
    from collectors.paylocity_collector import PaylocityCollector
    from collectors.paycor_collector import PaycorCollector
    from collectors.paycom_collector import PaycomCollector
    from collectors.ukg_collector import UKGCollector
    from collectors.workday_collector import WorkdayCollector

    platform = str(company.get("Job Platform") or detect_job_platform(str(company.get("Job Board URL") or ""))).lower()
    url = str(company.get("Job Board URL") or "").lower()
    direct_workforce_api = "workforcenow.adp.com" in url and "/mdf/recruitment/recruitment.html" in url
    embedded_workforce_board = platform.strip() == "adp workforce now" and "workforcenow.adp.com" not in url
    if direct_workforce_api or embedded_workforce_board:
        return with_reason(ADPCollector(delay_seconds, debug, debug_dir), "Job Board URL matched or embeds the ADP Workforce Now public API.")
    if "workday" in platform or "myworkdayjobs.com" in url:
        return with_reason(WorkdayCollector(delay_seconds, debug, debug_dir), "Job Platform or Job Board URL matched Workday.")
    if "greenhouse" in platform or "greenhouse.io" in url:
        return with_reason(GreenhouseCollector(delay_seconds, debug, debug_dir), "Job Platform or Job Board URL matched Greenhouse.")
    if "lever" in platform or "lever.co" in url:
        return with_reason(LeverCollector(delay_seconds, debug, debug_dir), "Job Platform or Job Board URL matched Lever.")
    if "icims" in platform or "icims.com" in url:
        return with_reason(ICIMSCollector(delay_seconds, debug, debug_dir), "Job Platform or Job Board URL matched ICIMS.")
    if "paylocity" in platform or "paylocity.com" in url:
        return with_reason(PaylocityCollector(delay_seconds, debug, debug_dir), "Job Platform or Job Board URL matched Paylocity.")
    if "paycor" in platform or "recruitingbypaycor.com" in url:
        return with_reason(PaycorCollector(delay_seconds, debug, debug_dir), "Job Platform or Job Board URL matched Paycor.")
    if "paycom" in platform or "paycomonline.net" in url:
        return with_reason(PaycomCollector(delay_seconds, debug, debug_dir), "Job Platform or Job Board URL matched Paycom.")
    if "ukg" in platform or "ultipro.com" in url or "ukg.com" in url:
        return with_reason(UKGCollector(delay_seconds, debug, debug_dir), "Job Platform or Job Board URL matched UKG.")
    return with_reason(GenericCollector(delay_seconds, debug, debug_dir), "No supported platform detected; using generic public page collector.")


def with_reason(collector: BaseCollector, reason: str) -> BaseCollector:
    collector.selection_reason = reason
    return collector
