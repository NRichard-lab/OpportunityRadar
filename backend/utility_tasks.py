from __future__ import annotations

import json
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import fields
import re
from pathlib import Path
from threading import Event, local
from typing import Any, Callable
from uuid import uuid4

from bs4 import BeautifulSoup

from backend.backup_restore import create_sqlite_backup
from backend.company_information import (
    FIELD_LABELS,
    CompanyInformationDiscovery,
    confirmed_company_domain,
    discover_company_information,
    merge_company_records,
    missing_company_information_fields,
    needs_company_information as company_needs_company_information,
    normalized_company_key,
    plausible_company_domains,
    field_is_clearly_invalid,
)
from backend.exports import SnapshotExporter
from backend.import_security import enforce_record_limit, validate_staged_import
from backend.migration import excel_company_to_api
from backend.outbound_security import OutboundSecurityError, validate_outbound_url
from backend.repository import OpportunityRepository, company_api_to_excel, utc_now
from config import APP_ENABLE_BROWSER_JOBS, APP_MAX_HTTP_WORKERS, DEPLOYMENT_VERSION
from excel_tools import read_company_rows
from job_tools import JobRecord, enrich_job_record
from main import enrich_company
from website_tools import fetch_html, make_session


ProgressCallback = Callable[..., None]


_worker_state = local()


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
    groups, duplicate_records_skipped = _company_refresh_groups(repository.list_companies())
    total = len(groups)
    summary = _new_company_refresh_summary(total, duplicate_records_skipped)
    progress(0, total, "Preparing company review", dict(summary))
    if not groups:
        return summary

    max_workers = max(1, min(APP_MAX_HTTP_WORKERS, total))
    group_iterator = iter(groups)
    pending: dict[Future[CompanyInformationDiscovery], dict[str, Any]] = {}

    def submit_next(executor: ThreadPoolExecutor) -> bool:
        if cancelled.is_set():
            return False
        try:
            group = next(group_iterator)
        except StopIteration:
            return False
        # Current-company updates stay compact; per-company results are published
        # separately as bounded live snapshots below.
        progress(summary["processedCount"], total, group["name"])
        future = executor.submit(_discover_company_refresh_group, group, cancelled)
        pending[future] = group
        return True

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="company-info") as executor:
        for _ in range(max_workers):
            if not submit_next(executor):
                break

        while pending:
            done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for future in done:
                group = pending.pop(future)
                if cancelled.is_set() and future.cancelled():
                    continue
                try:
                    discovery = future.result()
                except (InterruptedError, UtilityCancelled):
                    if cancelled.is_set():
                        continue
                    discovery = CompanyInformationDiscovery(failed=True, notes=["The company review was interrupted."])
                except Exception as exc:
                    discovery = CompanyInformationDiscovery(
                        failed=True,
                        notes=[f"The company could not be reviewed: {_safe_company_error(exc)}"],
                    )
                company_result = _save_company_refresh_group(repository, group, discovery)
                summary["processedCount"] += 1
                summary[f"{_outcome_counter(company_result['outcome'])}"] += 1
                summary["companyResults"].append(company_result)
                summary["jobBoardsVerified"] += int(
                    "Job-board URL" in company_result.get("updatedFields", [])
                )
                _add_legacy_company_summary_fields(summary)
                live_summary = dict(summary)
                live_summary["companyResults"] = [company_result]
                progress(
                    summary["processedCount"],
                    total,
                    group["name"],
                    live_summary,
                )
                submit_next(executor)

    if cancelled.is_set():
        # Preserve the complete partial report once when cancellation finishes,
        # rather than rewriting the growing list after every company.
        progress(
            summary["processedCount"],
            total,
            summary["companyResults"][-1]["companyName"] if summary["companyResults"] else "Cancelled",
            dict(summary),
        )
        raise UtilityCancelled("Cancelled by user.")
    _add_legacy_company_summary_fields(summary)
    return summary


def _discover_company_refresh_group(
    group: dict[str, Any],
    cancelled: Event,
) -> CompanyInformationDiscovery:
    check_cancelled(cancelled)
    return discover_company_information(
        group["merged"],
        requested_fields=group["requestedFields"],
        session=_company_refresh_session(),
        use_browser_discovery=APP_ENABLE_BROWSER_JOBS,
        cancelled=cancelled,
    )


def _company_refresh_session():
    session = getattr(_worker_state, "company_information_session", None)
    if session is None:
        session = make_session()
        _worker_state.company_information_session = session
    return session


def _company_refresh_groups(companies: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    by_name: dict[str, list[dict[str, Any]]] = {}
    for company in companies:
        name_key = normalized_company_key(str(company.get("name") or ""))
        by_name.setdefault(name_key or str(company.get("id") or id(company)), []).append(company)

    by_identity: dict[str, list[dict[str, Any]]] = {}
    for name_key, records in by_name.items():
        confirmed_domains = {
            domain for record in records
            if (domain := confirmed_company_domain(record))
        }
        sole_confirmed_domain = next(iter(confirmed_domains)) if len(confirmed_domains) == 1 else ""
        for company in records:
            plausible_domains = plausible_company_domains(company)
            plausible_domain = next(iter(plausible_domains)) if len(plausible_domains) == 1 else ""
            # A name alone is not a safe merge key. A blank legacy duplicate can
            # share the sole verified domain for that name, but conflicting domains
            # remain independent to avoid cross-filling distinct organizations.
            can_share_sole_domain = bool(
                sole_confirmed_domain
                and (not plausible_domains or plausible_domains == {sole_confirmed_domain})
            )
            domain = confirmed_company_domain(company)
            key = (
                f"{name_key}|domain:{sole_confirmed_domain}"
                if can_share_sole_domain
                else f"{name_key}|domain:{domain}"
                if domain and plausible_domain == domain
                else f"{name_key}|record:{company.get('id') or id(company)}"
            )
            by_identity.setdefault(key, []).append(company)

    groups: list[dict[str, Any]] = []
    duplicates_skipped = 0
    for records in by_identity.values():
        selected = [record for record in records if company_needs_company_information(record)]
        if not selected:
            continue
        requested_fields = set().union(
            *(missing_company_information_fields(record) for record in selected)
        )
        merged = merge_company_records(records)
        merged["name"] = str(selected[0].get("name") or merged.get("name") or "")
        groups.append(
            {
                "name": merged["name"],
                "merged": merged,
                "selectedRecords": selected,
                "requestedFields": requested_fields,
            }
        )
        duplicates_skipped += max(0, len(selected) - 1)
    groups.sort(key=lambda group: (str(group["name"]).casefold(), str(group["merged"].get("id") or "")))
    return groups, duplicates_skipped


def _save_company_refresh_group(
    repository: OpportunityRepository,
    group: dict[str, Any],
    discovery: CompanyInformationDiscovery,
) -> dict[str, Any]:
    changed_fields: set[str] = set()
    errors: list[str] = []
    saved_records = 0
    remaining_requested: set[str] = set()
    updates = {**discovery.updates, "lastChecked": utc_now()}
    replacement_sources = updates.pop("replacementSourceValues", {})
    requested_replacements = updates.get("replaceConfirmedFields", [])
    change_keys = (*FIELD_LABELS.keys(), "websiteVerified")
    for record in group["selectedRecords"]:
        try:
            before = repository.get_company(record["id"])
            record_updates = dict(updates)
            replacement_candidates = set(requested_replacements)
            replacement_candidates.update(
                field_name
                for field_name in group["requestedFields"]
                if field_name in discovery.updates
                and field_name in FIELD_LABELS
                and bool(str(before.get(field_name) or "").strip())
                and field_is_clearly_invalid(before, field_name)
            )
            allowed_replacements = [
                field_name
                for field_name in sorted(replacement_candidates)
                if field_is_clearly_invalid(before, field_name)
                or _same_replacement_source(before.get(field_name), replacement_sources.get(field_name))
            ]
            if allowed_replacements:
                record_updates["replaceConfirmedFields"] = allowed_replacements
            else:
                record_updates.pop("replaceConfirmedFields", None)
            record_updates["searchStatus"] = _company_status_after_updates(
                before,
                record_updates,
                set(allowed_replacements),
            )
            record_updates["reconcileSearchStatus"] = True
            after = repository.update_discovered_company_fields(record["id"], record_updates)
            saved_records += 1
            changed_fields.update(
                key for key in change_keys if before.get(key) != after.get(key)
            )
            remaining_requested.update(
                missing_company_information_fields(after).intersection(group["requestedFields"])
            )
        except Exception as exc:
            errors.append(f"{record.get('name') or record.get('id')}: {_safe_company_error(exc)}")

    if changed_fields:
        outcome = "updated"
        message = f"Updated {', '.join(_company_field_label(key) for key in sorted(changed_fields))}."
        if remaining_requested:
            message += (
                f" Still missing {', '.join(_company_field_label(key) for key in sorted(remaining_requested))}."
            )
    elif discovery.failed or (errors and not saved_records):
        outcome = "failed"
        message = "The company could not be reviewed; existing information was preserved."
    elif remaining_requested:
        outcome = "no_information_found"
        message = "No additional high-confidence information was found; existing information was preserved."
    else:
        outcome = "unchanged"
        message = "Confirmed available information; no saved values needed to change."

    if len(group["selectedRecords"]) > 1:
        message += f" One lookup safely covered {len(group['selectedRecords'])} duplicate records."
    useful_notes = [note for note in discovery.notes if note]
    if errors:
        useful_notes.extend(errors)
    if useful_notes:
        message += f" {' '.join(useful_notes[:2])}"
    representative = group["selectedRecords"][0]
    reported_found_fields = [
        key for key in discovery.found_fields
        if key in group["requestedFields"] or key in changed_fields
    ]
    return {
        "companyId": str(representative.get("id") or ""),
        "companyName": str(group["name"]),
        "outcome": outcome,
        "foundFields": [_company_field_label(key) for key in reported_found_fields],
        "updatedFields": [_company_field_label(key) for key in sorted(changed_fields)],
        "message": message,
    }


def _same_replacement_source(current: Any, source: Any) -> bool:
    current_value = str(current or "").strip().rstrip("/")
    source_value = str(source or "").strip().rstrip("/")
    return bool(current_value and source_value and current_value == source_value)


def _company_status_after_updates(
    current: dict[str, Any],
    updates: dict[str, Any],
    allowed_replacements: set[str],
) -> str:
    effective = dict(current)
    for field_name in FIELD_LABELS:
        value = updates.get(field_name)
        if value in (None, ""):
            continue
        if current.get(field_name) in (None, "") or field_name in allowed_replacements:
            effective[field_name] = value

    if updates.get("websiteVerified"):
        discovered_website = str(
            updates.get("officialWebsite") or current.get("officialWebsite") or ""
        )
        current_website = str(current.get("officialWebsite") or "")
        if (
            not current_website
            or discovered_website == current_website
            or "officialWebsite" in allowed_replacements
        ):
            effective["websiteVerified"] = True

    return "Completed" if not missing_company_information_fields(effective) else "Partial"


def _new_company_refresh_summary(total: int, duplicates_skipped: int) -> dict[str, Any]:
    return {
        "totalCompaniesNeedingReview": total,
        "processedCount": 0,
        "updatedCount": 0,
        "noInformationFoundCount": 0,
        "failedCount": 0,
        "unchangedCount": 0,
        "duplicateRecordsSkipped": duplicates_skipped,
        "companiesChecked": 0,
        "companiesUpdated": 0,
        "companiesNeedReview": 0,
        "couldNotBeReached": 0,
        "jobBoardsVerified": 0,
        "companyResults": [],
    }


def _company_field_label(field_name: str) -> str:
    if field_name == "websiteVerified":
        return "Website verification"
    return FIELD_LABELS.get(field_name, field_name)


def _outcome_counter(outcome: str) -> str:
    return {
        "updated": "updatedCount",
        "no_information_found": "noInformationFoundCount",
        "failed": "failedCount",
        "unchanged": "unchangedCount",
    }[outcome]


def _add_legacy_company_summary_fields(summary: dict[str, Any]) -> None:
    summary.update(
        {
            "companiesChecked": summary["processedCount"],
            "companiesUpdated": summary["updatedCount"],
            "companiesNeedReview": summary["noInformationFoundCount"] + summary["unchangedCount"],
            "couldNotBeReached": summary["failedCount"],
        }
    )


def _safe_company_error(exc: Exception) -> str:
    if isinstance(exc, (KeyError, ValueError)):
        return str(exc)[:240]
    return f"{type(exc).__name__} while saving or validating the company"


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
    check_cancelled(cancelled)
    # Snapshot JSON/XLSX exports are not database backups. The authoritative SQLite
    # state is captured directly with SQLite's online backup API and validated before
    # its artifact becomes visible.
    result = create_sqlite_backup(
        repository.database_path,
        Path(backup_root),
        deployment_version=DEPLOYMENT_VERSION,
        additional_files={
            source.name: source
            for source in (
                exporter.master_path,
                exporter.companies_json_path,
                exporter.jobs_json_path,
                exporter.applications_json_path,
                repository.database_path.with_name(".email_secret.key"),
            )
        },
    )
    progress(1, 1, repository.database_path.name)
    return result


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
    return company_needs_company_information(company)


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
            if isinstance(item, dict):
                description = " ".join(str(item.get("description") or "").split())
                if len(description) >= 40:
                    result.setdefault("companyDescription", description[:1500])
                industry = item.get("industry")
                if isinstance(industry, str) and industry.strip():
                    result.setdefault("industry", " ".join(industry.split()))
    if not result.get("companyDescription"):
        for attributes in (
            {"property": re.compile(r"^og:description$", re.I)},
            {"name": re.compile(r"^description$", re.I)},
        ):
            meta = soup.find("meta", attrs=attributes)
            description = " ".join(str(meta.get("content") if meta else "").split())
            if len(description) >= 40:
                result["companyDescription"] = description[:1500]
                break
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
    return (
        "industry", "companyDescription", "city", "state", "officialWebsite",
        "careersPageUrl", "jobBoardUrl", "jobPlatform", "foundedYear",
        "totalAssets", "assetsAsOfDate",
    )


def check_cancelled(cancelled: Event) -> None:
    if cancelled.is_set():
        raise UtilityCancelled("Cancelled by user.")
