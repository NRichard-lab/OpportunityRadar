from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    industry TEXT NOT NULL DEFAULT 'Financial Services',
    city TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT 'United States',
    known_website TEXT NOT NULL DEFAULT '',
    official_website TEXT NOT NULL DEFAULT '',
    website_discovery_method TEXT NOT NULL DEFAULT '',
    website_candidate_urls TEXT NOT NULL DEFAULT '',
    website_verification_notes TEXT NOT NULL DEFAULT '',
    website_verified INTEGER NOT NULL DEFAULT 0 CHECK (website_verified IN (0, 1)),
    careers_page_url TEXT NOT NULL DEFAULT '',
    job_board_url TEXT NOT NULL DEFAULT '',
    job_board_discovery_method TEXT NOT NULL DEFAULT 'Not Found',
    jobs_rss_feed_url TEXT NOT NULL DEFAULT '',
    job_platform TEXT NOT NULL DEFAULT '',
    feed_found INTEGER NOT NULL DEFAULT 0 CHECK (feed_found IN (0, 1)),
    search_status TEXT NOT NULL DEFAULT 'Needs Review',
    confidence INTEGER NOT NULL DEFAULT 0,
    last_checked TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    founded_year INTEGER,
    total_assets REAL,
    assets_as_of_date TEXT NOT NULL DEFAULT '',
    company_info_last_checked TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    legacy_id TEXT NOT NULL DEFAULT '',
    company_id TEXT REFERENCES companies(id) ON DELETE CASCADE,
    company_name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    work_type TEXT NOT NULL DEFAULT 'Not Listed',
    pay_min REAL,
    pay_max REAL,
    pay_text TEXT NOT NULL DEFAULT '',
    pay_period TEXT NOT NULL DEFAULT 'unknown',
    pay_currency TEXT NOT NULL DEFAULT 'USD',
    posted_date TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    job_platform TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    description_snippet TEXT NOT NULL DEFAULT '',
    collected_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Open',
    role_type TEXT NOT NULL DEFAULT 'UNKNOWN',
    role_type_reason TEXT NOT NULL DEFAULT '',
    raw_data_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_company_id ON jobs(company_id);

CREATE TABLE IF NOT EXISTS raw_job_candidates (
    id TEXT PRIMARY KEY,
    company_id TEXT REFERENCES companies(id) ON DELETE CASCADE,
    job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    company_name TEXT NOT NULL DEFAULT '',
    candidate_text TEXT NOT NULL DEFAULT '',
    candidate_href TEXT NOT NULL DEFAULT '',
    rejection_reason TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_candidates_company_id ON raw_job_candidates(company_id);

CREATE TABLE IF NOT EXISTS applications (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    applied INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0, 1)),
    application_status TEXT NOT NULL DEFAULT 'Interested',
    date_applied TEXT NOT NULL DEFAULT '',
    follow_up_date TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    not_interested INTEGER NOT NULL DEFAULT 0 CHECK (not_interested IN (0, 1)),
    payload_json TEXT NOT NULL DEFAULT '{}',
    archived_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resumes (
    id TEXT PRIMARY KEY,
    version TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL DEFAULT '',
    extracted_text TEXT NOT NULL DEFAULT '',
    skills_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resume_fit_results (
    id TEXT PRIMARY KEY,
    resume_id TEXT NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    score REAL,
    status TEXT NOT NULL DEFAULT 'Matched',
    resume_version TEXT NOT NULL DEFAULT '',
    job_fingerprint TEXT NOT NULL DEFAULT '',
    algorithm_version TEXT NOT NULL DEFAULT '',
    matched_at TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(resume_id, job_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS utility_runs (
    id TEXT PRIMARY KEY,
    utility_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance_job_runs (
    id TEXT PRIMARY KEY,
    job_key TEXT NOT NULL,
    task_name TEXT NOT NULL,
    trigger_type TEXT NOT NULL DEFAULT 'manual',
    progress_verb TEXT NOT NULL DEFAULT '',
    progress_unit TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    current_item TEXT NOT NULL DEFAULT '',
    current_message TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT '',
    runtime_seconds REAL,
    result_summary_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_maintenance_job_runs_key_created
    ON maintenance_job_runs(job_key, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_maintenance_job_runs_one_active_per_key
    ON maintenance_job_runs(job_key)
    WHERE status IN ('Queued', 'Running', 'Cancelling');

CREATE TABLE IF NOT EXISTS maintenance_schedules (
    job_key TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    frequency TEXT NOT NULL DEFAULT 'daily',
    run_time TEXT NOT NULL DEFAULT '02:00',
    timezone TEXT NOT NULL DEFAULT 'America/Denver',
    last_scheduled_date TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_history (
    id TEXT PRIMARY KEY,
    migration_version TEXT NOT NULL,
    source_manifest_path TEXT NOT NULL,
    source_manifest_sha256 TEXT NOT NULL,
    report_json_path TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    counts_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS email_settings (
    id TEXT PRIMARY KEY,
    smtp_host TEXT NOT NULL DEFAULT '',
    smtp_port INTEGER NOT NULL DEFAULT 465,
    security TEXT NOT NULL DEFAULT 'ssl_tls',
    smtp_username TEXT NOT NULL DEFAULT '',
    smtp_password_ciphertext TEXT NOT NULL DEFAULT '',
    from_email TEXT NOT NULL DEFAULT '',
    from_name TEXT NOT NULL DEFAULT 'Opportunity Radar',
    reply_to_email TEXT NOT NULL DEFAULT '',
    daily_enabled INTEGER NOT NULL DEFAULT 0 CHECK (daily_enabled IN (0, 1)),
    recipient_email TEXT NOT NULL DEFAULT '',
    send_after_refresh INTEGER NOT NULL DEFAULT 1 CHECK (send_after_refresh IN (0, 1)),
    send_when_empty INTEGER NOT NULL DEFAULT 0 CHECK (send_when_empty IN (0, 1)),
    tracking_started_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_digests (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL DEFAULT '',
    job_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    trigger_type TEXT NOT NULL DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS idx_email_digests_started ON email_digests(started_at DESC);

CREATE TABLE IF NOT EXISTS email_digest_jobs (
    digest_id TEXT NOT NULL REFERENCES email_digests(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (digest_id, job_id),
    UNIQUE (job_id)
);

CREATE TABLE IF NOT EXISTS email_sent_jobs (
    job_id TEXT PRIMARY KEY,
    digest_id TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

"""


def connect(
    database_path: Path,
    *,
    readonly: bool = False,
    require_existing: bool = False,
) -> sqlite3.Connection:
    database_path = Path(database_path)
    if readonly or require_existing:
        mode = "ro" if readonly else "rw"
        # SQLite URI modes are deliberate here: both fail when the target does
        # not exist, so a lost production mount cannot be replaced by an empty
        # database between an application-level existence check and connect().
        connection = sqlite3.connect(
            f"{database_path.resolve(strict=False).as_uri()}?mode={mode}",
            uri=True,
            timeout=5.0,
            check_same_thread=True,
        )
    else:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            database_path,
            timeout=5.0,
            check_same_thread=True,
        )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if not readonly:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        return connection
    except BaseException:
        try:
            connection.close()
        except sqlite3.Error:
            pass
        raise


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    existing = {row[1] for row in connection.execute("PRAGMA table_info(companies)")}
    for column, definition in (
        ("founded_year", "INTEGER"),
        ("total_assets", "REAL"),
        ("assets_as_of_date", "TEXT NOT NULL DEFAULT ''"),
        ("company_info_last_checked", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in existing:
            connection.execute(f"ALTER TABLE companies ADD COLUMN {column} {definition}")
    run_columns = {row[1] for row in connection.execute("PRAGMA table_info(maintenance_job_runs)")}
    if "trigger_type" not in run_columns:
        connection.execute("ALTER TABLE maintenance_job_runs ADD COLUMN trigger_type TEXT NOT NULL DEFAULT 'manual'")
    job_columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    if "first_seen_at" not in job_columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN first_seen_at TEXT NOT NULL DEFAULT ''")
    connection.execute(
        "UPDATE jobs SET first_seen_at=COALESCE(NULLIF(collected_at,''),created_at) WHERE first_seen_at=''"
    )
    resume_columns = {row[1] for row in connection.execute("PRAGMA table_info(resumes)")}
    if "version" not in resume_columns:
        connection.execute("ALTER TABLE resumes ADD COLUMN version TEXT NOT NULL DEFAULT ''")
    connection.execute(
        """UPDATE resumes SET version = COALESCE(
        NULLIF(json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END, '$.version'), ''),
        NULLIF(json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END, '$.id'), ''), id
        ) WHERE version = ''"""
    )
    fit_columns = {row[1] for row in connection.execute("PRAGMA table_info(resume_fit_results)")}
    for column, definition in (
        ("status", "TEXT NOT NULL DEFAULT 'Matched'"),
        ("resume_version", "TEXT NOT NULL DEFAULT ''"),
        ("job_fingerprint", "TEXT NOT NULL DEFAULT ''"),
        ("algorithm_version", "TEXT NOT NULL DEFAULT ''"),
        ("matched_at", "TEXT NOT NULL DEFAULT ''"),
        ("error", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in fit_columns:
            connection.execute(f"ALTER TABLE resume_fit_results ADD COLUMN {column} {definition}")
    now = datetime.now().astimezone().replace(microsecond=0).isoformat()
    connection.execute(
        "INSERT OR IGNORE INTO settings (key,value_json,updated_at) VALUES ('scheduler_timezone','\"America/Denver\"',?)",
        (now,),
    )
    connection.execute("PRAGMA user_version = 6")
    connection.commit()
