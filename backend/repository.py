from __future__ import annotations

import json
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from uuid import uuid4

from backend.db import connect, initialize_schema, normalize_company_name
from backend.match_constants import MATCH_ALGORITHM_VERSION, job_match_fingerprint


COMPANY_SELECT = """SELECT id, name, normalized_name, industry, company_description,
 city, state, country, known_website,
 official_website, website_discovery_method, website_candidate_urls,
website_verification_notes, website_verified, careers_page_url, job_board_url,
job_board_discovery_method, jobs_rss_feed_url, job_platform, feed_found,
search_status, confidence, last_checked, notes, founded_year, total_assets,
 assets_as_of_date, company_info_last_checked FROM companies"""


class DuplicateCompanyError(ValueError):
    """Raised when a create or rename would duplicate a normalized company name."""

    def __init__(self, name: str, existing_company_id: str, existing_name: str) -> None:
        self.name = name
        self.existing_company_id = existing_company_id
        self.existing_name = existing_name
        super().__init__(f'A company named "{existing_name}" already exists.')

JOB_MATCH_SELECT = """SELECT j.*, r.version AS active_resume_version,
f.score AS match_score, f.status AS match_status, f.resume_version AS match_resume_version,
f.job_fingerprint AS match_job_fingerprint, f.algorithm_version AS match_algorithm_version,
f.matched_at AS match_matched_at, f.error AS match_error, f.payload_json AS match_payload_json
FROM jobs j
LEFT JOIN resumes r ON r.id='current'
LEFT JOIN resume_fit_results f ON f.resume_id='current' AND f.job_id=j.id"""


class OpportunityRepository:
    def __init__(
        self,
        database_path: Path,
        *,
        initialize: bool = False,
        require_existing: bool = False,
    ) -> None:
        self.database_path = Path(database_path)
        self.require_existing = require_existing
        if initialize:
            with self.connection() as connection:
                initialize_schema(connection)

    @contextmanager
    def connection(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        connection = connect(
            self.database_path,
            readonly=readonly,
            require_existing=self.require_existing,
        )
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def list_companies(self) -> list[dict[str, Any]]:
        with self.connection(readonly=True) as connection:
            rows = connection.execute(f"{COMPANY_SELECT} ORDER BY name COLLATE NOCASE").fetchall()
        return [company_row_to_api(row) for row in rows]

    def query_companies(
        self,
        *,
        page: int = 1,
        page_size: int = 25,
        search: str = "",
        state: str = "",
        industry: str = "",
        job_board_type: str = "",
        discovery_status: str = "",
        has_verified_job_board: bool | None = None,
        has_active_jobs: bool | None = None,
        sort_by: str = "companyName",
        sort_direction: str = "asc",
    ) -> dict[str, Any]:
        sort_columns = {
            "companyName": "c.name COLLATE NOCASE",
            "city": "c.city COLLATE NOCASE",
            "state": "c.state COLLATE NOCASE",
            "jobBoardType": "c.job_platform COLLATE NOCASE",
            "discoveryStatus": "c.search_status COLLATE NOCASE",
            "jobCount": "active_job_count",
            "lastCollectionDate": "last_collection_date",
        }
        sort_column = sort_columns.get(sort_by, sort_columns["companyName"])
        direction = "DESC" if sort_direction.lower() == "desc" else "ASC"
        page_size = page_size if page_size in {25, 50, 100} else 25

        where: list[str] = []
        parameters: list[Any] = []
        if search.strip():
            term = f"%{search.strip().casefold()}%"
            where.append("""(LOWER(c.name) LIKE ? OR LOWER(c.city) LIKE ? OR
                LOWER(c.state) LIKE ? OR LOWER(c.known_website) LIKE ? OR
                LOWER(c.official_website) LIKE ? OR LOWER(c.careers_page_url) LIKE ? OR
                LOWER(c.job_board_url) LIKE ?)""")
            parameters.extend([term] * 7)
        for column, value in (
            ("c.state", state),
            ("c.industry", industry),
            ("c.job_platform", job_board_type),
            ("c.search_status", discovery_status),
        ):
            if value:
                where.append(f"{column} = ?")
                parameters.append(value)
        if has_verified_job_board is not None:
            where.append("TRIM(c.job_board_url) <> ''" if has_verified_job_board else "TRIM(c.job_board_url) = ''")
        if has_active_jobs is not None:
            where.append("active_job_count > 0" if has_active_jobs else "active_job_count = 0")

        metrics = """WITH company_metrics AS (
            SELECT c.id,
                COALESCE(SUM(CASE WHEN LOWER(j.status) = 'open' THEN 1 ELSE 0 END), 0) AS active_job_count,
                COALESCE(MAX(NULLIF(j.collected_at, '')), '') AS last_collection_date
            FROM companies c LEFT JOIN jobs j ON j.company_id = c.id
            GROUP BY c.id
        )"""
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        from_sql = "FROM companies c JOIN company_metrics m ON m.id = c.id"

        with self.connection(readonly=True) as connection:
            total = connection.execute(
                f"{metrics} SELECT COUNT(*) {from_sql} {where_sql}", parameters
            ).fetchone()[0]
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(max(1, page), total_pages)
            offset = (page - 1) * page_size
            rows = connection.execute(
                f"""{metrics}
                SELECT c.*, m.active_job_count, m.last_collection_date,
                    (SELECT COUNT(*) FROM applications a JOIN jobs aj ON aj.id = a.job_id
                     WHERE aj.company_id = c.id AND a.archived_at IS NULL AND a.applied = 1) AS applied_count
                {from_sql} {where_sql}
                ORDER BY {sort_column} {direction}, c.id COLLATE NOCASE ASC
                LIMIT ? OFFSET ?""",
                [*parameters, page_size, offset],
            ).fetchall()
            options = {
                "states": distinct_values(connection, "state"),
                "industries": distinct_values(connection, "industry"),
                "jobBoardTypes": distinct_values(connection, "job_platform"),
                "discoveryStatuses": distinct_values(connection, "search_status"),
            }
        return {
            "items": [company_row_to_api(row) for row in rows],
            "page": page,
            "pageSize": page_size,
            "total": total,
            "totalPages": total_pages,
            "options": options,
        }

    def list_company_rows(self) -> list[dict[str, Any]]:
        return [company_api_to_excel(company) for company in self.list_companies()]

    def get_company(self, company_id: str) -> dict[str, Any]:
        with self.connection(readonly=True) as connection:
            row = connection.execute(f"{COMPANY_SELECT} WHERE id = ?", (company_id,)).fetchone()
        if row is None:
            raise KeyError(company_id)
        return company_row_to_api(row)

    def create_company(self, payload: dict[str, str]) -> dict[str, Any]:
        company_id = f"company-{uuid4()}"
        now = utc_now()
        name = payload["name"].strip()
        normalized_name = normalize_company_name(name)
        if not normalized_name:
            raise ValueError("Company Name is required.")
        website = payload.get("companyWebsite", "").strip()
        job_board = payload.get("jobBoardUrl", "").strip()
        values = (
            company_id, name, normalized_name, payload.get("industry", "Financial Services").strip(),
            payload.get("companyDescription", "").strip(),
            payload.get("city", "").strip(), payload.get("state", "").strip(),
            payload.get("country", "United States").strip(), website, website,
            "Not Found", "", "", 0, payload.get("careersPageUrl", "").strip(), job_board,
            "Manual" if job_board else "Not Found", "", "", 0, "Needs Review", 0, "",
            payload.get("notes", "").strip(), now, now,
        )
        with self.connection() as connection:
            duplicate = find_company_by_normalized_name(connection, normalized_name)
            if duplicate is not None:
                raise duplicate_company_error(name, duplicate)
            try:
                connection.execute(
                    """INSERT INTO companies (id, name, normalized_name, industry,
                company_description, city, state, country,
                known_website, official_website, website_discovery_method,
                website_candidate_urls, website_verification_notes, website_verified,
                careers_page_url, job_board_url, job_board_discovery_method,
                jobs_rss_feed_url, job_platform, feed_found, search_status, confidence,
                last_checked, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
            except sqlite3.IntegrityError as exc:
                duplicate = find_company_by_normalized_name(connection, normalized_name)
                if duplicate is not None:
                    raise duplicate_company_error(name, duplicate) from None
                raise exc
        return self.get_company(company_id)

    def update_company(self, company_id: str, payload: dict[str, str]) -> dict[str, Any]:
        name = payload["name"].strip()
        normalized_name = normalize_company_name(name)
        if not normalized_name:
            raise ValueError("Company Name is required.")
        website = payload.get("companyWebsite", "").strip()
        careers = payload.get("careersPageUrl", "").strip()
        job_board = payload.get("jobBoardUrl", "").strip()
        with self.connection() as connection:
            row = connection.execute(f"{COMPANY_SELECT} WHERE id = ?", (company_id,)).fetchone()
            if row is None:
                raise KeyError(company_id)
            current = company_row_to_api(row)
            current_normalized_name = normalize_company_name(current["name"])
            duplicate = find_company_by_normalized_name(
                connection, normalized_name, exclude_id=company_id,
            )
            if duplicate is not None and normalized_name != current_normalized_name:
                raise duplicate_company_error(name, duplicate)
            stored_normalized_name = normalized_name_for_existing_company(
                row, normalized_name, duplicate,
            )
            urls_changed = any((current[api_key] or "").strip() != value for api_key, value in (
                ("officialWebsite", website), ("careersPageUrl", careers), ("jobBoardUrl", job_board)
            ))
            company_description = payload.get("companyDescription", "").strip()
            if not company_description:
                company_description = current.get("companyDescription", "")
            try:
                cursor = connection.execute(
                    """UPDATE companies SET name = ?, normalized_name = ?, industry = ?,
                company_description = ?, city = ?, state = ?, country = ?,
                known_website = ?, official_website = ?, careers_page_url = ?, job_board_url = ?,
                notes = ?, website_verified = CASE WHEN ? THEN 0 ELSE website_verified END,
                job_board_discovery_method = CASE WHEN ? THEN 'Manual Re-verification Required' ELSE job_board_discovery_method END,
                search_status = CASE WHEN ? THEN 'Needs Review' ELSE search_status END,
                last_checked = CASE WHEN ? THEN '' ELSE last_checked END, updated_at = ? WHERE id = ?""",
                    (
                        name, stored_normalized_name,
                        payload.get("industry", "Financial Services").strip(), company_description,
                        payload.get("city", "").strip(), payload.get("state", "").strip(),
                        payload.get("country", "United States").strip(), website, website, careers,
                        job_board, payload.get("notes", "").strip(), urls_changed, urls_changed,
                        urls_changed, urls_changed, utc_now(), company_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                duplicate = find_company_by_normalized_name(
                    connection, normalized_name, exclude_id=company_id,
                )
                if duplicate is not None:
                    raise duplicate_company_error(name, duplicate) from None
                raise exc
            if cursor.rowcount != 1:
                raise KeyError(company_id)
        company = self.get_company(company_id)
        company["jobBoardReverificationRequired"] = urls_changed
        return company

    def update_discovered_company_fields(self, company_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        current = self.get_company(company_id)
        columns = {
            "industry": "industry", "companyDescription": "company_description",
            "city": "city", "state": "state",
            "knownWebsite": "known_website", "officialWebsite": "official_website",
            "websiteDiscoveryMethod": "website_discovery_method",
            "websiteCandidateUrls": "website_candidate_urls",
            "websiteVerificationNotes": "website_verification_notes",
            "careersPageUrl": "careers_page_url", "jobBoardUrl": "job_board_url",
            "jobBoardDiscoveryMethod": "job_board_discovery_method", "jobPlatform": "job_platform",
            "foundedYear": "founded_year", "totalAssets": "total_assets",
            "assetsAsOfDate": "assets_as_of_date",
        }
        raw_replace_confirmed = updates.get("replaceConfirmedFields")
        replace_confirmed_fields = {
            str(field) for field in raw_replace_confirmed
            if isinstance(field, str) and field in columns
        } if isinstance(raw_replace_confirmed, (list, tuple, set, frozenset)) else set()
        assignments: list[str] = []
        parameters: list[Any] = []
        for api_key, column in columns.items():
            value = updates.get(api_key)
            if api_key == "companyDescription" and isinstance(value, str):
                value = value.strip()
            may_replace_invalid_board = api_key == "jobBoardUrl" and updates.get("replaceInvalidJobBoard")
            may_replace_confirmed = api_key in replace_confirmed_fields
            may_replace_on_refresh = updates.get("replaceDiscoveredValues") and api_key in {
                "officialWebsite", "websiteDiscoveryMethod", "websiteCandidateUrls",
                "websiteVerificationNotes", "careersPageUrl", "jobBoardUrl",
                "jobBoardDiscoveryMethod", "jobPlatform", "totalAssets", "assetsAsOfDate",
            }
            if value in (None, "") or (
                current.get(api_key) not in (None, "")
                and not may_replace_invalid_board
                and not may_replace_confirmed
                and not may_replace_on_refresh
            ):
                continue
            assignments.append(f"{column} = ?")
            parameters.append(value)

        discovered_website = str(updates.get("officialWebsite") or current.get("officialWebsite") or "")
        if updates.get("websiteVerified") and (
            updates.get("replaceDiscoveredValues")
            or "officialWebsite" in replace_confirmed_fields
            or discovered_website == str(current.get("officialWebsite") or discovered_website)
        ):
            assignments.append("website_verified = 1")
        if updates.get("searchStatus") and (
            current.get("searchStatus") != "Completed" or updates.get("reconcileSearchStatus")
        ):
            assignments.append("search_status = ?")
            parameters.append(updates["searchStatus"])
        assignments.extend(["last_checked = ?", "company_info_last_checked = ?", "updated_at = ?"])
        checked_at = str(updates.get("lastChecked") or utc_now())
        parameters.extend([checked_at, checked_at, utc_now(), company_id])
        with self.connection() as connection:
            cursor = connection.execute(
                f"UPDATE companies SET {', '.join(assignments)} WHERE id = ?", parameters
            )
            if cursor.rowcount != 1:
                raise KeyError(company_id)
        return self.get_company(company_id)

    def delete_company(self, company_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            company = connection.execute("SELECT name FROM companies WHERE id = ?", (company_id,)).fetchone()
            if company is None:
                raise KeyError(company_id)
            job_ids = [row[0] for row in connection.execute("SELECT id FROM jobs WHERE company_id = ?", (company_id,))]
            candidate_count = connection.execute("SELECT COUNT(*) FROM raw_job_candidates WHERE company_id = ?", (company_id,)).fetchone()[0]
            application_count = 0
            if job_ids:
                placeholders = ",".join("?" for _ in job_ids)
                application_count = connection.execute(f"SELECT COUNT(*) FROM applications WHERE job_id IN ({placeholders})", job_ids).fetchone()[0]
            connection.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        return {
            "message": "Company and related job data deleted.",
            "deletedCompanyId": company_id,
            "deletedJobIds": sorted(job_ids),
            "deletedJobs": len(job_ids),
            "deletedRawCandidates": candidate_count,
            "deletedApplications": application_count,
        }

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.connection(readonly=True) as connection:
            rows = connection.execute(
                f"{JOB_MATCH_SELECT} ORDER BY j.company_name COLLATE NOCASE, j.title COLLATE NOCASE"
            ).fetchall()
        return [job_row_to_api(row) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.connection(readonly=True) as connection:
            row = connection.execute(f"{JOB_MATCH_SELECT} WHERE j.id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return job_row_to_api(row)

    def replace_jobs(self, jobs: Iterable[dict[str, Any]]) -> int:
        prepared = self._prepare_jobs(list(jobs))
        now = utc_now()
        with self.connection() as connection:
            for job in prepared:
                insert_job(connection, job, now)
            prune_missing_jobs(connection, {job["id"] for job in prepared})
        return len(prepared)

    def upsert_jobs(self, jobs: Iterable[dict[str, Any]]) -> int:
        prepared = self._prepare_jobs(list(jobs))
        now = utc_now()
        with self.connection() as connection:
            for job in prepared:
                insert_job(connection, job, now)
        return len(prepared)

    def upsert_jobs_for_companies(self, jobs: Iterable[dict[str, Any]], company_ids: set[str]) -> int:
        prepared = self._prepare_jobs(list(jobs))
        now = utc_now()
        with self.connection() as connection:
            for job in prepared:
                insert_job(connection, job, now)
            prune_missing_jobs(connection, {job["id"] for job in prepared}, company_ids=company_ids)
        return len(prepared)

    def _prepare_jobs(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        company_ids = {company["id"] for company in self.list_companies()}
        by_name = {company["name"].casefold(): company["id"] for company in self.list_companies()}
        used: set[str] = set()
        prepared: list[dict[str, Any]] = []
        for source in jobs:
            job = dict(source)
            legacy_id = str(job.get("legacyId") or job.get("id") or "")
            if not legacy_id or not str(job.get("title") or "").strip():
                continue
            job_id = legacy_id
            if job_id in used:
                basis = "|".join(str(job.get(key) or "") for key in ("companyId", "title", "sourceUrl", "location"))
                digest = hashlib.sha256(basis.encode()).hexdigest()[:12]
                job_id = f"{legacy_id}--{digest}"
                ordinal = 2
                while job_id in used:
                    job_id = f"{legacy_id}--{digest}-{ordinal}"
                    ordinal += 1
            used.add(job_id)
            job["id"] = job_id
            job["legacyId"] = legacy_id
            if job.get("companyId") not in company_ids:
                job["companyId"] = by_name.get(str(job.get("companyName") or "").casefold(), "")
            prepared.append(job)
        return prepared

    def list_applications(self) -> dict[str, dict[str, Any]]:
        with self.connection(readonly=True) as connection:
            rows = connection.execute("SELECT * FROM applications WHERE archived_at IS NULL").fetchall()
        return {row["job_id"]: application_row_to_api(row) for row in rows}

    def upsert_application(self, job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        if not self.job_exists(job_id):
            raise KeyError(job_id)
        existing = self.list_applications().get(job_id, {})
        merged = {**existing, **patch}
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO applications (job_id, applied, application_status, date_applied,
                follow_up_date, notes, not_interested, payload_json, archived_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(job_id) DO UPDATE SET applied=excluded.applied,
                application_status=excluded.application_status, date_applied=excluded.date_applied,
                follow_up_date=excluded.follow_up_date, notes=excluded.notes,
                not_interested=excluded.not_interested, payload_json=excluded.payload_json,
                archived_at=NULL, updated_at=excluded.updated_at""",
                (
                    job_id, bool(merged.get("applied")), merged.get("applicationStatus", "Interested"),
                    merged.get("dateApplied", ""), merged.get("followUpDate", ""), merged.get("notes", ""),
                    bool(merged.get("notInterested")), json.dumps(merged, sort_keys=True), now,
                ),
            )
        return self.list_applications()[job_id]

    def import_application_overrides(self, overrides: dict[str, dict[str, Any]]) -> dict[str, Any]:
        imported: list[str] = []
        skipped: list[str] = []
        for job_id, patch in overrides.items():
            try:
                self.upsert_application(job_id, patch)
                imported.append(job_id)
            except KeyError:
                skipped.append(job_id)
        return {"importedJobIds": imported, "skippedJobIds": skipped}

    def job_exists(self, job_id: str) -> bool:
        with self.connection(readonly=True) as connection:
            return connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is not None

    def get_resume(self, resume_id: str = "current") -> dict[str, Any] | None:
        with self.connection(readonly=True) as connection:
            row = connection.execute("SELECT version,payload_json FROM resumes WHERE id = ?", (resume_id,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        payload["version"] = row["version"]
        return payload

    def upsert_resume(self, payload: dict[str, Any], resume_id: str = "current") -> dict[str, Any]:
        now = utc_now()
        normalized = dict(payload)
        resume_text = str(normalized.get("extractedText") or normalized.get("rawText") or "")
        version = str(normalized.get("version") or normalized.get("id") or hashlib.sha256(
            f"{normalized.get('fileName', '')}|{resume_text}".encode()
        ).hexdigest()[:24])
        normalized["id"] = version
        normalized["version"] = version
        normalized["rawText"] = resume_text
        normalized["extractedText"] = resume_text
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO resumes (id, version, name, file_name, uploaded_at, extracted_text,
                skills_json, payload_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET version=excluded.version,name=excluded.name,file_name=excluded.file_name,
                uploaded_at=excluded.uploaded_at, extracted_text=excluded.extracted_text,
                skills_json=excluded.skills_json, payload_json=excluded.payload_json,
                updated_at=excluded.updated_at""",
                (
                    resume_id, version, normalized.get("name", ""), normalized.get("fileName", ""),
                    normalized.get("uploadedAt", ""), resume_text, json.dumps(normalized.get("skills", [])),
                    json.dumps(normalized, sort_keys=True), now,
                ),
            )
        return normalized

    def upsert_resume_fit_result(
        self,
        *,
        job_id: str,
        resume_version: str,
        job_fingerprint: str,
        status: str,
        score: float | None,
        details: dict[str, Any],
        error: str = "",
        algorithm_version: str = MATCH_ALGORITHM_VERSION,
    ) -> dict[str, Any]:
        now = utc_now()
        result_id = "match-" + hashlib.sha256(f"current|{job_id}".encode()).hexdigest()[:32]
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO resume_fit_results
                (id,resume_id,job_id,score,status,resume_version,job_fingerprint,algorithm_version,
                matched_at,error,payload_json,created_at) VALUES (?,'current',?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(resume_id,job_id) DO UPDATE SET score=excluded.score,status=excluded.status,
                resume_version=excluded.resume_version,job_fingerprint=excluded.job_fingerprint,
                algorithm_version=excluded.algorithm_version,matched_at=excluded.matched_at,
                error=excluded.error,payload_json=excluded.payload_json,created_at=excluded.created_at""",
                (
                    result_id, job_id, score, status, resume_version, job_fingerprint,
                    algorithm_version, now, error, json.dumps(details, sort_keys=True), now,
                ),
            )
        return self.get_resume_fit_result(job_id) or {}

    def get_resume_fit_result(self, job_id: str) -> dict[str, Any] | None:
        with self.connection(readonly=True) as connection:
            row = connection.execute(
                "SELECT * FROM resume_fit_results WHERE resume_id='current' AND job_id=?",
                (job_id,),
            ).fetchone()
        return fit_result_to_api(row) if row else None

    def upsert_company_snapshots(self, companies: Iterable[dict[str, Any]]) -> int:
        prepared = [dict(company) for company in companies]
        now = utc_now()
        with self.connection() as connection:
            for source in prepared:
                incoming_id = str(source.get("id") or "").strip()
                existing_row = connection.execute(
                    f"{COMPANY_SELECT} WHERE id = ?", (incoming_id,),
                ).fetchone() if incoming_id else None
                name = str(source.get("name") or "").strip()
                if existing_row is not None and not name:
                    name = str(existing_row["name"])
                normalized_name = normalize_company_name(name)
                if not normalized_name:
                    raise ValueError("Company Name is required.")
                source["name"] = name

                if existing_row is not None:
                    current = company_row_to_api(existing_row)
                    duplicate = find_company_by_normalized_name(
                        connection, normalized_name, exclude_id=incoming_id,
                    )
                    if (
                        duplicate is not None
                        and normalized_name != normalize_company_name(current["name"])
                    ):
                        raise duplicate_company_error(name, duplicate)
                    stored_normalized_name = normalized_name_for_existing_company(
                        existing_row, normalized_name, duplicate,
                    )
                    company = merge_company_snapshot(current, source, prefer_incoming=True)
                    company["id"] = incoming_id
                else:
                    duplicate = find_company_by_normalized_name(connection, normalized_name)
                    if duplicate is not None:
                        target_row = connection.execute(
                            f"{COMPANY_SELECT} WHERE id = ?", (duplicate["id"],),
                        ).fetchone()
                        if target_row is None:  # pragma: no cover - same-transaction defensive guard
                            raise KeyError(duplicate["id"])
                        other_duplicate = find_company_by_normalized_name(
                            connection, normalized_name, exclude_id=duplicate["id"],
                        )
                        stored_normalized_name = normalized_name_for_existing_company(
                            target_row, normalized_name, other_duplicate,
                        )
                        company = merge_company_snapshot(
                            company_row_to_api(target_row), source, prefer_incoming=False,
                        )
                        company["id"] = duplicate["id"]
                    else:
                        stored_normalized_name = normalized_name
                        company = source
                        company["id"] = incoming_id or f"company-{uuid4()}"

                try:
                    connection.execute(
                        """INSERT INTO companies (id,name,normalized_name,industry,company_description,
                    city,state,country,known_website,official_website,
                    website_discovery_method,website_candidate_urls,website_verification_notes,website_verified,
                    careers_page_url,job_board_url,job_board_discovery_method,jobs_rss_feed_url,job_platform,
                    feed_found,search_status,confidence,last_checked,notes,founded_year,total_assets,
                    assets_as_of_date,company_info_last_checked,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,normalized_name=excluded.normalized_name,
                    industry=excluded.industry,company_description=excluded.company_description,city=excluded.city,
                    state=excluded.state,country=excluded.country,known_website=excluded.known_website,
                    official_website=excluded.official_website,website_discovery_method=excluded.website_discovery_method,
                    website_candidate_urls=excluded.website_candidate_urls,website_verification_notes=excluded.website_verification_notes,
                    website_verified=excluded.website_verified,careers_page_url=excluded.careers_page_url,
                    job_board_url=excluded.job_board_url,job_board_discovery_method=excluded.job_board_discovery_method,
                    jobs_rss_feed_url=excluded.jobs_rss_feed_url,job_platform=excluded.job_platform,
                    feed_found=excluded.feed_found,search_status=excluded.search_status,confidence=excluded.confidence,
                    last_checked=excluded.last_checked,notes=excluded.notes,founded_year=excluded.founded_year,
                    total_assets=excluded.total_assets,assets_as_of_date=excluded.assets_as_of_date,
                    company_info_last_checked=excluded.company_info_last_checked,updated_at=excluded.updated_at""",
                        company_insert_values(company, now, normalized_name=stored_normalized_name),
                    )
                except sqlite3.IntegrityError as exc:
                    duplicate = find_company_by_normalized_name(
                        connection, normalized_name, exclude_id=company["id"],
                    )
                    if duplicate is not None:
                        raise duplicate_company_error(name, duplicate) from None
                    raise exc
        return len(prepared)

    def record_utility_run(self, utility_name: str, status: str, started_at: str, completed_at: str, payload: dict[str, Any]) -> str:
        run_id = f"utility-{uuid4()}"
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO utility_runs (id,utility_name,status,started_at,completed_at,payload_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (run_id, utility_name, status, started_at, completed_at, json.dumps(payload, default=str), utc_now()),
            )
        return run_id

    def replace_raw_candidates(self, candidates: Iterable[dict[str, Any]], *, company_ids: set[str] | None = None) -> int:
        prepared = []
        companies = self.list_companies()
        valid_ids = {company["id"] for company in companies}
        by_name = {company["name"].casefold(): company["id"] for company in companies}
        for index, source in enumerate(candidates):
            if not isinstance(source, dict):
                continue
            company_id = str(source.get("companyId") or "")
            if company_id not in valid_ids:
                company_id = by_name.get(str(source.get("companyName") or "").casefold(), "")
            basis = json.dumps(source, sort_keys=True, default=str)
            candidate_id = "candidate-" + hashlib.sha256(f"{index}:{basis}".encode()).hexdigest()[:32]
            prepared.append((candidate_id, company_id or None, source))
        now = utc_now()
        with self.connection() as connection:
            if company_ids is None:
                connection.execute("DELETE FROM raw_job_candidates")
            elif company_ids:
                placeholders = ",".join("?" for _ in company_ids)
                connection.execute(f"DELETE FROM raw_job_candidates WHERE company_id IN ({placeholders})", tuple(sorted(company_ids)))
            for candidate_id, company_id, source in prepared:
                connection.execute(
                    """INSERT INTO raw_job_candidates (id,company_id,job_id,company_name,candidate_text,
                    candidate_href,rejection_reason,payload_json,imported_at) VALUES (?,?,NULL,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET company_id=excluded.company_id,company_name=excluded.company_name,
                    candidate_text=excluded.candidate_text,candidate_href=excluded.candidate_href,
                    rejection_reason=excluded.rejection_reason,payload_json=excluded.payload_json,
                    imported_at=excluded.imported_at""",
                    (
                        candidate_id, company_id, str(source.get("companyName") or ""),
                        str(source.get("candidateText") or ""), str(source.get("candidateHref") or ""),
                        str(source.get("rejectionReason") or ""), json.dumps(source, sort_keys=True), now,
                    ),
                )
        return len(prepared)


def company_row_to_api(row: sqlite3.Row) -> dict[str, Any]:
    company = {
        "id": row["id"], "name": row["name"], "industry": row["industry"],
        "companyDescription": row["company_description"],
        "city": row["city"], "state": row["state"], "country": row["country"],
        "knownWebsite": row["known_website"], "officialWebsite": row["official_website"],
        "websiteDiscoveryMethod": row["website_discovery_method"],
        "websiteCandidateUrls": row["website_candidate_urls"],
        "websiteVerificationNotes": row["website_verification_notes"],
        "websiteVerified": bool(row["website_verified"]), "careersPageUrl": row["careers_page_url"],
        "jobBoardUrl": row["job_board_url"], "jobBoardDiscoveryMethod": row["job_board_discovery_method"],
        "jobsRssFeedUrl": row["jobs_rss_feed_url"], "jobPlatform": row["job_platform"],
        "feedFound": bool(row["feed_found"]), "searchStatus": row["search_status"],
        "confidence": row["confidence"], "lastChecked": row["last_checked"], "notes": row["notes"],
        "foundedYear": row["founded_year"], "totalAssets": row["total_assets"],
        "assetsAsOfDate": row["assets_as_of_date"],
        "companyInfoLastChecked": row["company_info_last_checked"],
    }
    if "active_job_count" in row.keys():
        company.update({
            "activeJobCount": row["active_job_count"],
            "jobCount": row["active_job_count"],
            "appliedCount": row["applied_count"],
            "lastCollectionDate": row["last_collection_date"],
        })
    return company


def distinct_values(connection: sqlite3.Connection, column: str) -> list[str]:
    allowed = {"state", "industry", "job_platform", "search_status"}
    if column not in allowed:
        raise ValueError(f"Unsupported company option column: {column}")
    return [
        row[0] for row in connection.execute(
            f"SELECT DISTINCT {column} FROM companies WHERE TRIM({column}) <> '' ORDER BY {column} COLLATE NOCASE"
        )
    ]


def company_api_to_excel(company: dict[str, Any]) -> dict[str, Any]:
    return {
        "Company ID": company["id"], "Company Name": company["name"], "Industry": company.get("industry", ""),
        "Company Description": company.get("companyDescription", ""),
        "City": company.get("city", ""), "State": company.get("state", ""), "Country": company.get("country", ""),
        "Known Website": company.get("knownWebsite", ""), "Official Website": company.get("officialWebsite", ""),
        "Website Discovery Method": company.get("websiteDiscoveryMethod", ""),
        "Website Candidate URLs": company.get("websiteCandidateUrls", ""),
        "Website Verification Notes": company.get("websiteVerificationNotes", ""),
        "Website Verified": company.get("websiteVerified", False), "Careers Page URL": company.get("careersPageUrl", ""),
        "Job Board URL": company.get("jobBoardUrl", ""), "Job Board Discovery Method": company.get("jobBoardDiscoveryMethod", ""),
        "Jobs RSS Feed URL": company.get("jobsRssFeedUrl", ""), "Job Platform": company.get("jobPlatform", ""),
        "Feed Found": company.get("feedFound", False), "Search Status": company.get("searchStatus", ""),
        "Confidence": company.get("confidence", 0), "Last Checked": company.get("lastChecked", ""), "Notes": company.get("notes", ""),
        "Founded Year": company.get("foundedYear") or "", "Total Assets": company.get("totalAssets") or "",
        "Assets As Of Date": company.get("assetsAsOfDate", ""),
        "Company Information Last Checked": company.get("companyInfoLastChecked", ""),
    }


def job_row_to_api(row: sqlite3.Row) -> dict[str, Any]:
    job = {
        "id": row["id"], "companyId": row["company_id"] or "", "companyName": row["company_name"],
        "title": row["title"], "location": row["location"], "workType": row["work_type"],
        "payMin": row["pay_min"], "payMax": row["pay_max"], "payText": row["pay_text"],
        "payPeriod": row["pay_period"], "payCurrency": row["pay_currency"], "postedDate": row["posted_date"],
        "sourceUrl": row["source_url"], "jobPlatform": row["job_platform"], "description": row["description"],
        "descriptionSnippet": row["description_snippet"], "collectedAt": row["collected_at"], "status": row["status"],
        "roleType": row["role_type"], "roleTypeReason": row["role_type_reason"],
        "rawData": json.loads(row["raw_data_json"] or "{}"),
    }
    if "active_resume_version" not in row.keys():
        return job
    active_version = str(row["active_resume_version"] or "")
    stored_status = str(row["match_status"] or "")
    stored_fingerprint = str(row["match_job_fingerprint"] or "")
    details = json.loads(row["match_payload_json"] or "{}")
    needs_rematch = bool(
        active_version and stored_status and (
            str(row["match_resume_version"] or "") != active_version
            or str(row["match_algorithm_version"] or "") != MATCH_ALGORITHM_VERSION
            or stored_fingerprint != job_match_fingerprint(job)
        )
    )
    if not active_version or not stored_status:
        match_status = "Not Matched"
    elif needs_rematch:
        match_status = "Needs Rematch"
    elif stored_status == "Failed":
        match_status = "Match Failed"
    else:
        match_status = "Matched"
    score = row["match_score"] if match_status == "Matched" else None
    job.update({
        "matchScore": round(float(score)) if score is not None else None,
        "matchStatus": match_status,
        "matchLabel": match_label(score) if score is not None else match_status,
        "matchedAt": str(row["match_matched_at"] or ""),
        "matchAlgorithmVersion": str(row["match_algorithm_version"] or ""),
        "matchDetails": details,
        "matchError": str(row["match_error"] or ""),
        "needsRematch": needs_rematch,
    })
    return job


def fit_result_to_api(row: sqlite3.Row) -> dict[str, Any]:
    score = row["score"]
    return {
        "id": row["id"], "resumeId": row["resume_id"], "jobId": row["job_id"],
        "score": round(float(score)) if score is not None else None,
        "status": row["status"], "resumeVersion": row["resume_version"],
        "jobFingerprint": row["job_fingerprint"], "algorithmVersion": row["algorithm_version"],
        "matchedAt": row["matched_at"], "error": row["error"],
        "details": json.loads(row["payload_json"] or "{}"),
    }


def match_label(score: float | None) -> str:
    if score is None:
        return "Not Matched"
    if score >= 80:
        return "Strong Match"
    if score >= 60:
        return "Good Match"
    if score >= 40:
        return "Moderate Match"
    return "Low Match"


def application_row_to_api(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "applied": bool(row["applied"]), "applicationStatus": row["application_status"],
        "dateApplied": row["date_applied"], "followUpDate": row["follow_up_date"],
        "notes": row["notes"], "notInterested": bool(row["not_interested"]),
    }


def insert_job(connection: sqlite3.Connection, job: dict[str, Any], now: str) -> None:
    connection.execute(
        """INSERT INTO jobs (id, legacy_id, company_id, company_name, title, location,
        work_type, pay_min, pay_max, pay_text, pay_period, pay_currency, posted_date,
        source_url, job_platform, description, description_snippet, collected_at, status,
        role_type, role_type_reason, raw_data_json, first_seen_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET legacy_id=excluded.legacy_id,company_id=excluded.company_id,
        company_name=excluded.company_name,title=excluded.title,location=excluded.location,
        work_type=excluded.work_type,pay_min=excluded.pay_min,pay_max=excluded.pay_max,
        pay_text=excluded.pay_text,pay_period=excluded.pay_period,pay_currency=excluded.pay_currency,
        posted_date=excluded.posted_date,source_url=excluded.source_url,job_platform=excluded.job_platform,
        description=excluded.description,description_snippet=excluded.description_snippet,
        collected_at=excluded.collected_at,status=excluded.status,role_type=excluded.role_type,
        role_type_reason=excluded.role_type_reason,raw_data_json=excluded.raw_data_json,
        updated_at=excluded.updated_at""",
        (
            job["id"], job.get("legacyId", job["id"]), job.get("companyId") or None,
            job.get("companyName", ""), job.get("title", ""), job.get("location", ""),
            job.get("workType", "Not Listed"), job.get("payMin"), job.get("payMax"), job.get("payText", ""),
            job.get("payPeriod", "unknown"), job.get("payCurrency", "USD"), job.get("postedDate", ""),
            job.get("sourceUrl", ""), job.get("jobPlatform", ""), job.get("description", ""),
            job.get("descriptionSnippet", ""), job.get("collectedAt", ""), job.get("status", "Open"),
            job.get("roleType", "UNKNOWN"), job.get("roleTypeReason", ""),
            json.dumps(job.get("rawData", {}), sort_keys=True), job.get("firstSeenAt") or now, now, now,
        ),
    )


def prune_missing_jobs(connection: sqlite3.Connection, keep_ids: set[str], *, company_ids: set[str] | None = None) -> None:
    clauses: list[str] = []
    parameters: list[Any] = []
    if company_ids is not None:
        if not company_ids:
            return
        placeholders = ",".join("?" for _ in company_ids)
        clauses.append(f"company_id IN ({placeholders})")
        parameters.extend(sorted(company_ids))
    if keep_ids:
        placeholders = ",".join("?" for _ in keep_ids)
        clauses.append(f"id NOT IN ({placeholders})")
        parameters.extend(sorted(keep_ids))
    scope = " AND ".join(clauses) if clauses else "1=1"
    connection.execute(
        f"UPDATE jobs SET status='Archived', updated_at=? WHERE {scope} AND EXISTS (SELECT 1 FROM applications WHERE applications.job_id=jobs.id)",
        (utc_now(), *parameters),
    )
    connection.execute(
        f"DELETE FROM jobs WHERE {scope} AND NOT EXISTS (SELECT 1 FROM applications WHERE applications.job_id=jobs.id)",
        parameters,
    )


def company_insert_values(
    company: dict[str, Any],
    now: str,
    *,
    normalized_name: str | None = None,
) -> tuple[Any, ...]:
    stored_normalized_name = (
        normalize_company_name(company.get("name"))
        if normalized_name is None else normalized_name
    )
    return (
        company["id"], company["name"], stored_normalized_name,
        company.get("industry", "Financial Services"), company.get("companyDescription", ""), company.get("city", ""),
        company.get("state", ""), company.get("country", "United States"), company.get("knownWebsite", ""),
        company.get("officialWebsite", ""), company.get("websiteDiscoveryMethod", ""),
        company.get("websiteCandidateUrls", ""), company.get("websiteVerificationNotes", ""),
        bool(company.get("websiteVerified")), company.get("careersPageUrl", ""), company.get("jobBoardUrl", ""),
        company.get("jobBoardDiscoveryMethod", "Not Found"), company.get("jobsRssFeedUrl", ""),
        company.get("jobPlatform", ""), bool(company.get("feedFound")), company.get("searchStatus", "Needs Review"),
        company.get("confidence", 0), company.get("lastChecked", ""), company.get("notes", ""),
        company.get("foundedYear"), company.get("totalAssets"), company.get("assetsAsOfDate", ""),
        company.get("companyInfoLastChecked", ""), now, now,
    )


COMPANY_SNAPSHOT_FIELDS = (
    "name", "industry", "companyDescription", "city", "state", "country",
    "knownWebsite", "officialWebsite", "websiteDiscoveryMethod", "websiteCandidateUrls",
    "websiteVerificationNotes", "websiteVerified", "careersPageUrl", "jobBoardUrl",
    "jobBoardDiscoveryMethod", "jobsRssFeedUrl", "jobPlatform", "feedFound",
    "searchStatus", "confidence", "lastChecked", "notes", "foundedYear", "totalAssets",
    "assetsAsOfDate", "companyInfoLastChecked",
)


def find_company_by_normalized_name(
    connection: sqlite3.Connection,
    normalized_name: str,
    *,
    exclude_id: str | None = None,
) -> sqlite3.Row | None:
    """Find a duplicate even when it predates the normalized-name migration."""
    if not normalized_name:
        return None
    parameters: list[Any] = []
    where = ""
    if exclude_id is not None:
        where = "WHERE id <> ?"
        parameters.append(exclude_id)
    rows = connection.execute(
        f"""SELECT id, name, normalized_name, created_at FROM companies {where}
        ORDER BY CASE WHEN normalized_name <> '' THEN 0 ELSE 1 END, created_at, id""",
        parameters,
    ).fetchall()
    return next(
        (row for row in rows if normalize_company_name(row["name"]) == normalized_name),
        None,
    )


def duplicate_company_error(name: str, row: sqlite3.Row) -> DuplicateCompanyError:
    return DuplicateCompanyError(name, str(row["id"]), str(row["name"]))


def normalized_name_for_existing_company(
    row: sqlite3.Row,
    normalized_name: str,
    duplicate: sqlite3.Row | None,
) -> str:
    if str(row["normalized_name"] or "") == normalized_name:
        return normalized_name
    # A preserved legacy duplicate must remain outside the unique partial index.
    return "" if duplicate is not None else normalized_name


def merge_company_snapshot(
    current: dict[str, Any],
    incoming: dict[str, Any],
    *,
    prefer_incoming: bool,
) -> dict[str, Any]:
    """Merge snapshots without allowing blanks to erase confirmed stored values.

    Same-ID snapshots retain the established refresh behavior for nonblank values.
    A different ID with the same normalized name is treated as a duplicate import:
    the stored record wins and only its missing fields are filled.
    """
    merged = dict(current)
    for key in COMPANY_SNAPSHOT_FIELDS:
        value = incoming.get(key)
        if not has_company_value(value):
            continue
        if prefer_incoming or not has_company_value(merged.get(key)):
            merged[key] = value.strip() if isinstance(value, str) else value
            continue
        if key in {"websiteVerified", "feedFound"}:
            merged[key] = bool(merged.get(key)) or bool(value)
        elif key == "confidence":
            merged[key] = max(int(merged.get(key) or 0), int(value or 0))
    return merged


def has_company_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
