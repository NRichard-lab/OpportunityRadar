from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.database import DATABASE_PATH, get_connection, initialize_database
from backend.job_board_discovery import DiscoveryError, discover_job_board, gather_public_company_information
from backend.job_collection import CollectionError, approve_candidate, reprocess_saved_jobs, run_collection


class CompanyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    company_website: str = ""
    careers_page_url: str = ""
    verified_job_board_url: str = ""
    job_board_type: Literal["Workday", "ADP", "Greenhouse", "Lever", "ICIMS", "Paylocity", "UKG", "SaaS HR", "Dayforce", "Self-Hosted / In-House", "Other External ATS", ""] = ""
    classification_confidence: Literal["High", "Medium", "Low"] = "Low"
    industry: str = "Financial Services"
    city: str = ""
    state: str = ""
    country: str = "United States"
    founded_year: int | None = Field(default=None, ge=1600, le=2200)
    total_assets: float | None = Field(default=None, ge=0)
    total_assets_display: str = ""
    assets_as_of_date: str = ""
    information_source_note: str = ""
    location_discovery_source: str = ""
    location_confidence: Literal["Verified", "Needs Review", "Not Found"] = "Not Found"
    possible_locations: str = ""
    notes: str = ""
    discovery_method: str = ""


class CompanyUpdate(CompanyCreate):
    discovery_status: Literal["Not Started", "Discovering", "Verified", "Needs Review", "Failed"] = "Not Started"
    needs_manual_refresh: bool = False
    discovery_result: bool = False


class JobBoardDiscoveryRequest(BaseModel):
    company_website: str = ""
    careers_page_url: str = ""


class ApplicationUpsert(BaseModel):
    status: Literal["Interested", "Applied", "Interviewing", "Offer", "Rejected", "Withdrawn"] = "Interested"
    applied_date: str = ""
    notes: str = ""


class CollectionRequest(BaseModel):
    debug: bool = False


class ReprocessJobsRequest(BaseModel):
    job_id: int | None = None


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


def as_company_dict(row) -> dict[str, object] | None:
    value = as_dict(row)
    if value:
        for legacy_field in ("job_board_url", "search_status", "source_refresh_required"):
            value.pop(legacy_field, None)
    return value


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
            FROM companies LEFT JOIN jobs ON jobs.company_id = companies.id AND jobs.status <> 'No Longer Posted'
            WHERE companies.name LIKE ? OR companies.city LIKE ? OR companies.state LIKE ?
            GROUP BY companies.id ORDER BY companies.name COLLATE NOCASE
            """,
            (f"%{query}%", f"%{query}%", f"%{query}%"),
        ).fetchall()
    return [as_company_dict(row) for row in rows]


@app.post("/api/companies", status_code=201)
def create_company(company: CompanyCreate) -> dict[str, object]:
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO companies (
                name, company_website, careers_page_url, verified_job_board_url, industry,
                city, state, country, founded_year, total_assets, total_assets_display,
                assets_as_of_date, information_source_note, notes, discovery_method, updated_at
                , location_discovery_source, location_confidence, possible_locations,
                job_board_type, classification_confidence, discovery_status,
                needs_manual_refresh
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                company.name, company.company_website, company.careers_page_url,
                company.verified_job_board_url, company.industry, company.city, company.state,
                company.country, company.founded_year, company.total_assets,
                company.total_assets_display, company.assets_as_of_date,
                company.information_source_note, company.notes, company.discovery_method,
                datetime.now(timezone.utc).isoformat(), company.location_discovery_source,
                company.location_confidence, company.possible_locations,
                company.job_board_type, company.classification_confidence,
                "Not Started", 0,
            ),
        )
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return as_company_dict(row)


@app.put("/api/companies/{company_id}")
def update_company(company_id: int, company: CompanyUpdate) -> dict[str, object]:
    with get_connection() as conn:
        existing = conn.execute(
            """SELECT company_website, careers_page_url, verified_job_board_url,
                      needs_manual_refresh, last_verified_at
               FROM companies WHERE id = ?""",
            (company_id,),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Company not found")
        job_board_changed = existing["verified_job_board_url"] != company.verified_job_board_url
        website_fields_changed = any(
            existing[field] != getattr(company, field)
            for field in ("company_website", "careers_page_url", "verified_job_board_url")
        )
        discovery_status = company.discovery_status
        if website_fields_changed and not company.discovery_result:
            discovery_status = "Not Started"
        conn.execute(
            """
            UPDATE companies SET
                name = ?, company_website = ?, careers_page_url = ?, verified_job_board_url = ?,
                industry = ?, city = ?, state = ?, country = ?, notes = ?,
                founded_year = ?, total_assets = ?, total_assets_display = ?,
                assets_as_of_date = ?, information_source_note = ?, discovery_status = ?,
                discovery_method = ?, location_discovery_source = ?, location_confidence = ?,
                possible_locations = ?, job_board_type = ?, classification_confidence = ?,
                last_verified_at = ?, needs_manual_refresh = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                company.name,
                company.company_website,
                company.careers_page_url,
                company.verified_job_board_url,
                company.industry,
                company.city,
                company.state,
                company.country,
                company.notes,
                company.founded_year,
                company.total_assets,
                company.total_assets_display,
                company.assets_as_of_date,
                company.information_source_note,
                discovery_status,
                company.discovery_method,
                company.location_discovery_source,
                company.location_confidence,
                company.possible_locations,
                company.job_board_type,
                company.classification_confidence,
                datetime.now(timezone.utc).isoformat() if company.discovery_result else existing["last_verified_at"],
                int(False if company.discovery_result else (website_fields_changed or company.needs_manual_refresh or existing["needs_manual_refresh"])),
                datetime.now(timezone.utc).isoformat(),
                company_id,
            ),
        )
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    return as_company_dict(row)


@app.delete("/api/companies/{company_id}", status_code=204)
def delete_company(company_id: int) -> None:
    with get_connection() as conn:
        if not conn.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Company not found")
        conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))


@app.post("/api/companies/{company_id}/collect-jobs")
def collect_company_jobs(company_id: int, request: CollectionRequest) -> dict[str, object]:
    try:
        return run_collection(company_id, debug=request.debug)
    except CollectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/company-discovery/find-job-board")
def find_job_board(request: JobBoardDiscoveryRequest) -> dict[str, object]:
    try:
        result = discover_job_board(request.company_website, request.careers_page_url)
    except DiscoveryError as exc:
        status_code = 422 if exc.status == "Needs Review" else 502
        raise HTTPException(
            status_code=status_code,
            detail={
                "message": str(exc),
                "discovery_status": exc.status,
                "careers_page_url": exc.careers_page_url,
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not inspect the public careers path: {exc}") from exc
    return {
        "company_website": result.company_website,
        "careers_page_url": result.careers_page_url,
        "verified_job_board_url": result.job_board_url,
        "platform": result.platform,
        "job_board_type": result.job_board_type,
        "classification_confidence": result.classification_confidence,
        "discovery_method": result.discovery_method,
        "message": f"Verified {result.platform} job board found and ready to save.",
    }


@app.post("/api/companies/{company_id}/refresh-discovery")
def refresh_company_discovery(company_id: int) -> dict[str, object]:
    """Discover then persist the complete classification as one database update."""
    with get_connection() as conn:
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        if not company:
            raise HTTPException(status_code=404, detail="Company not found")
        company_data = dict(company)
        conn.execute(
            "UPDATE companies SET discovery_status = 'Discovering', updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), company_id),
        )
    try:
        result = discover_job_board(company_data["company_website"], company_data["careers_page_url"])
    except DiscoveryError as exc:
        status = exc.status if exc.status in {"Needs Review", "Failed"} else "Failed"
        with get_connection() as conn:
            conn.execute(
                """UPDATE companies SET careers_page_url = CASE WHEN ? <> '' THEN ? ELSE careers_page_url END,
                       verified_job_board_url = '', job_board_type = '', discovery_status = ?,
                       classification_confidence = 'Low', last_verified_at = '',
                       needs_manual_refresh = 0, updated_at = ? WHERE id = ?""",
                (exc.careers_page_url, exc.careers_page_url, status, datetime.now(timezone.utc).isoformat(), company_id),
            )
            row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        return {**as_company_dict(row), "message": str(exc)}
    verified_at = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """UPDATE companies SET company_website = ?, careers_page_url = ?,
                   verified_job_board_url = ?, job_board_type = ?, discovery_status = 'Verified',
                   classification_confidence = ?, discovery_method = ?, last_verified_at = ?,
                   needs_manual_refresh = 0, updated_at = ? WHERE id = ?""",
            (result.company_website or company_data["company_website"], result.careers_page_url,
             result.job_board_url, result.job_board_type, result.classification_confidence,
             result.discovery_method, verified_at, verified_at, company_id),
        )
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    return {**as_company_dict(row), "message": f"Verified {result.platform} job board found and saved."}


@app.post("/api/company-discovery/gather-information")
def gather_company_information(request: JobBoardDiscoveryRequest) -> dict[str, object]:
    try:
        return gather_public_company_information(request.company_website, request.careers_page_url)
    except DiscoveryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not review the public company pages: {exc}") from exc


@app.get("/api/jobs")
def list_jobs(query: str = Query(default="")) -> list[dict[str, object]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT jobs.*, companies.name AS company_name, applications.status AS application_status
            FROM jobs
            JOIN companies ON companies.id = jobs.company_id
            LEFT JOIN applications ON applications.job_id = jobs.id
            WHERE jobs.status <> 'No Longer Posted'
              AND (jobs.title LIKE ? OR companies.name LIKE ? OR jobs.location LIKE ?)
            ORDER BY jobs.updated_at DESC, jobs.id DESC
            """,
            (f"%{query}%", f"%{query}%", f"%{query}%"),
        ).fetchall()
    return [as_dict(row) for row in rows]


@app.post("/api/jobs/reprocess-details")
def reprocess_job_details(request: ReprocessJobsRequest) -> dict[str, object]:
    return reprocess_saved_jobs(request.job_id)


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


@app.get("/api/candidates")
def list_candidates() -> list[dict[str, object]]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT raw_job_candidates.*, companies.name AS company_name
               FROM raw_job_candidates
               JOIN companies ON companies.id = raw_job_candidates.company_id
               WHERE raw_job_candidates.review_status = 'Needs Review'
               ORDER BY raw_job_candidates.collected_at DESC, raw_job_candidates.id DESC"""
        ).fetchall()
    return [as_dict(row) for row in rows]


@app.post("/api/candidates/{candidate_id}/approve")
def approve_review_candidate(candidate_id: int) -> dict[str, object]:
    try:
        return approve_candidate(candidate_id)
    except CollectionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
