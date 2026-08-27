from __future__ import annotations

from typing import Any

from backend.exports import SnapshotExporter
from backend.repository import OpportunityRepository


class CompanyService:
    """Preserves the Company CRUD contract while delegating state to SQLite."""

    def __init__(self, repository: OpportunityRepository, exporter: SnapshotExporter | None = None) -> None:
        self.repository = repository
        self.exporter = exporter

    def add_company(self, payload: dict[str, str]) -> dict[str, Any]:
        company = self.repository.create_company(payload)
        if self.exporter:
            self.exporter.export_companies()
        return company

    def edit_company(self, company_id: str, payload: dict[str, str]) -> dict[str, Any]:
        company = self.repository.update_company(company_id, payload)
        if self.exporter:
            self.exporter.export_companies()
        return company

    def delete_company(self, company_id: str) -> dict[str, Any]:
        result = self.repository.delete_company(company_id)
        if self.exporter:
            self.exporter.export_companies()
            self.exporter.export_jobs()
            self.exporter.export_applications()
        return result
