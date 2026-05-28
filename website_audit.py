from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from config import LOG_DIR, OUTPUT_DIR
from excel_tools import read_company_rows, write_master
from search_tools import WebsiteEvaluation, choose_official_website_details, evaluate_official_website_details, search_web, site_root
from website_tools import make_session


logger = logging.getLogger(__name__)

AUDIT_COLUMNS = [
    "Company Name",
    "State",
    "Current Official Website",
    "Audit Status",
    "Confidence",
    "Issue",
    "Suggested Candidate URLs",
    "Notes",
]


@dataclass
class WebsiteAuditRow:
    company_name: str
    state: str
    current_official_website: str
    audit_status: str
    confidence: str
    issue: str
    suggested_candidate_urls: str
    notes: str


def audit_websites(
    master_path: Path,
    company_filter: str = "",
    limit: int | None = None,
    dry_run: bool = False,
) -> list[WebsiteAuditRow]:
    logger.info("Mode: audit-websites")
    all_rows = read_company_rows(master_path)
    if not dry_run:
        write_master(master_path, all_rows)
    rows = select_rows(all_rows, company_filter, limit)
    session = make_session()
    audit_rows: list[WebsiteAuditRow] = []

    for row in rows:
        audit_rows.append(audit_one(row, session))

    write_audit_outputs(audit_rows)
    logger.info("Website audit complete: %s row(s). Dry run: %s", len(audit_rows), dry_run)
    return audit_rows


def repair_websites(
    master_path: Path,
    company_filter: str = "",
    limit: int | None = None,
    dry_run: bool = False,
    allow_low_confidence: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    logger.info("Mode: repair-websites")
    rows = read_company_rows(master_path)
    selected = select_rows(rows, company_filter, limit)
    session = make_session()
    changed = 0
    reviewed = 0
    audit_rows: list[WebsiteAuditRow] = []

    for row in selected:
        audit = audit_one(row, session)
        audit_rows.append(audit)
        if audit.audit_status == "Verified" and row.get("Website Verified"):
            continue
        if row.get("Website Discovery Method") == "Manual" and row.get("Website Verified") and not force:
            logger.info("Skipping manually verified website for %s", row.get("Company Name"))
            continue
        if audit.audit_status == "Verified" and not force:
            continue

        reviewed += 1
        company_name = str(row.get("Company Name") or "")
        logger.info("Repairing website for %s", company_name)
        result = choose_official_website_details(
            company_name,
            str(row.get("Known Website") or ""),
            session,
            city=str(row.get("City") or ""),
            state=str(row.get("State") or ""),
            allow_low_confidence=allow_low_confidence,
        )
        log_repair_result(company_name, result)
        if result.verified and result.confidence in {"High", "Medium"}:
            if not dry_run:
                row["Official Website"] = result.final_url
                row["Website Discovery Method"] = result.discovery_method
                row["Website Candidate URLs"] = "\n".join(result.candidate_urls)
                row["Website Verification Notes"] = "; ".join(result.notes)
                row["Website Verified"] = True
                row["Confidence"] = result.confidence
                row["Search Status"] = "Completed"
                row["Last Checked"] = datetime.now().astimezone().replace(microsecond=0).isoformat()
            changed += 1
            logger.info("Selected verified website for %s: %s (%s)", company_name, result.final_url, result.confidence)
        else:
            notes = "; ".join(result.notes) or "Could not verify official website."
            if not dry_run:
                row["Website Discovery Method"] = result.discovery_method
                row["Website Candidate URLs"] = "\n".join(result.candidate_urls)
                row["Website Verification Notes"] = notes
                row["Website Verified"] = False
                row["Search Status"] = "Needs Review"
                if result.final_url:
                    row["Notes"] = combine_notes(str(row.get("Notes") or ""), f"Possible website, needs review: {result.final_url}")
                else:
                    row["Notes"] = combine_notes(str(row.get("Notes") or ""), "Could not verify official website.")
            logger.info("No verified website selected for %s: %s", company_name, notes)

    if not dry_run:
        write_master(master_path, rows)
    write_audit_outputs(audit_rows)
    summary = {"reviewed": reviewed, "changed": changed, "dryRun": dry_run}
    logger.info("Website repair summary: %s", summary)
    return summary


def audit_one(row: dict[str, Any], session) -> WebsiteAuditRow:
    company_name = str(row.get("Company Name") or "")
    official_url = str(row.get("Official Website") or "").strip()
    logger.info("Auditing website for %s", company_name)
    if not official_url:
        candidates = [site_root(candidate.url) for candidate in search_web(company_name, str(row.get("City") or ""), str(row.get("State") or ""), max_results=5)]
        return WebsiteAuditRow(
            company_name=company_name,
            state=str(row.get("State") or ""),
            current_official_website="",
            audit_status="Missing",
            confidence="Low",
            issue="Official Website is blank.",
            suggested_candidate_urls="\n".join(candidates),
            notes="Could not verify official website because it is missing.",
        )

    evaluation = evaluate_official_website_details(
        company_name,
        official_url,
        session,
        city=str(row.get("City") or ""),
        state=str(row.get("State") or ""),
        require_location=False,
    )
    status = status_from_evaluation(evaluation)
    issue = issue_from_evaluation(evaluation, row)
    suggested = ""
    if status != "Verified":
        suggested = "\n".join(site_root(candidate.url) for candidate in search_web(company_name, str(row.get("City") or ""), str(row.get("State") or ""), max_results=5))
    return WebsiteAuditRow(
        company_name=company_name,
        state=str(row.get("State") or ""),
        current_official_website=official_url,
        audit_status=status,
        confidence=evaluation.confidence,
        issue=issue,
        suggested_candidate_urls=suggested,
        notes="; ".join(evaluation.notes),
    )


def status_from_evaluation(evaluation: WebsiteEvaluation) -> str:
    if evaluation.rejected and "could not verify" in evaluation.rejection_reason:
        return "Failed to Check"
    if evaluation.verified and evaluation.confidence in {"High", "Medium"}:
        return "Verified"
    if evaluation.rejected:
        return "Likely Incorrect"
    return "Needs Review"


def issue_from_evaluation(evaluation: WebsiteEvaluation, row: dict[str, Any]) -> str:
    if evaluation.rejection_reason:
        return evaluation.rejection_reason
    notes = "; ".join(evaluation.notes)
    if "company name terms not found" in notes:
        return "Website does not contain company name terms."
    if "banking/credit union terms not found" in notes:
        return "Website does not contain bank or credit union terms."
    if str(row.get("Careers Page URL") or "") and not evaluation.verified:
        return "Careers Page URL exists but Official Website could not be verified."
    if evaluation.verified:
        return ""
    return "Website could not be confidently verified."


def write_audit_outputs(audit_rows: list[WebsiteAuditRow]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    json_path = LOG_DIR / "website_audit.json"
    xlsx_path = OUTPUT_DIR / "website_audit.xlsx"
    json_path.write_text(json.dumps([asdict(row) for row in audit_rows], indent=2), encoding="utf-8")

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Website Audit"
    sheet.append(AUDIT_COLUMNS)
    for row in audit_rows:
        sheet.append([
            row.company_name,
            row.state,
            row.current_official_website,
            row.audit_status,
            row.confidence,
            row.issue,
            row.suggested_candidate_urls,
            row.notes,
        ])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    workbook.save(xlsx_path)
    logger.info("Wrote website audit to %s and %s", json_path, xlsx_path)


def select_rows(rows: list[dict[str, Any]], company_filter: str, limit: int | None) -> list[dict[str, Any]]:
    selected = rows
    if company_filter:
        selected = [row for row in selected if company_filter.lower() in str(row.get("Company Name") or "").lower()]
    if limit:
        selected = selected[:limit]
    return selected


def log_repair_result(company_name: str, result: WebsiteEvaluation) -> None:
    logger.info("Candidate URLs found for %s: %s", company_name, result.candidate_urls)
    logger.info("Candidate selected for %s: %s", company_name, result.final_url or result.url)
    logger.info("Confidence score for %s: %s (%s)", company_name, result.score, result.confidence)
    logger.info("Verification notes for %s: %s", company_name, "; ".join(result.notes))


def combine_notes(*notes_values: str) -> str:
    notes: list[str] = []
    for value in notes_values:
        for note in str(value or "").split(";"):
            cleaned = note.strip()
            if cleaned and cleaned not in notes:
                notes.append(cleaned)
    return "; ".join(notes)
