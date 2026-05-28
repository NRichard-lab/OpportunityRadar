from __future__ import annotations

import io
import logging
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import DEFAULT_JSON_OUTPUT, DEFAULT_MASTER
from main import configure_logging, fill_missing_job_boards


app = FastAPI(title="Financial Jobs Radar Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class FillMissingJobBoardsRequest(BaseModel):
    limit: int = Field(default=10, ge=0)
    company: str | None = None
    useBrowserDiscovery: bool = True
    dryRun: bool = False


@app.get("/api/status")
def status() -> dict[str, str]:
    return {
        "status": "ready",
        "message": "Financial Jobs Radar backend is running",
    }


@app.post("/api/fill-missing-job-boards")
def fill_missing_job_boards_endpoint(request: FillMissingJobBoardsRequest) -> dict[str, Any]:
    stdout = io.StringIO()
    stderr = io.StringIO()

    try:
        configure_logging_once()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            summary = fill_missing_job_boards(
                master_path=Path(DEFAULT_MASTER),
                output_json_path=Path(DEFAULT_JSON_OUTPUT),
                limit=request.limit,
                use_browser_discovery=request.useBrowserDiscovery,
                company_filter=request.company or "",
                dry_run=request.dryRun,
            )
        return {
            "status": "completed",
            "message": "Missing job board discovery completed.",
            **summary,
            "commandOutput": stdout.getvalue(),
            "errorOutput": stderr.getvalue(),
        }
    except Exception as exc:
        logging.exception("Fill missing job boards endpoint failed.")
        return {
            "status": "failed",
            "message": str(exc),
            "commandOutput": stdout.getvalue(),
            "errorOutput": stderr.getvalue(),
        }


def configure_logging_once() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    configure_logging()
