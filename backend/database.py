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
                industry TEXT NOT NULL DEFAULT 'Financial Services',
                city TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT 'United States',
                search_status TEXT NOT NULL DEFAULT 'Not Started',
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
                detail_url TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                posted_date TEXT NOT NULL DEFAULT '',
                role_classification TEXT NOT NULL DEFAULT 'Unclassified',
                source_platform TEXT NOT NULL DEFAULT '',
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
                collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        existing = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        if existing:
            return
        conn.executemany(
            """
            INSERT INTO companies (
                name, company_website, careers_page_url, job_board_url, city, state,
                search_status, discovery_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("BECU", "https://www.becu.org", "", "", "Tukwila", "WA", "Needs Review", "Seed data"),
                ("WECU", "https://www.wecu.com", "", "", "Bellingham", "WA", "Needs Review", "Seed data"),
                ("Canvas Credit Union", "https://www.canvas.org", "", "", "Lone Tree", "CO", "Needs Review", "Seed data"),
            ],
        )
        becu_id = conn.execute("SELECT id FROM companies WHERE name = 'BECU'").fetchone()[0]
        conn.execute(
            """
            INSERT INTO jobs (company_id, title, location, department, employment_type,
                              role_classification, source_platform)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (becu_id, "Systems Administrator", "Remote / Washington", "Information Technology", "Full-time", "Infrastructure", "Sample"),
        )
