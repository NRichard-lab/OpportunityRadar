from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.repository import OpportunityRepository


SYNTHETIC_MARKER_KEY = "phase2.synthetic_fixture"


def create_synthetic_database(database_path: Path) -> dict[str, Any]:
    """Create a deterministic, non-sensitive database for persistence rehearsals."""
    database_path = Path(database_path).resolve()
    if database_path.exists():
        raise FileExistsError("Refusing to replace an existing database.")
    database_path.parent.mkdir(parents=True, exist_ok=True)

    repository = OpportunityRepository(database_path, initialize=True)
    companies = [
        {
            "id": "synthetic-company-northstar",
            "name": "Synthetic Northstar Cooperative",
            "industry": "Synthetic Financial Services",
            "city": "Example City",
            "state": "CO",
            "country": "United States",
            "knownWebsite": "https://northstar.example.invalid",
            "officialWebsite": "https://northstar.example.invalid",
            "websiteDiscoveryMethod": "Synthetic fixture",
            "websiteCandidateUrls": "",
            "websiteVerificationNotes": "Generated Phase 2 fixture; not a real company.",
            "websiteVerified": False,
            "careersPageUrl": "https://northstar.example.invalid/careers",
            "jobBoardUrl": "https://northstar.example.invalid/jobs",
            "jobBoardDiscoveryMethod": "Synthetic fixture",
            "jobsRssFeedUrl": "",
            "jobPlatform": "Synthetic",
            "feedFound": False,
            "searchStatus": "Synthetic",
            "confidence": 0,
            "lastChecked": "2026-01-01T00:00:00+00:00",
            "notes": "Synthetic test data only.",
        },
        {
            "id": "synthetic-company-cedar",
            "name": "Synthetic Cedar Finance Lab",
            "industry": "Synthetic Financial Technology",
            "city": "Sample Town",
            "state": "WA",
            "country": "United States",
            "knownWebsite": "https://cedar.example.invalid",
            "officialWebsite": "https://cedar.example.invalid",
            "websiteDiscoveryMethod": "Synthetic fixture",
            "websiteCandidateUrls": "",
            "websiteVerificationNotes": "Generated Phase 2 fixture; not a real company.",
            "websiteVerified": False,
            "careersPageUrl": "https://cedar.example.invalid/careers",
            "jobBoardUrl": "https://cedar.example.invalid/jobs",
            "jobBoardDiscoveryMethod": "Synthetic fixture",
            "jobsRssFeedUrl": "",
            "jobPlatform": "Synthetic",
            "feedFound": False,
            "searchStatus": "Synthetic",
            "confidence": 0,
            "lastChecked": "2026-01-01T00:00:00+00:00",
            "notes": "Synthetic test data only.",
        },
    ]
    repository.upsert_company_snapshots(companies)

    jobs = [
        {
            "id": "synthetic-job-analyst",
            "legacyId": "synthetic-job-analyst",
            "companyId": "synthetic-company-northstar",
            "companyName": "Synthetic Northstar Cooperative",
            "title": "Synthetic Operations Analyst",
            "location": "Example City, CO",
            "workType": "Hybrid",
            "payMin": 60000,
            "payMax": 75000,
            "payText": "$60,000-$75,000 synthetic range",
            "payPeriod": "year",
            "payCurrency": "USD",
            "postedDate": "2026-01-02",
            "sourceUrl": "https://northstar.example.invalid/jobs/analyst",
            "jobPlatform": "Synthetic",
            "description": "Synthetic role used only to verify container persistence.",
            "descriptionSnippet": "Synthetic role used only to verify container persistence.",
            "collectedAt": "2026-01-03T00:00:00+00:00",
            "status": "Open",
            "roleType": "IC",
            "roleTypeReason": "Synthetic fixture",
            "rawData": {"fixture": True},
            "firstSeenAt": "2026-01-03T00:00:00+00:00",
        },
        {
            "id": "synthetic-job-manager",
            "legacyId": "synthetic-job-manager",
            "companyId": "synthetic-company-cedar",
            "companyName": "Synthetic Cedar Finance Lab",
            "title": "Synthetic Program Manager",
            "location": "Sample Town, WA",
            "workType": "Remote",
            "payMin": 80000,
            "payMax": 95000,
            "payText": "$80,000-$95,000 synthetic range",
            "payPeriod": "year",
            "payCurrency": "USD",
            "postedDate": "2026-01-04",
            "sourceUrl": "https://cedar.example.invalid/jobs/manager",
            "jobPlatform": "Synthetic",
            "description": "Synthetic management role used only for restore validation.",
            "descriptionSnippet": "Synthetic management role used only for restore validation.",
            "collectedAt": "2026-01-05T00:00:00+00:00",
            "status": "Open",
            "roleType": "MGR",
            "roleTypeReason": "Synthetic fixture",
            "rawData": {"fixture": True},
            "firstSeenAt": "2026-01-05T00:00:00+00:00",
        },
    ]
    repository.upsert_jobs(jobs)
    repository.upsert_application(
        "synthetic-job-analyst",
        {
            "applied": True,
            "applicationStatus": "Applied",
            "dateApplied": "2026-01-06",
            "followUpDate": "2026-01-13",
            "notes": "Synthetic application state for backup and restore rehearsal.",
            "notInterested": False,
        },
    )
    repository.upsert_resume(
        {
            "id": "synthetic-resume-v1",
            "version": "synthetic-resume-v1",
            "name": "Synthetic Test Profile",
            "fileName": "synthetic-resume.pdf",
            "uploadedAt": "2026-01-01T00:00:00+00:00",
            "rawText": "Synthetic experience in operations, analysis, and program delivery.",
            "extractedText": "Synthetic experience in operations, analysis, and program delivery.",
            "skills": ["Synthetic analysis", "Synthetic operations"],
        }
    )
    with repository.connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO settings (key, value_json, updated_at) VALUES (?, ?, ?)",
            (SYNTHETIC_MARKER_KEY, "true", "2026-01-01T00:00:00+00:00"),
        )

    with closing(sqlite3.connect(database_path)) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in ("companies", "jobs", "applications", "resumes", "settings")
        }
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if integrity.lower() != "ok":
        raise RuntimeError("Synthetic database failed SQLite integrity validation.")
    return {
        "status": "created",
        "synthetic": True,
        "database": str(database_path),
        "schemaVersion": schema_version,
        "integrityCheck": integrity,
        "counts": counts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a new synthetic Opportunity Radar database for Phase 2 tests."
    )
    parser.add_argument("--database", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = create_synthetic_database(args.database)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
