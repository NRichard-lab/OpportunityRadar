from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
DATABASE_PATH = DATA_DIR / "opportunity_radar.db"


def connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    conn = connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_database() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                company_website TEXT NOT NULL DEFAULT '',
                careers_page_url TEXT NOT NULL DEFAULT '',
                job_board_url TEXT NOT NULL DEFAULT '',
                verified_job_board_url TEXT NOT NULL DEFAULT '',
                job_board_type TEXT NOT NULL DEFAULT '',
                discovery_status TEXT NOT NULL DEFAULT 'Not Started',
                classification_confidence TEXT NOT NULL DEFAULT 'Low',
                last_verified_at TEXT NOT NULL DEFAULT '',
                needs_manual_refresh INTEGER NOT NULL DEFAULT 0,
                last_collection_status TEXT NOT NULL DEFAULT '',
                industry TEXT NOT NULL DEFAULT 'Financial Services',
                city TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT 'United States',
                founded_year INTEGER,
                total_assets REAL,
                total_assets_display TEXT NOT NULL DEFAULT '',
                assets_as_of_date TEXT NOT NULL DEFAULT '',
                information_source_note TEXT NOT NULL DEFAULT '',
                location_discovery_source TEXT NOT NULL DEFAULT '',
                location_confidence TEXT NOT NULL DEFAULT 'Not Found',
                possible_locations TEXT NOT NULL DEFAULT '',
                last_collector TEXT NOT NULL DEFAULT '',
                last_collection_at TEXT NOT NULL DEFAULT '',
                last_raw_count INTEGER NOT NULL DEFAULT 0,
                last_saved_count INTEGER NOT NULL DEFAULT 0,
                last_review_count INTEGER NOT NULL DEFAULT 0,
                last_collection_error TEXT NOT NULL DEFAULT '',
                search_status TEXT NOT NULL DEFAULT 'Not Started',
                source_refresh_required INTEGER NOT NULL DEFAULT 0,
                discovery_method TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
                department TEXT NOT NULL DEFAULT '',
                employment_type TEXT NOT NULL DEFAULT '',
                pay_text TEXT NOT NULL DEFAULT '',
                pay_min REAL,
                pay_max REAL,
                pay_currency TEXT NOT NULL DEFAULT '',
                pay_period TEXT NOT NULL DEFAULT '',
                pay_display TEXT NOT NULL DEFAULT '',
                target_pay_min REAL,
                target_pay_max REAL,
                full_pay_min REAL,
                full_pay_max REAL,
                incentives_text TEXT NOT NULL DEFAULT '',
                benefits_summary TEXT NOT NULL DEFAULT '',
                benefit_tags TEXT NOT NULL DEFAULT '[]',
                compensation_source_text TEXT NOT NULL DEFAULT '',
                benefits_source_text TEXT NOT NULL DEFAULT '',
                has_health_insurance INTEGER NOT NULL DEFAULT 0,
                has_dental_insurance INTEGER NOT NULL DEFAULT 0,
                has_vision_insurance INTEGER NOT NULL DEFAULT 0,
                has_retirement INTEGER NOT NULL DEFAULT 0,
                retirement_details TEXT NOT NULL DEFAULT '',
                retirement_match_percent REAL,
                retirement_contribution_percent REAL,
                has_pto INTEGER NOT NULL DEFAULT 0,
                pto_details TEXT NOT NULL DEFAULT '',
                has_tuition_reimbursement INTEGER NOT NULL DEFAULT 0,
                tuition_details TEXT NOT NULL DEFAULT '',
                has_volunteer_time_off INTEGER NOT NULL DEFAULT 0,
                has_donation_match INTEGER NOT NULL DEFAULT 0,
                has_remote_hybrid INTEGER NOT NULL DEFAULT 0,
                other_benefit_details TEXT NOT NULL DEFAULT '',
                detail_url TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                posted_date TEXT NOT NULL DEFAULT '',
                role_classification TEXT NOT NULL DEFAULT 'Unclassified',
                source_platform TEXT NOT NULL DEFAULT '',
                external_job_id TEXT NOT NULL DEFAULT '',
                dedupe_key TEXT NOT NULL DEFAULT '',
                validation_source TEXT NOT NULL DEFAULT 'deterministic',
                status TEXT NOT NULL DEFAULT 'Open',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY,
                job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'Interested',
                applied_date TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS raw_job_candidates (
                id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                source_url TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                review_status TEXT NOT NULL DEFAULT 'Pending Review',
                rejection_reason TEXT NOT NULL DEFAULT '',
                external_job_id TEXT NOT NULL DEFAULT '',
                detail_url TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                dedupe_key TEXT NOT NULL DEFAULT '',
                collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS job_collection_runs (
                id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                collector TEXT NOT NULL,
                job_board_url TEXT NOT NULL,
                browser_used INTEGER NOT NULL DEFAULT 0,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                rejected_count INTEGER NOT NULL DEFAULT 0,
                saved_count INTEGER NOT NULL DEFAULT 0,
                duration_seconds REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'Running',
                error TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT NOT NULL DEFAULT ''
            );
            """
        )
        company_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(companies)").fetchall()
        }
        if "source_refresh_required" not in company_columns:
            conn.execute(
                "ALTER TABLE companies ADD COLUMN source_refresh_required INTEGER NOT NULL DEFAULT 0"
            )
        company_migrations = {
            "verified_job_board_url": "TEXT NOT NULL DEFAULT ''",
            "job_board_type": "TEXT NOT NULL DEFAULT ''",
            "discovery_status": "TEXT NOT NULL DEFAULT 'Not Started'",
            "classification_confidence": "TEXT NOT NULL DEFAULT 'Low'",
            "last_verified_at": "TEXT NOT NULL DEFAULT ''",
            "needs_manual_refresh": "INTEGER NOT NULL DEFAULT 0",
            "last_collection_status": "TEXT NOT NULL DEFAULT ''",
            "founded_year": "INTEGER",
            "total_assets": "REAL",
            "total_assets_display": "TEXT NOT NULL DEFAULT ''",
            "assets_as_of_date": "TEXT NOT NULL DEFAULT ''",
            "information_source_note": "TEXT NOT NULL DEFAULT ''",
            "location_discovery_source": "TEXT NOT NULL DEFAULT ''",
            "location_confidence": "TEXT NOT NULL DEFAULT 'Not Found'",
            "possible_locations": "TEXT NOT NULL DEFAULT ''",
            "last_collector": "TEXT NOT NULL DEFAULT ''",
            "last_collection_at": "TEXT NOT NULL DEFAULT ''",
            "last_raw_count": "INTEGER NOT NULL DEFAULT 0",
            "last_saved_count": "INTEGER NOT NULL DEFAULT 0",
            "last_review_count": "INTEGER NOT NULL DEFAULT 0",
            "last_collection_error": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in company_migrations.items():
            if column not in company_columns:
                conn.execute(f"ALTER TABLE companies ADD COLUMN {column} {definition}")
        # One-way compatibility migration from prototype columns. Runtime code
        # uses only the canonical fields above so statuses and URLs cannot alias.
        if "verified_job_board_url" not in company_columns:
            conn.execute(
                """UPDATE companies SET verified_job_board_url = job_board_url
                   WHERE verified_job_board_url = '' AND job_board_url <> ''"""
            )
        if "discovery_status" not in company_columns:
            conn.execute(
                """UPDATE companies SET discovery_status = CASE
                   WHEN search_status IN ('Not Started','Discovering','Verified','Needs Review','Failed') THEN search_status
                   ELSE 'Needs Review' END
                   WHERE discovery_status = 'Not Started' AND search_status <> 'Not Started'"""
            )
        if "needs_manual_refresh" not in company_columns:
            conn.execute(
                """UPDATE companies SET needs_manual_refresh = source_refresh_required
                   WHERE needs_manual_refresh = 0 AND source_refresh_required <> 0"""
            )
        conn.execute(
            """UPDATE companies SET classification_confidence = CASE
                   WHEN classification_confidence = 'Verified' THEN 'High'
                   WHEN classification_confidence = 'Needs Review' THEN 'Low'
                   WHEN classification_confidence IN ('High','Medium','Low') THEN classification_confidence
                   ELSE 'Low' END"""
        )
        conn.execute(
            """UPDATE companies SET job_board_type = CASE
                   WHEN lower(verified_job_board_url) LIKE '%myworkdayjobs.com%' THEN 'Workday'
                   WHEN lower(verified_job_board_url) LIKE '%adp.com%' THEN 'ADP'
                   WHEN lower(verified_job_board_url) LIKE '%greenhouse.io%' THEN 'Greenhouse'
                   WHEN lower(verified_job_board_url) LIKE '%lever.co%' THEN 'Lever'
                   WHEN lower(verified_job_board_url) LIKE '%icims.com%' THEN 'ICIMS'
                   WHEN lower(verified_job_board_url) LIKE '%paylocity.com%' THEN 'Paylocity'
                   WHEN lower(verified_job_board_url) LIKE '%saashr.com%' THEN 'SaaS HR'
                   WHEN lower(verified_job_board_url) LIKE '%dayforcehcm.com%' THEN 'Dayforce'
                   ELSE job_board_type END
               WHERE job_board_type = 'Needs Review'"""
        )
        if "last_collection_status" not in company_columns:
            conn.execute(
                """UPDATE companies SET last_collection_status = CASE
                   WHEN last_collection_error <> '' THEN 'Failed'
                   WHEN last_collection_at <> '' THEN 'Completed'
                   ELSE '' END
                   WHERE last_collection_status = ''"""
            )
        # Repair legacy verified rows once. Their old confidence/status columns
        # carried different meanings; a recognized saved board is high confidence.
        conn.execute(
            """UPDATE companies SET classification_confidence = 'High',
                   last_verified_at = updated_at, needs_manual_refresh = 0
               WHERE discovery_status = 'Verified' AND verified_job_board_url <> ''
                 AND job_board_type IN ('Workday','ADP','Greenhouse','Lever','ICIMS','Paylocity','UKG','SaaS HR','Dayforce','Self-Hosted / In-House')
                 AND last_verified_at = ''"""
        )
        table_migrations = {
            "jobs": {
                "external_job_id": "TEXT NOT NULL DEFAULT ''",
                "dedupe_key": "TEXT NOT NULL DEFAULT ''",
                "validation_source": "TEXT NOT NULL DEFAULT 'deterministic'",
                "pay_min": "REAL",
                "pay_max": "REAL",
                "pay_currency": "TEXT NOT NULL DEFAULT ''",
                "pay_period": "TEXT NOT NULL DEFAULT ''",
                "pay_display": "TEXT NOT NULL DEFAULT ''",
                "target_pay_min": "REAL",
                "target_pay_max": "REAL",
                "full_pay_min": "REAL",
                "full_pay_max": "REAL",
                "incentives_text": "TEXT NOT NULL DEFAULT ''",
                "benefits_summary": "TEXT NOT NULL DEFAULT ''",
                "benefit_tags": "TEXT NOT NULL DEFAULT '[]'",
                "compensation_source_text": "TEXT NOT NULL DEFAULT ''",
                "benefits_source_text": "TEXT NOT NULL DEFAULT ''",
                "has_health_insurance": "INTEGER NOT NULL DEFAULT 0",
                "has_dental_insurance": "INTEGER NOT NULL DEFAULT 0",
                "has_vision_insurance": "INTEGER NOT NULL DEFAULT 0",
                "has_retirement": "INTEGER NOT NULL DEFAULT 0",
                "retirement_details": "TEXT NOT NULL DEFAULT ''",
                "retirement_match_percent": "REAL",
                "retirement_contribution_percent": "REAL",
                "has_pto": "INTEGER NOT NULL DEFAULT 0",
                "pto_details": "TEXT NOT NULL DEFAULT ''",
                "has_tuition_reimbursement": "INTEGER NOT NULL DEFAULT 0",
                "tuition_details": "TEXT NOT NULL DEFAULT ''",
                "has_volunteer_time_off": "INTEGER NOT NULL DEFAULT 0",
                "has_donation_match": "INTEGER NOT NULL DEFAULT 0",
                "has_remote_hybrid": "INTEGER NOT NULL DEFAULT 0",
                "other_benefit_details": "TEXT NOT NULL DEFAULT ''",
            },
            "raw_job_candidates": {
                "rejection_reason": "TEXT NOT NULL DEFAULT ''",
                "external_job_id": "TEXT NOT NULL DEFAULT ''",
                "detail_url": "TEXT NOT NULL DEFAULT ''",
                "title": "TEXT NOT NULL DEFAULT ''",
                "location": "TEXT NOT NULL DEFAULT ''",
                "dedupe_key": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, columns in table_migrations.items():
            existing_columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing_columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        # Early prototypes included a visible job-list placeholder.  It has no
        # public posting URL and must never be presented as a live employer role.
        conn.execute(
            "DELETE FROM jobs WHERE source_platform = 'Sample' AND detail_url = ''"
        )
        existing = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        if existing:
            return
        conn.executemany(
            """
            INSERT INTO companies (
                name, company_website, careers_page_url, verified_job_board_url, city, state,
                discovery_status, discovery_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("BECU", "https://www.becu.org", "", "", "Tukwila", "WA", "Needs Review", "Seed data"),
                ("WECU", "https://www.wecu.com", "", "", "Bellingham", "WA", "Needs Review", "Seed data"),
                ("Canvas Credit Union", "https://www.canvas.org", "", "", "Lone Tree", "CO", "Needs Review", "Seed data"),
            ],
        )
