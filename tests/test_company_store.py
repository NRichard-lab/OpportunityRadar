from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.db import connect, initialize_schema
from backend.exports import SnapshotExporter
from backend.repository import OpportunityRepository
from company_store import CompanyService


class SQLiteCompanyStoreTests(unittest.TestCase):
    def test_exporter_can_disable_frontend_public_mirrors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = OpportunityRepository(root / "opportunity_radar.db", initialize=True)
            exporter = SnapshotExporter(
                repository,
                master_path=root / "master.xlsx",
                companies_json_path=root / "private" / "companies.json",
                frontend_companies_json_path=root / "public" / "companies.json",
                jobs_json_path=root / "private" / "jobs.json",
                frontend_jobs_json_path=root / "public" / "jobs.json",
                applications_json_path=root / "private" / "applications.json",
                jobs_xlsx_path=root / "private" / "jobs.xlsx",
                write_frontend_mirrors=False,
            )

            exporter.export_all(include_excel=False)

            self.assertTrue((root / "private" / "companies.json").exists())
            self.assertTrue((root / "private" / "jobs.json").exists())
            self.assertFalse((root / "public" / "companies.json").exists())
            self.assertFalse((root / "public" / "jobs.json").exists())

    def test_add_edit_reload_export_and_delete_cascade(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = OpportunityRepository(root / "opportunity_radar.db", initialize=True)
            exporter = SnapshotExporter(
                repository,
                master_path=root / "master.xlsx",
                companies_json_path=root / "companies.json",
                frontend_companies_json_path=root / "frontend" / "companies.json",
                jobs_json_path=root / "jobs.json",
                frontend_jobs_json_path=root / "frontend" / "jobs.json",
                applications_json_path=root / "applications.json",
                jobs_xlsx_path=root / "jobs.xlsx",
                write_frontend_mirrors=True,
            )
            service = CompanyService(repository, exporter)

            created = service.add_company(company_payload(companyWebsite="https://test.example"))
            company_id = created["id"]
            self.assertEqual(OpportunityRepository(repository.database_path).get_company(company_id)["id"], company_id)

            repository.replace_jobs([
                {"id": "job-target", "companyId": company_id, "companyName": created["name"], "title": "Target", "status": "Open"},
                {"id": "job-keep", "companyId": company_id, "companyName": created["name"], "title": "Second", "status": "Open"},
            ])
            repository.upsert_application("job-target", {"applied": True, "applicationStatus": "Applied"})
            repository.replace_jobs([
                {"id": "job-keep", "companyId": company_id, "companyName": created["name"], "title": "Second Updated", "status": "Open"},
            ])
            self.assertEqual(repository.list_applications()["job-target"]["applicationStatus"], "Applied")
            self.assertEqual(next(job for job in repository.list_jobs() if job["id"] == "job-target")["status"], "Archived")
            with repository.connection() as connection:
                connection.execute(
                    "INSERT INTO raw_job_candidates (id,company_id,company_name,payload_json,imported_at) VALUES (?,?,?,'{}','now')",
                    ("candidate-target", company_id, created["name"]),
                )

            edited = service.edit_company(company_id, company_payload(
                name="Test Financial Renamed", companyWebsite="https://renamed.example",
                careersPageUrl="https://renamed.example/careers", jobBoardUrl="https://jobs.renamed.example",
            ))
            self.assertEqual(edited["id"], company_id)
            self.assertTrue(edited["jobBoardReverificationRequired"])
            self.assertEqual(edited["jobBoardDiscoveryMethod"], "Manual Re-verification Required")
            self.assertEqual(len(repository.list_jobs()), 2)
            self.assertEqual(json.loads((root / "companies.json").read_text())[0]["id"], company_id)

            result = service.delete_company(company_id)
            self.assertEqual(result["message"], "Company and related job data deleted.")
            self.assertEqual(result["deletedJobIds"], ["job-keep", "job-target"])
            self.assertEqual(repository.list_companies(), [])
            self.assertEqual(repository.list_jobs(), [])
            self.assertEqual(repository.list_applications(), {})
            with repository.connection(readonly=True) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM raw_job_candidates").fetchone()[0], 0)
            self.assertEqual(json.loads((root / "frontend" / "companies.json").read_text()), [])

    def test_company_query_filters_sorts_and_paginates_in_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = OpportunityRepository(Path(temp_dir) / "opportunity_radar.db", initialize=True)
            created = []
            for index in range(30):
                created.append(repository.create_company(company_payload(
                    name="Duplicate" if index < 2 else f"Company {index:02d}",
                    companyWebsite=f"https://company-{index}.example",
                    jobBoardUrl="https://jobs.example" if index % 3 == 0 else "",
                    industry="Banking" if index % 5 == 0 else "Financial Services",
                    city="Denver" if index % 2 == 0 else "Austin",
                    state="CO" if index % 2 == 0 else "TX",
                )))
            with repository.connection() as connection:
                connection.execute(
                    "UPDATE companies SET job_platform = 'Workday', search_status = 'Completed' WHERE id = ?",
                    (created[0]["id"],),
                )
            repository.replace_jobs([
                {"id": "job-a", "companyId": created[0]["id"], "companyName": created[0]["name"], "title": "A", "status": "Open", "collectedAt": "2026-08-23T10:00:00Z"},
                {"id": "job-b", "companyId": created[0]["id"], "companyName": created[0]["name"], "title": "B", "status": "Open", "collectedAt": "2026-08-23T11:00:00Z"},
                {"id": "job-c", "companyId": created[1]["id"], "companyName": created[1]["name"], "title": "C", "status": "Archived", "collectedAt": "2026-08-22T10:00:00Z"},
                {"id": "job-d", "companyId": created[2]["id"], "companyName": created[2]["name"], "title": "D", "status": "Open", "collectedAt": "2026-08-21T10:00:00Z"},
            ])

            first_page = repository.query_companies()
            second_page = repository.query_companies(page=2)
            self.assertEqual((first_page["total"], len(first_page["items"]), first_page["totalPages"]), (30, 25, 2))
            self.assertEqual(len(second_page["items"]), 5)
            duplicate_ids = [item["id"] for item in first_page["items"] if item["name"] == "Duplicate"]
            self.assertEqual(duplicate_ids, sorted(duplicate_ids, key=str.casefold))

            self.assertEqual(repository.query_companies(state="CO")["total"], 15)
            self.assertEqual(repository.query_companies(industry="Banking")["total"], 6)
            self.assertEqual(repository.query_companies(search="company-7.example")["total"], 1)
            self.assertEqual(repository.query_companies(job_board_type="Workday")["items"][0]["id"], created[0]["id"])
            self.assertEqual(repository.query_companies(discovery_status="Completed")["total"], 1)
            self.assertEqual(repository.query_companies(has_verified_job_board=True)["total"], 10)
            self.assertEqual(repository.query_companies(has_active_jobs=True)["total"], 2)
            self.assertEqual(repository.query_companies(has_active_jobs=False)["total"], 28)

            by_jobs = repository.query_companies(sort_by="jobCount", sort_direction="desc")
            self.assertEqual((by_jobs["items"][0]["id"], by_jobs["items"][0]["activeJobCount"]), (created[0]["id"], 2))
            by_collection = repository.query_companies(sort_by="lastCollectionDate", sort_direction="desc")
            self.assertEqual(by_collection["items"][0]["id"], created[0]["id"])
            self.assertEqual(repository.query_companies(page=99)["page"], 2)


def company_payload(**overrides: str) -> dict[str, str]:
    payload = {
        "name": "Test Financial", "companyWebsite": "", "careersPageUrl": "", "jobBoardUrl": "",
        "industry": "Financial Services", "city": "Denver", "state": "CO",
        "country": "United States", "notes": "",
    }
    payload.update(overrides)
    return payload


if __name__ == "__main__":
    unittest.main()
