from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

from backend.file_security import EXPORT_WRITE_LOCK, atomic_write_text
from backend.repository import OpportunityRepository
from excel_tools import write_results
from job_tools import JobRecord, write_jobs_xlsx


class SnapshotExporter:
    """Writes compatibility snapshots; SQLite remains the authoritative store."""

    def __init__(
        self,
        repository: OpportunityRepository,
        *,
        master_path: Path,
        companies_json_path: Path,
        frontend_companies_json_path: Path,
        jobs_json_path: Path,
        frontend_jobs_json_path: Path,
        applications_json_path: Path,
        jobs_xlsx_path: Path,
        write_frontend_mirrors: bool = False,
    ) -> None:
        self.repository = repository
        self.master_path = Path(master_path)
        self.companies_json_path = Path(companies_json_path)
        self.frontend_companies_json_path = Path(frontend_companies_json_path)
        self.jobs_json_path = Path(jobs_json_path)
        self.frontend_jobs_json_path = Path(frontend_jobs_json_path)
        self.applications_json_path = Path(applications_json_path)
        self.jobs_xlsx_path = Path(jobs_xlsx_path)
        self.write_frontend_mirrors = write_frontend_mirrors

    def export_companies(self, *, include_excel: bool = True) -> int:
        with EXPORT_WRITE_LOCK:
            companies = self.repository.list_companies()
            _write_json(self.companies_json_path, companies)
            if self.write_frontend_mirrors:
                _write_json(self.frontend_companies_json_path, companies)
            if include_excel:
                write_results(self.master_path, self.repository.list_company_rows())
        return len(companies)

    def export_jobs(self, *, include_excel: bool = False) -> int:
        with EXPORT_WRITE_LOCK:
            jobs = self.repository.list_jobs()
            _write_json(self.jobs_json_path, jobs)
            if self.write_frontend_mirrors:
                _write_json(self.frontend_jobs_json_path, jobs)
            if include_excel:
                job_fields = {item.name for item in fields(JobRecord)}
                write_jobs_xlsx(self.jobs_xlsx_path, [JobRecord(**{key: value for key, value in job.items() if key in job_fields}) for job in jobs])
        return len(jobs)

    def export_applications(self) -> int:
        with EXPORT_WRITE_LOCK:
            applications = self.repository.list_applications()
            _write_json(self.applications_json_path, applications)
        return len(applications)

    def export_all(self, *, include_excel: bool = True) -> dict[str, int]:
        return {
            "companies": self.export_companies(include_excel=include_excel),
            "jobs": self.export_jobs(include_excel=include_excel),
            "applications": self.export_applications(),
        }


def _write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2))
