from __future__ import annotations

import io
import json
import logging
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, SecretStr

from backend.exports import SnapshotExporter
from backend.blueash_auth import (
    BlueAshAuthenticationError,
    BlueAshAuthorizationError,
    BlueAshAuthClient,
    BlueAshConfigurationError,
    BlueAshIdentity,
    BlueAshUnavailableError,
    blueash_auth_enabled,
    is_administrator,
    is_trusted_initial_administrator,
    login_url,
    validate_auth_configuration,
)
from backend.email_service import EmailConfigurationError, EmailDeliveryError, EmailService
from backend.migration import excel_company_to_api
from backend.maintenance_scheduler import DEFAULT_TIMEZONE, MaintenanceScheduler
from backend.repository import OpportunityRepository
from backend.resume_files import build_resume_profile
from backend.resume_matching import ResumeMatchService
from backend.utility_runs import UtilityRunManager
from backend.utility_tasks import (
    create_backup,
    export_data,
    import_data_file,
    refresh_company_discovery,
    refresh_missing_company_information,
    refresh_single_company_information,
    reprocess_saved_jobs,
)
from company_store import CompanyService
from config import (
    APP_ENABLE_BROWSER_JOBS,
    APP_ENABLE_COMPANY_REFRESH,
    APP_ENABLE_DISCOVERY,
    APP_ENABLE_SCHEDULES,
    APP_ENABLE_UTILITIES,
    APP_ENV,
    APP_PUBLIC_ORIGIN,
    APP_WRITE_FRONTEND_MIRRORS,
    BACKUP_DIR,
    BLUEASH_COOKIE_DOMAIN,
    BLUEASH_SESSION_COOKIE,
    DEFAULT_APPLICATIONS_JSON,
    DEFAULT_DATABASE,
    DEFAULT_FRONTEND_COMPANIES_JSON,
    DEFAULT_FRONTEND_JOBS_JSON,
    DEFAULT_JOBS_JSON,
    DEFAULT_JOBS_XLSX,
    DEFAULT_JSON_OUTPUT,
    DEFAULT_MASTER,
    IMPORT_DIR,
    LOG_DIR,
    feature_flags_payload,
)
from excel_tools import read_company_rows
from job_tools import collect_jobs
from main import configure_logging, fill_missing_job_boards
from website_audit import audit_websites, repair_websites


app = FastAPI(
    title="Opportunity Radar Backend",
    docs_url=None if APP_ENV == "production" else "/docs",
    redoc_url=None if APP_ENV == "production" else "/redoc",
    openapi_url=None if APP_ENV == "production" else "/openapi.json",
)
company_refresh_lock = Lock()
_utility_run_manager: UtilityRunManager | None = None
_maintenance_scheduler: MaintenanceScheduler | None = None
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(filter(None, [APP_PUBLIC_ORIGIN, "http://127.0.0.1:5173", "http://localhost:5173"])),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


def require_administrator(request: Request) -> BlueAshIdentity:
    identity = getattr(request.state, "identity", None)
    if not isinstance(identity, BlueAshIdentity):
        raise HTTPException(status_code=401, detail="Authentication is required.")
    allowed = is_trusted_initial_administrator(identity) if APP_ENV == "production" else is_administrator(identity)
    if not allowed:
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    return identity


def require_utilities_enabled() -> None:
    _require_feature(APP_ENABLE_UTILITIES, "Utilities")


def require_schedules_enabled() -> None:
    _require_feature(APP_ENABLE_SCHEDULES, "Schedules")


def require_discovery_enabled() -> None:
    _require_feature(APP_ENABLE_DISCOVERY, "Company discovery")


def require_browser_jobs_enabled() -> None:
    _require_feature(APP_ENABLE_BROWSER_JOBS, "Browser job collection")


def require_company_refresh_enabled() -> None:
    _require_feature(APP_ENABLE_COMPANY_REFRESH, "Company refresh")


def _require_feature(enabled: bool, label: str) -> None:
    if not enabled:
        raise HTTPException(status_code=403, detail=f"{label} is disabled for this release.")


def _require_action_features(action: str) -> None:
    reason = _disabled_action_reason(action)
    if reason:
        raise HTTPException(status_code=403, detail=reason)


def _disabled_action_reason(action: str) -> str:
    requirements: dict[str, tuple[tuple[bool, str], ...]] = {
        "refresh-missing-company-information": (
            (APP_ENABLE_COMPANY_REFRESH, "Company refresh"),
            (APP_ENABLE_DISCOVERY, "Company discovery"),
        ),
        "refresh-company-discovery": (
            (APP_ENABLE_COMPANY_REFRESH, "Company refresh"),
            (APP_ENABLE_DISCOVERY, "Company discovery"),
        ),
        "refresh-all-job-listings": ((APP_ENABLE_BROWSER_JOBS, "Browser job collection"),),
    }
    for enabled, label in requirements.get(action, ()):
        if not enabled:
            return f"{label} is disabled for this release."
    return ""


def _guarded_utility_worker(action: str, worker: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    def guarded(progress: Callable[..., None], cancelled: Any) -> dict[str, Any]:
        try:
            _require_action_features(action)
        except HTTPException as exc:
            raise RuntimeError(str(exc.detail)) from None
        return worker(progress, cancelled)

    return guarded


def utility_runs() -> UtilityRunManager:
    global _utility_run_manager
    if _utility_run_manager is None:
        validate_auth_configuration()
        _utility_run_manager = UtilityRunManager(Path(DEFAULT_DATABASE))
    return _utility_run_manager


def scheduler() -> MaintenanceScheduler:
    global _maintenance_scheduler
    if _maintenance_scheduler is None:
        _maintenance_scheduler = MaintenanceScheduler(
            Path(DEFAULT_DATABASE), utility_runs(), user_utility_definitions
        )
    return _maintenance_scheduler


@app.middleware("http")
async def production_security(request: Request, call_next: Callable[..., Any]) -> Response:
    path = request.url.path
    if path.startswith("/api") and not path.startswith("/api/auth/") and path != "/api/health":
        try:
            identity = await run_in_threadpool(
                BlueAshAuthClient().authenticate,
                request.cookies.get(BLUEASH_SESSION_COOKIE, ""),
            )
        except BlueAshAuthenticationError as exc:
            return JSONResponse({"detail": str(exc), "loginUrl": login_url()}, status_code=401)
        except BlueAshAuthorizationError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=403)
        except BlueAshUnavailableError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        except BlueAshConfigurationError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        if APP_ENV == "production" and not is_trusted_initial_administrator(identity):
            return JSONResponse({"detail": "Opportunity Radar is restricted to the trusted administrator."}, status_code=403)
        request.state.identity = identity
        request.state.user = identity.as_dict()
    if blueash_auth_enabled() and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin", "").rstrip("/")
        if origin and origin != APP_PUBLIC_ORIGIN:
            return JSONResponse({"detail": "Request origin is not allowed."}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    if APP_ENV == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


class UtilityRequest(BaseModel):
    limit: int = Field(default=10, ge=0)
    limitCompanies: int = Field(default=25, ge=0)
    company: str | None = None
    maxWorkers: int = Field(default=10, ge=1)
    browserWorkers: int = Field(default=3, ge=1)
    delaySeconds: float = Field(default=1.0, ge=0)
    dryRun: bool = False
    debug: bool = False
    useBrowserDiscovery: bool = False
    force: bool = False
    allowLowConfidence: bool = False


class CompanyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    companyWebsite: str = ""
    careersPageUrl: str = ""
    jobBoardUrl: str = ""
    industry: str = "Financial Services"
    city: str = ""
    state: str = ""
    country: str = "United States"
    notes: str = ""


class ApplicationPatch(BaseModel):
    applied: bool | None = None
    applicationStatus: str | None = None
    dateApplied: str | None = None
    followUpDate: str | None = None
    notes: str | None = None
    notInterested: bool | None = None


class BrowserOverridesRequest(BaseModel):
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


class MaintenanceScheduleRequest(BaseModel):
    enabled: bool
    runTime: str = Field(min_length=5, max_length=5)
    timezone: str = Field(default=DEFAULT_TIMEZONE, min_length=1, max_length=100)


class EmailSettingsRequest(BaseModel):
    smtpHost: str = Field(default="", max_length=255)
    smtpPort: int = Field(default=465, ge=1, le=65535)
    security: Literal["ssl_tls", "starttls", "none"] = "ssl_tls"
    smtpUsername: str = Field(default="", max_length=320)
    smtpPassword: SecretStr = Field(default_factory=lambda: SecretStr(""), max_length=1000)
    fromEmail: str = Field(default="", max_length=320)
    fromName: str = Field(default="Opportunity Radar", max_length=200)
    replyToEmail: str = Field(default="", max_length=320)
    dailyEnabled: bool = False
    recipientEmail: str = Field(default="", max_length=320)
    sendAfterRefresh: bool = True
    sendWhenEmpty: bool = False


class TestEmailRequest(BaseModel):
    recipient: str = Field(min_length=3, max_length=320)


@app.get("/api/status")
def status() -> dict[str, str]:
    return {
        "status": "ready" if Path(DEFAULT_DATABASE).exists() else "migration-required",
        "storage": "sqlite" if Path(DEFAULT_DATABASE).exists() else "legacy-files",
        "message": "Opportunity Radar backend is running.",
    }


@app.get("/api/health")
def health_endpoint() -> dict[str, str]:
    try:
        with repository().connection(readonly=True) as connection:
            connection.execute("SELECT 1").fetchone()
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable.") from None
    return {"status": "healthy"}


@app.get("/api/auth/session")
def auth_session_endpoint(
    request: Request,
    return_to: str = Query(default="", alias="returnTo"),
) -> dict[str, Any]:
    try:
        identity = BlueAshAuthClient().authenticate(request.cookies.get(BLUEASH_SESSION_COOKIE, ""))
        if APP_ENV == "production" and not is_trusted_initial_administrator(identity):
            raise HTTPException(status_code=403, detail="Opportunity Radar is restricted to the trusted administrator.")
        return {**identity.as_dict(), "features": feature_flags_payload()}
    except BlueAshAuthenticationError as exc:
        raise HTTPException(status_code=401, detail={"message": str(exc), "loginUrl": login_url(return_to)}) from None
    except BlueAshAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except BlueAshUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except BlueAshConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.post("/api/auth/logout")
def auth_logout_endpoint(request: Request, response: Response) -> dict[str, str]:
    try:
        BlueAshAuthClient().logout(request.cookies.get(BLUEASH_SESSION_COOKIE, ""))
    except BlueAshUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except BlueAshConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    response.delete_cookie(
        BLUEASH_SESSION_COOKIE, path="/", domain=BLUEASH_COOKIE_DOMAIN or None,
        secure=APP_ENV == "production", httponly=True, samesite="lax",
    )
    return {"message": "Signed out of Blue Ash.", "redirectUrl": login_url()}


@app.get("/api/companies")
def list_companies_endpoint() -> list[dict[str, Any]]:
    return repository().list_companies()


@app.get("/api/companies-page")
def query_companies_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, alias="pageSize"),
    search: str = "",
    state: str = "",
    industry: str = "",
    job_board_type: str = Query(default="", alias="jobBoardType"),
    discovery_status: str = Query(default="", alias="discoveryStatus"),
    has_verified_job_board: bool | None = Query(default=None, alias="hasVerifiedJobBoard"),
    has_active_jobs: bool | None = Query(default=None, alias="hasActiveJobs"),
    sort_by: Literal[
        "companyName", "city", "state", "jobBoardType", "discoveryStatus",
        "jobCount", "lastCollectionDate",
    ] = Query(default="companyName", alias="sortBy"),
    sort_direction: Literal["asc", "desc"] = Query(default="asc", alias="sortDirection"),
) -> dict[str, Any]:
    return repository().query_companies(
        page=page,
        page_size=page_size,
        search=search,
        state=state,
        industry=industry,
        job_board_type=job_board_type,
        discovery_status=discovery_status,
        has_verified_job_board=has_verified_job_board,
        has_active_jobs=has_active_jobs,
        sort_by=sort_by,
        sort_direction=sort_direction,
    )


@app.get("/api/jobs")
def list_jobs_endpoint() -> list[dict[str, Any]]:
    return repository().list_jobs()


@app.get("/api/applications", dependencies=[Depends(require_administrator)])
def list_applications_endpoint() -> dict[str, dict[str, Any]]:
    return repository().list_applications()


@app.put("/api/applications/{job_id}", dependencies=[Depends(require_administrator)])
def update_application_endpoint(job_id: str, request: ApplicationPatch) -> dict[str, Any]:
    try:
        application = repository().upsert_application(job_id, request.model_dump(exclude_none=True))
    except KeyError:
        raise HTTPException(status_code=404, detail="Job not found.") from None
    exporter().export_applications()
    return {"message": "Application tracking updated.", "application": application}


@app.post("/api/applications/import-browser-overrides", dependencies=[Depends(require_administrator)])
def import_browser_overrides_endpoint(request: BrowserOverridesRequest) -> dict[str, Any]:
    result = repository().import_application_overrides(request.overrides)
    exporter().export_applications()
    return result


@app.get("/api/resume", dependencies=[Depends(require_administrator)])
def get_resume_endpoint() -> dict[str, Any] | None:
    return repository().get_resume()


@app.put("/api/resume", dependencies=[Depends(require_administrator)])
def update_resume_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    return repository().upsert_resume(payload)


@app.post("/api/resume/upload", dependencies=[Depends(require_administrator)])
async def upload_resume_endpoint(request: Request, filename: str = Query(min_length=1, max_length=255)) -> dict[str, Any]:
    contents = await request.body()
    if not contents:
        raise HTTPException(status_code=422, detail="The selected resume is empty.")
    try:
        profile = build_resume_profile(Path(filename).name, contents)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return repository().upsert_resume(profile)


@app.get("/api/jobs/{job_id}/match", dependencies=[Depends(require_administrator)])
def get_job_match_endpoint(job_id: str) -> dict[str, Any]:
    try:
        job = repository().get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job not found.") from None
    return match_response(job)


@app.post("/api/jobs/{job_id}/match", dependencies=[Depends(require_administrator)])
def rematch_job_endpoint(job_id: str) -> dict[str, Any]:
    try:
        job = resume_match_service().match_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"message": "Resume match updated.", "job": job, **match_response(job)}


@app.post("/api/companies", status_code=201, dependencies=[Depends(require_administrator)])
def create_company_endpoint(request: CompanyRequest) -> dict[str, Any]:
    if not request.name.strip():
        raise HTTPException(status_code=422, detail="Company Name is required.")
    company = company_service().add_company(request.model_dump())
    return {"message": "Company added.", "company": company}


@app.put("/api/companies/{company_id}", dependencies=[Depends(require_administrator)])
def update_company_endpoint(company_id: str, request: CompanyRequest) -> dict[str, Any]:
    if not request.name.strip():
        raise HTTPException(status_code=422, detail="Company Name is required.")
    try:
        company = company_service().edit_company(company_id, request.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail="Company not found.") from None
    return {"message": "Company updated.", "company": company}


@app.post(
    "/api/companies/{company_id}/refresh",
    dependencies=[
        Depends(require_administrator), Depends(require_company_refresh_enabled),
        Depends(require_discovery_enabled), Depends(require_browser_jobs_enabled),
    ],
)
def refresh_company_endpoint(company_id: str) -> dict[str, Any]:
    if not company_refresh_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another company refresh is already running. Please try again shortly.")
    try:
        return run_single_company_refresh(company_id)
    finally:
        company_refresh_lock.release()


@app.delete("/api/companies/{company_id}", dependencies=[Depends(require_administrator)])
def delete_company_endpoint(company_id: str) -> dict[str, Any]:
    try:
        return company_service().delete_company(company_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Company not found.") from None


@app.post(
    "/api/export-companies-json",
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def export_companies_json_endpoint(request: UtilityRequest) -> dict[str, Any]:
    return run_utility("Export companies JSON", lambda: {
        "companiesExported": exporter().export_companies(include_excel=False),
        "frontendMirrorWritten": APP_WRITE_FRONTEND_MIRRORS,
        **(
            {"frontendCompaniesJson": str(DEFAULT_FRONTEND_COMPANIES_JSON)}
            if APP_WRITE_FRONTEND_MIRRORS else {}
        ),
    })


@app.post(
    "/api/fill-missing-job-boards",
    dependencies=[
        Depends(require_administrator), Depends(require_utilities_enabled),
        Depends(require_company_refresh_enabled), Depends(require_discovery_enabled),
    ],
)
def fill_missing_job_boards_endpoint(request: UtilityRequest) -> dict[str, Any]:
    return run_utility("Fill missing job board URLs", lambda: run_company_file_adapter(
        lambda: fill_missing_job_boards(
            master_path=Path(DEFAULT_MASTER), output_json_path=Path(DEFAULT_JSON_OUTPUT),
            limit=request.limit, use_browser_discovery=request.useBrowserDiscovery,
            company_filter=request.company or "", dry_run=request.dryRun, force=request.force,
            debug_job_board_discovery=request.debug, max_workers=request.maxWorkers,
            browser_workers=request.browserWorkers,
        ),
        dry_run=request.dryRun,
    ))


@app.post(
    "/api/audit-websites",
    dependencies=[
        Depends(require_administrator), Depends(require_utilities_enabled),
        Depends(require_discovery_enabled),
    ],
)
def audit_websites_endpoint(request: UtilityRequest) -> dict[str, Any]:
    return run_utility("Audit websites", lambda: run_readonly_company_adapter(lambda: summarize_audit_rows(
        audit_websites(
            master_path=Path(DEFAULT_MASTER), company_filter=request.company or "",
            limit=request.limit or None, dry_run=request.dryRun,
        )
    )))


@app.post(
    "/api/repair-websites",
    dependencies=[
        Depends(require_administrator), Depends(require_utilities_enabled),
        Depends(require_company_refresh_enabled), Depends(require_discovery_enabled),
    ],
)
def repair_websites_endpoint(request: UtilityRequest) -> dict[str, Any]:
    return run_utility("Repair websites", lambda: run_company_file_adapter(
        lambda: repair_websites(
            master_path=Path(DEFAULT_MASTER), company_filter=request.company or "",
            limit=request.limit or None, dry_run=request.dryRun,
            allow_low_confidence=request.allowLowConfidence, force=request.force,
        ),
        dry_run=request.dryRun,
    ))


@app.post(
    "/api/collect-jobs",
    dependencies=[
        Depends(require_administrator), Depends(require_utilities_enabled),
        Depends(require_browser_jobs_enabled),
    ],
)
def collect_jobs_endpoint(request: UtilityRequest) -> dict[str, Any]:
    return run_utility("Collect jobs", lambda: run_job_collection(request))


@app.post(
    "/api/export-jobs-xlsx",
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def export_jobs_xlsx_endpoint(request: UtilityRequest) -> dict[str, Any]:
    return run_utility("Export jobs XLSX", lambda: {
        "jobsExported": exporter().export_jobs(include_excel=True),
        "jobsXlsx": str(DEFAULT_JOBS_XLSX),
    })


@app.post(
    "/api/quick-refresh",
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def quick_refresh_endpoint(request: UtilityRequest) -> dict[str, Any]:
    return run_utility("Quick refresh", lambda: exporter().export_all(include_excel=False))


@app.post(
    "/api/full-refresh",
    dependencies=[
        Depends(require_administrator), Depends(require_utilities_enabled),
        Depends(require_company_refresh_enabled), Depends(require_discovery_enabled),
        Depends(require_browser_jobs_enabled),
    ],
)
def full_refresh_endpoint(request: UtilityRequest) -> dict[str, Any]:
    def full_refresh() -> dict[str, Any]:
        discovery = run_company_file_adapter(
            lambda: fill_missing_job_boards(
                master_path=Path(DEFAULT_MASTER), output_json_path=Path(DEFAULT_JSON_OUTPUT),
                limit=request.limit, use_browser_discovery=request.useBrowserDiscovery,
                company_filter=request.company or "", dry_run=request.dryRun, force=request.force,
                debug_job_board_discovery=request.debug, max_workers=request.maxWorkers,
                browser_workers=request.browserWorkers,
            ),
            dry_run=request.dryRun,
        )
        jobs = run_job_collection(request)
        return {"jobBoardDiscovery": discovery, "jobCollection": jobs}
    return run_utility("Full refresh", full_refresh)


@app.post(
    "/api/user-utilities/actions/{action}", status_code=202,
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def start_user_utility(action: str) -> dict[str, Any]:
    _require_action_features(action)
    return start_maintenance_action(action)


@app.post(
    "/api/user-utilities/import-data", status_code=202,
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
async def start_import_utility(request: Request, filename: str = Query(min_length=1, max_length=255)) -> dict[str, Any]:
    return await start_import_run(request, filename)


async def start_import_run(request: Request, filename: str) -> dict[str, Any]:
    safe_name = Path(filename).name
    suffix = Path(safe_name).suffix.lower()
    if suffix not in {".json", ".xlsx"}:
        raise HTTPException(status_code=422, detail="Import supports JSON and Excel (.xlsx) files.")
    contents = await request.body()
    if not contents:
        raise HTTPException(status_code=422, detail="The selected import file is empty.")
    import_dir = Path(IMPORT_DIR)
    import_dir.mkdir(parents=True, exist_ok=True)
    stored_path = import_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"
    stored_path.write_bytes(contents)
    def import_worker(progress: Callable[..., None], cancelled: Any) -> dict[str, Any]:
        summary = import_data_file(repository(), exporter(), stored_path, progress, cancelled)
        summary["matching"] = safe_auto_match(summary.get("jobIds", []))
        exporter().export_jobs(include_excel=True)
        return summary
    try:
        return utility_runs().start(
            action="import-data", task_name="Import Data", progress_verb="Importing",
            progress_unit="records", worker=import_worker, format_summary=format_import_summary,
        )
    except RuntimeError as exc:
        stored_path.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail=str(exc)) from None


@app.get(
    "/api/user-utilities/runs/{run_id}",
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def get_user_utility_run(run_id: str) -> dict[str, Any]:
    return get_maintenance_run(run_id)


@app.post(
    "/api/user-utilities/runs/{run_id}/cancel",
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def cancel_user_utility_run(run_id: str) -> dict[str, Any]:
    return cancel_maintenance_run(run_id)


@app.get(
    "/api/settings/email",
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def get_email_settings_endpoint() -> dict[str, Any]:
    return email_service().get_settings()


@app.put(
    "/api/settings/email",
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def update_email_settings_endpoint(request: EmailSettingsRequest) -> dict[str, Any]:
    try:
        payload = request.model_dump(exclude={"smtpPassword"})
        payload["smtpPassword"] = request.smtpPassword.get_secret_value()
        return email_service().save_settings(payload)
    except EmailConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.post(
    "/api/settings/email/test",
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def test_email_endpoint(request: TestEmailRequest) -> dict[str, Any]:
    try:
        return email_service().send_test_email(request.recipient)
    except EmailConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except EmailDeliveryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


@app.get(
    "/api/email/status",
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def email_status_endpoint() -> dict[str, Any]:
    return email_service().status()


@app.get(
    "/api/email/history",
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def email_history_endpoint(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    return {"history": email_service().history(limit)}


@app.post(
    "/api/email/send-new-jobs",
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def send_new_jobs_endpoint() -> dict[str, Any]:
    try:
        result = email_service().send_new_jobs_digest(trigger_type="manual")
    except EmailConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if result.get("status") == "Failed":
        raise HTTPException(status_code=502, detail=result.get("error") or "The daily job email could not be sent.")
    return result


@app.get(
    "/api/maintenance/jobs",
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def list_maintenance_jobs() -> dict[str, Any]:
    definitions = user_utility_definitions()
    runs = utility_runs().list_runs()
    schedules = scheduler().list_schedules()
    job_keys = [*definitions.keys(), "import-data"]
    jobs: list[dict[str, Any]] = []
    for job_key in job_keys:
        matching = [run for run in runs if run["job_key"] == job_key]
        active_run = next((run for run in matching if run["running"]), None)
        latest_run = matching[0] if matching else None
        task_name = (
            definitions[job_key]["task_name"]
            if job_key in definitions else "Import Data"
        )
        statistics = utility_runs().statistics(job_key)
        last_run = statistics["lastRun"]
        jobs.append({
            "jobKey": job_key, "job_key": job_key, "taskName": task_name,
            "description": definitions.get(job_key, {}).get("description", "Import data selected by the user."),
            "supportsScheduling": job_key in schedules,
            "schedule": schedules.get(job_key),
            "running": active_run is not None,
            "activeRunId": active_run["id"] if active_run else None,
            "active_run_id": active_run["id"] if active_run else None,
            "activeRun": active_run, "latestRun": latest_run,
            "lastRun": last_run,
            "lastRuntimeSeconds": last_run["runtimeSeconds"] if last_run else None,
            "averageRuntimeSeconds": statistics["averageRuntimeSeconds"],
            "lastResult": maintenance_result(active_run or last_run),
        })
    active_runs = [run for run in runs if run["running"]]
    return {
        "jobs": jobs, "activeRuns": active_runs,
        "runningCount": len(active_runs), "running_count": len(active_runs),
    }


@app.put(
    "/api/maintenance/jobs/{job_key}/schedule",
    dependencies=[
        Depends(require_administrator), Depends(require_utilities_enabled),
        Depends(require_schedules_enabled),
    ],
)
def update_maintenance_schedule(job_key: str, request: MaintenanceScheduleRequest) -> dict[str, Any]:
    if request.enabled:
        _require_action_features(job_key)
    try:
        return scheduler().update_schedule(
            job_key, enabled=request.enabled, run_time=request.runTime, timezone=request.timezone
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="This maintenance job does not support scheduling.") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.get(
    "/api/maintenance/jobs/{job_key}/history",
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def maintenance_job_history(
    job_key: str,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    if job_key not in {*user_utility_definitions(), "import-data"}:
        raise HTTPException(status_code=404, detail="Maintenance job not found.")
    return {"jobKey": job_key, "runs": utility_runs().history(job_key, limit=limit)}


@app.post(
    "/api/maintenance/jobs/{job_key}/run", status_code=202,
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
async def start_maintenance_job(
    job_key: str,
    request: Request,
    filename: str = Query(default="", max_length=255),
) -> dict[str, Any]:
    if job_key != "import-data":
        _require_action_features(job_key)
        return start_maintenance_action(job_key)
    if not filename:
        raise HTTPException(status_code=422, detail="Select a JSON or Excel file to import.")
    return await start_import_run(request, filename)


@app.get(
    "/api/maintenance/runs/{run_id}",
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def get_maintenance_run(run_id: str) -> dict[str, Any]:
    try:
        return utility_runs().get(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Maintenance run not found.") from None


@app.post(
    "/api/maintenance/runs/{run_id}/cancel",
    dependencies=[Depends(require_administrator), Depends(require_utilities_enabled)],
)
def cancel_maintenance_run(run_id: str) -> dict[str, Any]:
    try:
        return utility_runs().cancel(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Maintenance run not found.") from None


def start_maintenance_action(action: str, *, trigger_type: str = "manual") -> dict[str, Any]:
    definitions = user_utility_definitions()
    if action not in definitions:
        raise HTTPException(status_code=404, detail="Maintenance job not found.")
    _require_action_features(action)
    try:
        definition = definitions[action]
        return utility_runs().start(
            action=action,
            task_name=definition["task_name"],
            progress_verb=definition["progress_verb"],
            progress_unit=definition["progress_unit"],
            worker=definition["worker"],
            format_summary=definition["format_summary"],
            trigger_type=trigger_type,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def repository() -> OpportunityRepository:
    if not Path(DEFAULT_DATABASE).exists():
        raise HTTPException(status_code=503, detail="SQLite migration has not been applied. Run migrate-to-sqlite --apply.")
    return OpportunityRepository(Path(DEFAULT_DATABASE))


def resume_match_service() -> ResumeMatchService:
    return ResumeMatchService(repository())


def match_response(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "jobId": job["id"], "score": job.get("matchScore"),
        "status": job.get("matchStatus", "Not Matched"),
        "label": job.get("matchLabel", "Not Matched"),
        "matchedAt": job.get("matchedAt", ""),
        "algorithmVersion": job.get("matchAlgorithmVersion", ""),
        "details": job.get("matchDetails", {}), "error": job.get("matchError", ""),
        "needsRematch": bool(job.get("needsRematch")), "job": job,
    }


def safe_auto_match(job_ids: list[str]) -> dict[str, Any]:
    unique_ids = [job_id for job_id in dict.fromkeys(job_ids) if job_id]
    try:
        return resume_match_service().match_jobs_if_needed(unique_ids)
    except Exception as exc:
        logging.exception("Automatic resume matching failed; saved jobs were retained.")
        return {"matched": 0, "failed": len(unique_ids), "skipped": 0, "error": str(exc)}


def exporter() -> SnapshotExporter:
    return SnapshotExporter(
        repository(), master_path=Path(DEFAULT_MASTER), companies_json_path=Path(DEFAULT_JSON_OUTPUT),
        frontend_companies_json_path=Path(DEFAULT_FRONTEND_COMPANIES_JSON), jobs_json_path=Path(DEFAULT_JOBS_JSON),
        frontend_jobs_json_path=Path(DEFAULT_FRONTEND_JOBS_JSON), applications_json_path=Path(DEFAULT_APPLICATIONS_JSON),
        jobs_xlsx_path=Path(DEFAULT_JOBS_XLSX), write_frontend_mirrors=APP_WRITE_FRONTEND_MIRRORS,
    )


def company_service() -> CompanyService:
    return CompanyService(repository(), exporter())


def email_service() -> EmailService:
    return EmailService(repository())


def run_readonly_company_adapter(action: Callable[[], Any]) -> Any:
    exporter().export_companies(include_excel=True)
    return action()


def run_company_file_adapter(action: Callable[[], Any], *, dry_run: bool) -> Any:
    exporter().export_companies(include_excel=True)
    result = action()
    if not dry_run:
        snapshots = [excel_company_to_api(row) for row in read_company_rows(Path(DEFAULT_MASTER))]
        repository().upsert_company_snapshots(snapshots)
        exporter().export_companies(include_excel=True)
    return result


def run_single_company_refresh(company_id: str) -> dict[str, Any]:
    current_repository = repository()
    try:
        company_before = current_repository.get_company(company_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Company not found.") from None

    warnings: list[str] = []
    errors: list[str] = []
    metadata_changed = False
    company_after = company_before
    try:
        company_result = refresh_single_company_information(current_repository, company_id)
        company_after = company_result["company"]
        metadata_changed = bool(company_result["metadataChanged"])
        warnings.extend(company_result.get("warnings", []))
    except Exception as exc:
        logging.exception("Company discovery failed for %s.", company_before["name"])
        errors.append(f"Company information could not be refreshed: {exc}")

    current_exporter = exporter()
    current_exporter.export_companies(include_excel=True)
    current_exporter.export_jobs(include_excel=False)
    before_jobs = [job for job in current_repository.list_jobs() if job.get("companyId") == company_id]
    collection_summary: dict[str, Any] = {}
    try:
        collection_summary = collect_jobs(
            master_path=Path(DEFAULT_MASTER), jobs_json_path=Path(DEFAULT_JOBS_JSON),
            jobs_xlsx_path=Path(DEFAULT_JOBS_XLSX), company_ids={company_id},
            max_workers=1, browser_workers=1, delay_seconds=0.75,
            dry_run=False, debug_job_collection=False,
        )
        exported_jobs = json.loads(Path(DEFAULT_JOBS_JSON).read_text(encoding="utf-8-sig"))
        target_jobs = [
            job for job in exported_jobs
            if str(job.get("companyId") or "") == company_id
            or str(job.get("companyName") or "").casefold() == company_after["name"].casefold()
        ]
        current_repository.upsert_jobs_for_companies(target_jobs, {company_id})
        collection_summary["matching"] = safe_auto_match([str(job.get("id") or "") for job in target_jobs])
        candidates_path = Path(LOG_DIR) / "rejected_job_candidates.json"
        if candidates_path.exists():
            candidates = json.loads(candidates_path.read_text(encoding="utf-8-sig"))
            current_repository.replace_raw_candidates(candidates, company_ids={company_id})
        if collection_summary.get("errors"):
            warnings.append(f"{collection_summary['errors']} job source could not be fully processed.")
        if not collection_summary.get("companies_attempted"):
            warnings.append("No usable verified job board was available for job collection.")
    except Exception as exc:
        logging.exception("Job collection failed for %s.", company_before["name"])
        errors.append(f"Jobs could not be refreshed: {exc}")
    finally:
        current_exporter.export_companies(include_excel=True)
        current_exporter.export_jobs(include_excel=True)

    after_jobs = [job for job in current_repository.list_jobs() if job.get("companyId") == company_id]
    before_by_id = {job["id"]: job for job in before_jobs}
    after_by_id = {job["id"]: job for job in after_jobs}
    new_job_ids = set(after_by_id) - set(before_by_id)
    shared_ids = set(before_by_id) & set(after_by_id)
    updated_jobs = sum(1 for job_id in shared_ids if job_record_changed(before_by_id[job_id], after_by_id[job_id]))
    removed_or_closed = len(set(before_by_id) - set(after_by_id)) + sum(
        1 for job_id in shared_ids
        if str(before_by_id[job_id].get("status") or "").casefold() == "open"
        and str(after_by_id[job_id].get("status") or "").casefold() != "open"
    )
    return {
        "status": "completed" if not errors else "partial",
        "companyId": company_id, "companyName": company_after["name"],
        "companyMetadataChanged": metadata_changed,
        "totalJobsDiscovered": int(collection_summary.get("jobs_found", 0)),
        "newJobs": len(new_job_ids), "updatedJobs": updated_jobs,
        "removedOrClosedJobs": removed_or_closed,
        "activeJobs": sum(1 for job in after_jobs if str(job.get("status") or "").casefold() == "open"),
        "warnings": warnings, "errors": errors,
    }


def job_record_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    keys = (
        "title", "location", "workType", "payMin", "payMax", "payText", "payPeriod",
        "postedDate", "sourceUrl", "jobPlatform", "description", "descriptionSnippet",
        "status", "roleType", "roleTypeReason", "rawData",
    )
    return any(before.get(key) != after.get(key) for key in keys)


def run_job_collection(request: UtilityRequest, progress: Callable[[int, int, str], None] | None = None, cancellation_event: Any = None) -> dict[str, Any]:
    current_exporter = exporter()
    current_exporter.export_companies(include_excel=True)
    current_exporter.export_jobs(include_excel=False)
    summary = collect_jobs(
        master_path=Path(DEFAULT_MASTER), jobs_json_path=Path(DEFAULT_JOBS_JSON),
        jobs_xlsx_path=Path(DEFAULT_JOBS_XLSX), limit_companies=request.limitCompanies or None,
        company_filter=request.company or "", max_workers=request.maxWorkers,
        browser_workers=request.browserWorkers, delay_seconds=request.delaySeconds,
        dry_run=request.dryRun, debug_job_collection=request.debug,
        progress_callback=progress, cancellation_event=cancellation_event,
    )
    if not request.dryRun:
        jobs = json.loads(Path(DEFAULT_JOBS_JSON).read_text(encoding="utf-8-sig"))
        repository().replace_jobs(jobs)
        summary["matching"] = safe_auto_match([str(job.get("id") or "") for job in jobs])
        candidates_path = Path(LOG_DIR) / "rejected_job_candidates.json"
        if candidates_path.exists():
            candidates = json.loads(candidates_path.read_text(encoding="utf-8-sig"))
            target_ids = None
            if request.company:
                needle = request.company.casefold()
                target_ids = {
                    company["id"] for company in repository().list_companies()
                    if needle in company["id"].casefold() or needle in company["name"].casefold()
                }
            repository().replace_raw_candidates(candidates, company_ids=target_ids)
        current_exporter.export_jobs(include_excel=True)
    return summary


def user_utility_definitions() -> dict[str, dict[str, Any]]:
    def company_info_worker(progress: Callable[[int, int, str], None], cancelled: Any) -> dict[str, Any]:
        summary = refresh_missing_company_information(repository(), progress, cancelled)
        exporter().export_companies(include_excel=True)
        return summary

    def discovery_worker(progress: Callable[[int, int, str], None], cancelled: Any) -> dict[str, Any]:
        summary = refresh_company_discovery(repository(), progress, cancelled)
        exporter().export_companies(include_excel=True)
        return summary

    def jobs_worker(progress: Callable[[int, int, str], None], cancelled: Any) -> dict[str, Any]:
        request = UtilityRequest(
            limit=0, limitCompanies=0, maxWorkers=10, browserWorkers=3,
            delaySeconds=0.75, dryRun=False, debug=False, useBrowserDiscovery=True,
            force=False, allowLowConfidence=False,
        )
        return run_job_collection(request, progress, cancelled)

    def reprocess_worker(progress: Callable[[int, int, str], None], cancelled: Any) -> dict[str, Any]:
        summary = reprocess_saved_jobs(repository(), progress, cancelled)
        summary["matching"] = safe_auto_match(summary.get("changedJobIds", []))
        exporter().export_jobs(include_excel=True)
        return summary

    def rematch_worker(progress: Callable[..., None], cancelled: Any) -> dict[str, Any]:
        return resume_match_service().rematch_all(progress, cancelled)

    definitions = {
        "refresh-missing-company-information": {
            "task_name": "Refresh Missing Company Information", "progress_verb": "Checking",
            "progress_unit": "companies", "worker": company_info_worker,
            "format_summary": format_company_refresh_summary,
            "description": "Finds missing public company details and verifies careers and job-board links.",
        },
        "refresh-company-discovery": {
            "task_name": "Refresh Company Discovery", "progress_verb": "Checking",
            "progress_unit": "companies", "worker": discovery_worker,
            "format_summary": format_company_refresh_summary,
            "description": "Rechecks companies that need review or have missing job-board information.",
        },
        "refresh-all-job-listings": {
            "task_name": "Refresh All Job Listings", "progress_verb": "Checking",
            "progress_unit": "companies", "worker": jobs_worker,
            "format_summary": lambda summary: (
                f"Job listing refresh complete: {summary.get('companies_attempted', 0)} companies checked, "
                f"{summary.get('jobs_saved', 0)} jobs saved, and {summary.get('errors', 0)} companies could not be reached."
            ),
            "description": "Automatically checks every configured company for current job openings.",
            "after_scheduled_success": lambda summary: email_service().send_new_jobs_digest(trigger_type="scheduled"),
        },
        "reprocess-saved-jobs": {
            "task_name": "Reprocess Saved Jobs", "progress_verb": "Processing",
            "progress_unit": "jobs", "worker": reprocess_worker,
            "format_summary": lambda summary: (
                f"Saved job reprocessing complete: {summary.get('jobsProcessed', 0)} jobs checked and "
                f"{summary.get('jobsUpdated', 0)} updated."
            ),
            "description": "Re-reads saved jobs to update details and filters without collecting new postings.",
        },
        "rematch-all-jobs": {
            "task_name": "Rematch All Jobs", "progress_verb": "Matching",
            "progress_unit": "jobs", "worker": rematch_worker,
            "format_summary": lambda summary: (
                f"Resume matching complete: {summary.get('jobsMatched', 0)} jobs matched and "
                f"{summary.get('jobsFailed', 0)} could not be matched."
            ),
            "description": "Recalculates resume fit for all current jobs using the active resume.",
            "supports_scheduling": False,
        },
        "create-backup": {
            "task_name": "Create Backup", "progress_verb": "Backing up",
            "progress_unit": "files", "worker": lambda progress, cancelled: create_backup(
                repository(), exporter(), Path(BACKUP_DIR), progress, cancelled
            ),
            "format_summary": lambda summary: f"Backup complete: {summary.get('filesBackedUp', 0)} files saved to {summary.get('backupDirectory', '')}.",
            "description": "Creates a timestamped recovery copy of the database and export files.",
        },
        "export-data": {
            "task_name": "Export Data", "progress_verb": "Exporting",
            "progress_unit": "data sets", "worker": lambda progress, cancelled: export_data(exporter(), progress, cancelled),
            "format_summary": lambda summary: (
                f"Export complete: {summary.get('companiesExported', 0)} companies, "
                f"{summary.get('jobsExported', 0)} jobs, and {summary.get('applicationsExported', 0)} applications exported."
            ),
            "description": "Exports companies, jobs, applications, and reports to Excel and JSON.",
        },
    }
    for action, definition in definitions.items():
        definition["worker"] = _guarded_utility_worker(action, definition["worker"])
        disabled_reason = _disabled_action_reason(action)
        definition["enabled"] = not disabled_reason
        definition["disabled_reason"] = disabled_reason
    return definitions


def format_company_refresh_summary(summary: dict[str, Any]) -> str:
    return (
        f"Company information refresh complete: {summary.get('companiesUpdated', 0)} companies updated, "
        f"{summary.get('jobBoardsVerified', 0)} job boards verified, "
        f"{summary.get('companiesNeedReview', 0)} companies need review, and "
        f"{summary.get('couldNotBeReached', 0)} could not be reached."
    )


def format_import_summary(summary: dict[str, Any]) -> str:
    return (
        f"Import complete: {summary.get('companiesImported', 0)} companies and "
        f"{summary.get('jobsImported', 0)} jobs imported."
    )


def maintenance_result(run: dict[str, Any] | None) -> str:
    if run is None:
        return "Never Run"
    if run["running"]:
        return "Running"
    if run["status"] == "Completed":
        return "Success"
    return "Failed"


def run_utility(name: str, action: Callable[[], Any]) -> dict[str, Any]:
    configure_logging_once()
    started_at = datetime.now().astimezone().replace(microsecond=0).isoformat()
    started = time.monotonic()
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            summary = action()
        completed_at = datetime.now().astimezone().replace(microsecond=0).isoformat()
        response = {
            "status": "completed", "message": f"{name} completed.", "startedAt": started_at,
            "completedAt": completed_at, "durationSeconds": round(time.monotonic() - started, 2),
            "summary": make_json_safe(summary), "stdout": stdout.getvalue(), "stderr": stderr.getvalue(),
        }
    except Exception as exc:
        completed_at = datetime.now().astimezone().replace(microsecond=0).isoformat()
        logging.exception("%s endpoint failed.", name)
        response = {
            "status": "failed", "message": f"{name} failed.", "startedAt": started_at,
            "completedAt": completed_at, "durationSeconds": round(time.monotonic() - started, 2),
            "summary": {}, "error": str(exc), "stdout": stdout.getvalue(), "stderr": stderr.getvalue(),
        }
    try:
        repository().record_utility_run(name, response["status"], started_at, completed_at, response)
    except Exception:
        logging.exception("Could not persist utility run history.")
    return response


def summarize_audit_rows(rows: list[Any]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    for row in rows:
        value = getattr(row, "audit_status", "Unknown")
        statuses[value] = statuses.get(value, 0) + 1
    return {"rowsAudited": len(rows), "statuses": statuses, "auditRows": rows}


def make_json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return make_json_safe(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(item) for item in value]
    return value


def configure_logging_once() -> None:
    if not logging.getLogger().handlers:
        configure_logging()


@app.on_event("startup")
def start_maintenance_scheduler() -> None:
    validate_auth_configuration()
    email_service().bootstrap_from_environment()
    if APP_ENABLE_SCHEDULES:
        if not APP_ENABLE_UTILITIES:
            raise BlueAshConfigurationError("APP_ENABLE_SCHEDULES requires APP_ENABLE_UTILITIES=true.")
        scheduler().start()


@app.on_event("shutdown")
def stop_maintenance_scheduler() -> None:
    if _maintenance_scheduler is not None:
        _maintenance_scheduler.stop()
