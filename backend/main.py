from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.database import DATABASE_PATH, get_connection, initialize_database


class CompanyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    company_website: str = ""
    careers_page_url: str = ""
    job_board_url: str = ""
    industry: str = "Financial Services"
    city: str = ""
    state: str = ""
    country: str = "United States"
    notes: str = ""


class ApplicationUpsert(BaseModel):
    status: Literal["Interested", "Applied", "Interviewing", "Offer", "Rejected", "Withdrawn"] = "Interested"
    applied_date: str = ""
    notes: str = ""


app = FastAPI(title="Opportunity Radar API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    initialize_database()


def as_dict(row):
    return dict(row) if row else None


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "database": str(DATABASE_PATH)}


@app.get("/api/dashboard")
def dashboard() -> dict[str, object]:
    with get_connection() as conn:
        counts = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM companies) AS companies,
                (SELECT COUNT(*) FROM jobs WHERE status = 'Open') AS open_jobs,
                (SELECT COUNT(*) FROM applications) AS applications,
                (SELECT COUNT(*) FROM raw_job_candidates WHERE review_status = 'Pending Review') AS candidates_for_review
            """
        ).fetchone()
        recent_jobs = conn.execute(
            """
            SELECT jobs.*, companies.name AS company_name
            FROM jobs JOIN companies ON companies.id = jobs.company_id
            WHERE jobs.status = 'Open'
            ORDER BY jobs.updated_at DESC, jobs.id DESC LIMIT 5
            """
        ).fetchall()
    return {"summary": as_dict(counts), "recent_jobs": [as_dict(row) for row in recent_jobs]}


@app.get("/api/companies")
def list_companies(query: str = Query(default="")) -> list[dict[str, object]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT companies.*, COUNT(jobs.id) AS job_count
            FROM companies LEFT JOIN jobs ON jobs.company_id = companies.id
            WHERE companies.name LIKE ? OR companies.city LIKE ? OR companies.state LIKE ?
            GROUP BY companies.id ORDER BY companies.name COLLATE NOCASE
            """,
            (f"%{query}%", f"%{query}%", f"%{query}%"),
        ).fetchall()
    return [as_dict(row) for row in rows]


@app.post("/api/companies", status_code=201)
def create_company(company: CompanyCreate) -> dict[str, object]:
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO companies (
                name, company_website, careers_page_url, job_board_url, industry,
                city, state, country, notes, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*company.model_dump().values(), datetime.now(timezone.utc).isoformat()),
        )
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return as_dict(row)


@app.get("/api/jobs")
def list_jobs(query: str = Query(default="")) -> list[dict[str, object]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT jobs.*, companies.name AS company_name, applications.status AS application_status
            FROM jobs
            JOIN companies ON companies.id = jobs.company_id
            LEFT JOIN applications ON applications.job_id = jobs.id
            WHERE jobs.title LIKE ? OR companies.name LIKE ? OR jobs.location LIKE ?
            ORDER BY jobs.updated_at DESC, jobs.id DESC
            """,
            (f"%{query}%", f"%{query}%", f"%{query}%"),
        ).fetchall()
    return [as_dict(row) for row in rows]


@app.get("/api/applications")
def list_applications() -> list[dict[str, object]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT applications.*, jobs.title AS job_title, companies.name AS company_name
            FROM applications
            JOIN jobs ON jobs.id = applications.job_id
            JOIN companies ON companies.id = jobs.company_id
            ORDER BY applications.updated_at DESC
            """
        ).fetchall()
    return [as_dict(row) for row in rows]


@app.put("/api/jobs/{job_id}/application")
def upsert_application(job_id: int, application: ApplicationUpsert) -> dict[str, object]:
    with get_connection() as conn:
        if not conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Job not found")
        conn.execute(
            """
            INSERT INTO applications (job_id, status, applied_date, notes, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status,
                applied_date = excluded.applied_date,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (job_id, application.status, application.applied_date, application.notes, datetime.now(timezone.utc).isoformat()),
        )
        row = conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    return as_dict(row)
