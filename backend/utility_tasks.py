from __future__ import annotations

import hashlib
import json
from dataclasses import fields
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any, Callable
from uuid import uuid4

from bs4 import BeautifulSoup

from backend.exports import SnapshotExporter
from backend.file_security import atomic_write_text
from backend.import_security import enforce_record_limit, validate_staged_import
from backend.migration import excel_company_to_api
from backend.outbound_security import OutboundSecurityError, validate_outbound_url
from backend.repository import OpportunityRepository, company_api_to_excel, utc_now
from config import APP_ENABLE_BROWSER_JOBS
from excel_tools import read_company_rows
from job_tools import JobRecord, enrich_job_record
from main import enrich_company
from website_tools import fetch_html, make_session


ProgressCallback = Callable[[int, int, str], None]


class UtilityCancelled(Exception):
    pass


def refresh_single_company_information(
    repository: OpportunityRepository,
    company_id: str,
) -> dict[str, Any]:
    before = repository.get_company(company_id)
    discovered = enrich_company(
        company_api_to_excel(before),
        utc_now(),
        use_browser_discovery=APP_ENABLE_BROWSER_JOBS,
    )
    updates = discovery_to_api(discovered)
    website = str(updates.get("officialWebsite") or before.get("officialWebsite") or before.get("knownWebsite") or "")
    if website:
        updates.update(extract_public_company_metadata(website))
    updates["replaceDiscoveredValues"] = True
    after = repository.update_discovered_company_fields(company_id, updates)
    warnings: list[str] = []
    if discovered.get("Search Status") == "Failed":
        warnings.append("Company discovery could not verify new information; existing saved values were retained.")
    return {
        "company": after,
        "metadataChanged": any(before.get(key) != after.get(key) for key in discoverable_keys()),
        "warnings": warnings,
    }


def refresh_missing_company_information(
    repository: OpportunityRepository,
    progress: ProgressCallback,
    cancelled: Event,
) -> dict[str, Any]:
    companies = [company for company in repository.list_companies() if needs_company_information(company)]
    updated = 0
    boards_verified = 0
    needs_review = 0
    unreachable = 0
    for index, company in enumerate(companies, start=1):
        check_cancelled(cancelled)
        progress(index, len(companies), company["name"])
        before = repository.get_company(company["id"])
        discovered = enrich_company(
            company_api_to_excel(company),
            utc_now(),
            use_browser_discovery=APP_ENABLE_BROWSER_JOBS,
        )
        updates = discovery_to_api(discovered)
        website = str(updates.get("officialWebsite") or company.get("officialWebsite") or company.get("knownWebsite") or "")
        if website:
            updates.update(extract_public_company_metadata(website))
        after = repository.update_discovered_company_fields(company["id"], updates)
        if any(before.get(key) != after.get(key) for key in discoverable_keys()):
            updated += 1
        if not before.get("jobBoardUrl") and after.get("jobBoardUrl"):
            boards_verified += 1
        if after.get("searchStatus") in {"Needs Review", "Partial"}:
            needs_review += 1
        if discovered.get("Search Status") == "Failed":
            unreachable += 1
    return {
        "companiesChecked": len(companies), "companiesUpdated": updated,
        "jobBoardsVerified": boards_verified, "companiesNeedReview": needs_review,
        "couldNotBeReached": unreachable,
    }


def refresh_company_discovery(
    repository: OpportunityRepository,
    progress: ProgressCallback,
    cancelled: Event,
) -> dict[str, Any]:
    from job_board_discovery import discover_job_board_for_row

    companies = repository.list_companies()
    verified = 0
    review = 0
    failed = 0
    updated = 0
    skipped = 0
    for index, company in enumerate(companies, start=1):
        check_cancelled(cancelled)
        progress(index, len(companies), company["name"])
        needs_review = company.get("searchStatus") != "Completed"
        missing_board = not str(company.get("jobBoardUrl") or "").strip()
        changed_urls = company.get("jobBoardDiscoveryMethod") == "Manual Re-verification Required"
        invalid_board = False
        if not needs_review and not missing_board and not changed_urls:
            invalid_board = not public_url_is_reachable(str(company.get("jobBoardUrl") or ""))
        if not (needs_review or missing_board or changed_urls or invalid_board):
            skipped += 1
            continue
        discovery_row = company_api_to_excel(company)
        if invalid_board:
            discovery_row["Job Board URL"] = ""
        discovery = discover_job_board_for_row(
            discovery_row, use_browser_discovery=APP_ENABLE_BROWSER_JOBS,
            max_pages=5, force=False, debug=False,
        )
        updates: dict[str, Any] = {"lastChecked": utc_now()}
        if discovery.found and discovery.candidate_selected:
            updates.update({
                "jobBoardUrl": discovery.candidate_selected,
                "jobBoardDiscoveryMethod": discovery.discovery_method,
                "jobPlatform": discovery.platform,
                "searchStatus": "Completed",
                "replaceInvalidJobBoard": invalid_board,
            })
            verified += 1
        elif discovery.status == "Needs Review":
            updates["searchStatus"] = "Needs Review"
            review += 1
        else:
            failed += 1
        before = repository.get_company(company["id"])
        after = repository.update_discovered_company_fields(company["id"], updates)
        updated += int(any(before.get(key) != after.get(key) for key in discoverable_keys()))
    return {
        "companiesChecked": len(companies), "companiesSkipped": skipped, "companiesUpdated": updated,
        "jobBoardsVerified": verified, "companiesNeedReview": review,
        "couldNotBeReached": failed,
    }


def reprocess_saved_jobs(
    repository: OpportunityRepository,
    progress: ProgressCallback,
    cancelled: Event,
) -> dict[str, Any]:
    jobs = repository.list_jobs()
    processed: list[dict[str, Any]] = []
    changed = 0
    changed_job_ids: list[str] = []
    for index, job in enumerate(jobs, start=1):
        check_cancelled(cancelled)
        progress(index, len(jobs), job.get("companyName", ""))
        job_fields = {item.name for item in fields(JobRecord)}
        enriched = enrich_job_record(JobRecord(**{key: value for key, value in job.items() if key in job_fields}))
        payload = vars(enriched)
        payload["rawData"] = dict(payload.get("rawData") or {})
        normalized_description = " ".join(str(payload.get("description") or "").split())
        payload["description"] = normalized_description
        payload["descriptionSnippet"] = normalized_description[:320]
        benefit_terms = [
            label for label, terms in (
                ("Health insurance", ("health insurance", "medical insurance")),
                ("Dental insurance", ("dental insurance",)),
                ("Vision insurance", ("vision insurance",)),
                ("Retirement plan", ("401(k)", "401k", "retirement plan")),
                ("Paid time off", ("paid time off", "pto")),
                ("Tuition assistance", ("tuition assistance", "tuition reimbursement")),
            ) if any(term in normalized_description.lower() for term in terms)
        ]
        payload.setdefault("rawData", {})["benefits"] = benefit_terms
        job_changed = any(job.get(key) != payload.get(key) for key in ("payMin", "payMax", "payText", "payPeriod", "roleType", "roleTypeReason", "description", "descriptionSnippet", "rawData"))
        changed += int(job_changed)
        if job_changed:
            changed_job_ids.append(job["id"])
        processed.append(payload)
    repository.upsert_jobs(processed)
    return {"jobsProcessed": len(processed), "jobsUpdated": changed, "changedJobIds": changed_job_ids}


def create_backup(
    repository: OpportunityRepository,
    exporter: SnapshotExporter,
    backup_root: Path,
    progress: ProgressCallback,
    cancelled: Event,
) -> dict[str, Any]:
    check_cancelled(cancelled)
    exporter.export_all(include_excel=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(backup_root) / f"backup_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    sources = [
        repository.database_path, exporter.master_path, exporter.companies_json_path,
        exporter.jobs_json_path, exporter.applications_json_path,
        repository.database_path.with_name(".email_secret.key"),
    ]
    entries = []
    for index, source in enumerate(sources, start=1):
        check_cancelled(cancelled)
        progress(index, len(sources), source.name)
        if not source.exists():
            continue
        destination = backup_dir / source.name
        if source == repository.database_path:
            with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as destination_db:
                source_db.backup(destination_db)
        else:
            shutil.copy2(source, destination)
        entries.append({"file": source.name, "bytes": destination.stat().st_size, "sha256": sha256_file(destination)})
    atomic_write_text(backup_dir / "manifest.json", json.dumps({"createdAt": utc_now(), "files": entries}, indent=2))
    return {"backupDirectory": str(backup_dir), "filesBackedUp": len(entries)}


def export_data(exporter: SnapshotExporter, progress: ProgressCallback, cancelled: Event) -> dict[str, Any]:
    check_cancelled(cancelled)
    progress(1, 3, "Companies")
    companies = exporter.export_companies(include_excel=True)
    check_cancelled(cancelled)
    progress(2, 3, "Jobs")
    jobs = exporter.export_jobs(include_excel=True)
    check_cancelled(cancelled)
    progress(3, 3, "Applications")
    applications = exporter.export_applications()
    return {"companiesExported": companies, "jobsExported": jobs, "applicationsExported": applications}


def import_data_file(
    repository: OpportunityRepository,
    exporter: SnapshotExporter,
    path: Path,
    progress: ProgressCallback,
    cancelled: Event,
) -> dict[str, Any]:
    validate_staged_import(path)
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        company_payloads = [excel_company_to_api(row) for row in read_company_rows(path)]
        job_payloads: list[dict[str, Any]] = []
    elif suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            company_payloads = payload.get("companies", [])
            job_payloads = payload.get("jobs", [])
        elif isinstance(payload, list) and (not payload or "title" not in payload[0]):
            company_payloads, job_payloads = payload, []
        elif isinstance(payload, list):
            company_payloads, job_payloads = [], payload
        else:
            raise ValueError("The selected JSON file does not contain company or job records.")
    else:
        raise ValueError("Import supports .xlsx and .json files.")

    company_payloads, job_payloads = enforce_record_limit(company_payloads, job_payloads)
    normalized_companies = [normalize_imported_company(source) for source in company_payloads]
    normalized_jobs = [normalize_imported_job(source) for source in job_payloads]

    imported_companies = 0
    total = len(normalized_companies) + len(normalized_jobs)
    for index, company in enumerate(normalized_companies, start=1):
        check_cancelled(cancelled)
        try:
            existing = repository.get_company(company["id"])
            company = {**existing, **{key: value for key, value in company.items() if value not in (None, "")}}
        except KeyError:
            pass
        progress(index, total, company["name"])
        repository.upsert_company_snapshots([company])
        imported_companies += 1
    imported_jobs = 0
    imported_job_ids: list[str] = []
    for offset, job in enumerate(normalized_jobs, start=len(normalized_companies) + 1):
        check_cancelled(cancelled)
        progress(offset, total, str(job.get("companyName") or "Jobs"))
        imported_jobs += repository.upsert_jobs([job])
        if job.get("id"):
            imported_job_ids.append(str(job["id"]))
    exporter.export_all(include_excel=True)
    return {"companiesImported": imported_companies, "jobsImported": imported_jobs, "jobIds": imported_job_ids}


def needs_company_information(company: dict[str, Any]) -> bool:
    keys = ("officialWebsite", "careersPageUrl", "jobBoardUrl", "jobPlatform", "city", "state", "foundedYear", "totalAssets")
    return not company.get("websiteVerified") or any(company.get(key) in (None, "") for key in keys)


def discovery_to_api(discovered: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "Industry": "industry", "City": "city", "State": "state", "Known Website": "knownWebsite",
        "Official Website": "officialWebsite", "Website Discovery Method": "websiteDiscoveryMethod",
        "Website Candidate URLs": "websiteCandidateUrls", "Website Verification Notes": "websiteVerificationNotes",
        "Website Verified": "websiteVerified", "Careers Page URL": "careersPageUrl",
        "Job Board URL": "jobBoardUrl", "Job Board Discovery Method": "jobBoardDiscoveryMethod",
        "Job Platform": "jobPlatform", "Search Status": "searchStatus", "Last Checked": "lastChecked",
    }
    return {api_key: discovered.get(excel_key) for excel_key, api_key in mapping.items()}


def extract_public_company_metadata(url: str) -> dict[str, Any]:
    try:
        final_url, html = fetch_html(url, make_session())
    except Exception:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(soup.get_text(" ", strip=True).split())
    result: dict[str, Any] = {}
    founded = re.search(r"(?i)\b(?:founded|established|since)\D{0,24}((?:18|19|20)\d{2})\b", text)
    if founded:
        result["foundedYear"] = int(founded.group(1))
    assets = re.search(r"(?i)\b(?:total\s+)?assets\D{0,30}\$?([\d,.]+)\s*(million|billion|m|b)\b", text)
    if assets:
        multiplier = 1_000_000_000 if assets.group(2).lower() in {"b", "billion"} else 1_000_000
        result["totalAssets"] = float(assets.group(1).replace(",", "")) * multiplier
        as_of = re.search(r"(?i)assets.{0,80}?as of\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4}|[A-Za-z]+\s+\d{4})", text)
        if as_of:
            result["assetsAsOfDate"] = as_of.group(1)
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            values = json.loads(script.string or "null")
        except json.JSONDecodeError:
            continue
        for item in flatten_json_ld(values):
            address = item.get("address") if isinstance(item, dict) else None
            if isinstance(address, dict):
                result.setdefault("city", str(address.get("addressLocality") or ""))
                result.setdefault("state", str(address.get("addressRegion") or ""))
    result["officialWebsite"] = final_url
    return {key: value for key, value in result.items() if value not in (None, "")}


def flatten_json_ld(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for value_item in value for item in flatten_json_ld(value_item)]
    if not isinstance(value, dict):
        return []
    return [value, *flatten_json_ld(value.get("@graph", []))]


def public_url_is_reachable(url: str) -> bool:
    if not url:
        return False
    try:
        response = make_session().get(url, timeout=12, allow_redirects=True)
        return response.status_code < 400
    except Exception:
        return False


def normalize_imported_company(source: dict[str, Any]) -> dict[str, Any]:
    if "Company Name" in source:
        company = excel_company_to_api(source)
    else:
        company = dict(source)
    if not str(company.get("name") or "").strip():
        raise ValueError("Every imported company must have a company name.")
    company["id"] = str(company.get("id") or f"company-{uuid4()}")
    company.setdefault("industry", "Financial Services")
    company.setdefault("country", "United States")
    validate_imported_urls(
        company,
        ("knownWebsite", "companyWebsite", "officialWebsite", "careersPageUrl", "jobBoardUrl", "jobsRssFeedUrl"),
    )
    return company


def normalize_imported_job(source: dict[str, Any]) -> dict[str, Any]:
    job = dict(source)
    if not str(job.get("id") or "").strip() or not str(job.get("title") or "").strip():
        raise ValueError("Every imported job must have an ID and title.")
    validate_imported_urls(job, ("sourceUrl",))
    return job


def validate_imported_urls(record: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        value = str(record.get(key) or "").strip()
        if not value:
            continue
        try:
            validate_outbound_url(value)
        except OutboundSecurityError as exc:
            raise ValueError(f"Imported field {key} must contain a safe public HTTP(S) URL.") from exc


def discoverable_keys() -> tuple[str, ...]:
    return ("industry", "city", "state", "officialWebsite", "careersPageUrl", "jobBoardUrl", "jobPlatform", "foundedYear", "totalAssets", "assetsAsOfDate")


def check_cancelled(cancelled: Event) -> None:
    if cancelled.is_set():
        raise UtilityCancelled("Cancelled by user.")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
