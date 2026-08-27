from __future__ import annotations

import argparse
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from config import (
    APP_ENABLE_BROWSER_JOBS,
    APP_ENABLE_COMPANY_REFRESH,
    APP_ENABLE_DISCOVERY,
    DATA_DIR,
    DEFAULT_APPLICATIONS_JSON,
    DEFAULT_INPUT,
    DEFAULT_JOBS_JSON,
    DEFAULT_JOBS_XLSX,
    DEFAULT_JSON_OUTPUT,
    DEFAULT_MASTER,
    LOG_DIR,
    OUTPUT_DIR,
)
from excel_tools import (
    create_sample_workbook,
    export_excel_to_json,
    read_company_rows,
    read_companies,
    update_master,
    write_master,
)


VALID_MODES = {
    "export-json",
    "bootstrap-enrich",
    "fill-missing-job-boards",
    "collect-jobs",
    "discover-job-board",
    "audit-websites",
    "repair-websites",
}


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"financial_jobs_radar_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )
    logging.getLogger("primp").setLevel(logging.WARNING)
    logging.getLogger("ddgs").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.info("Writing log to %s", log_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Opportunity Radar company data tools.")
    parser.add_argument(
        "--mode",
        choices=sorted(VALID_MODES),
        default="export-json",
        help="Run local JSON export, master enrichment, job board fill, or job collection.",
    )
    parser.add_argument("--input", type=Path, default=None, help="Input Excel workbook path.")
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER, help="Permanent master company workbook path.")
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON_OUTPUT, help="Output companies JSON path.")
    parser.add_argument("--jobs-json", type=Path, default=DEFAULT_JOBS_JSON, help="Output jobs JSON snapshot path.")
    parser.add_argument("--jobs-xlsx", type=Path, default=DEFAULT_JOBS_XLSX, help="Output jobs Excel snapshot path.")
    parser.add_argument("--use-browser-discovery", action="store_true", help="Use Playwright to discover public job board URLs during enrichment.")
    parser.add_argument("--company", default="", help="Company name for discover-job-board mode.")
    parser.add_argument("--companies", default="", help="Comma-separated company names for targeted collect-jobs mode.")
    parser.add_argument("--careers-url", default="", help="Careers URL for discover-job-board mode.")
    parser.add_argument("--limit", type=int, default=0, help="Limit rows processed for fill-missing-job-boards.")
    parser.add_argument("--limit-companies", type=int, default=0, help="Limit companies processed for collect-jobs.")
    parser.add_argument("--max-workers", type=int, default=10, help="Concurrent workers for HTTP/static enrichment and collectors.")
    parser.add_argument("--browser-workers", type=int, default=3, help="Concurrent workers for browser collectors.")
    parser.add_argument("--delay-seconds", type=float, default=1.0, help="Delay before collector requests.")
    parser.add_argument("--dry-run", action="store_true", help="Show planned work without writing job snapshots.")
    parser.add_argument("--allow-low-confidence", action="store_true", help="Allow repair/enrichment to store low-confidence website matches.")
    parser.add_argument("--force", action="store_true", help="Allow repair/discovery modes to overwrite existing values when appropriate.")
    parser.add_argument("--debug-job-collection", action="store_true", help="Write debug files for rejected job candidates.")
    parser.add_argument("--debug-job-board-discovery", action="store_true", help="Write detailed job board URL discovery audit files.")
    parser.add_argument("--max-pages", type=int, default=5, help="Maximum same-domain careers pages to scan for job board discovery.")
    parser.add_argument("--skip-recent-days", type=int, default=7, help="Skip enrichment rows checked within this many days unless --force is used.")
    return parser.parse_args()


def combine_notes(*parts: str | list[str]) -> str:
    notes: list[str] = []
    for part in parts:
        if isinstance(part, list):
            notes.extend(value for value in part if value)
        elif part:
            notes.append(part)
    return "; ".join(dict.fromkeys(notes))


def status_for_result(official_url: str, careers_url: str, confidence: str, feed_found: bool) -> str:
    if official_url and careers_url and confidence in {"High", "Medium"}:
        return "Completed"
    if official_url and (careers_url or feed_found):
        return "Partial"
    if official_url:
        return "Needs Review"
    return "Failed"


def enrich_company(company: dict[str, str], checked_at: str, use_browser_discovery: bool = False) -> dict[str, object]:
    from job_platforms import detect_job_platform
    from search_tools import choose_official_website_details
    from website_tools import find_careers_page, find_feed, make_session

    logger = logging.getLogger(__name__)
    session = make_session()
    name = company.get("Company Name", "")
    logger.info("Enriching %s", name)

    base_result: dict[str, object] = {
        "Company Name": name,
        "City": company.get("City", ""),
        "State": company.get("State", ""),
        "Known Website": company.get("Known Website", ""),
        "Official Website": "",
        "Website Discovery Method": "Not Found",
        "Website Candidate URLs": "",
        "Website Verification Notes": "",
        "Website Verified": False,
        "Careers Page URL": "",
        "Job Board URL": "",
        "Job Board Discovery Method": "Not Found",
        "Jobs RSS Feed URL": "",
        "Job Platform": "",
        "Feed Found": False,
        "Search Status": "Failed",
        "Confidence": "Low",
        "Last Checked": checked_at,
        "Notes": company.get("Notes", ""),
    }

    try:
        phase_started = time.monotonic()
        website_result = choose_official_website_details(
            name,
            company.get("Known Website", ""),
            session,
            company.get("City", ""),
            company.get("State", ""),
        )
        website_seconds = time.monotonic() - phase_started
        official_url = website_result.final_url if website_result.verified else ""
        confidence = website_result.confidence
        website_notes = "; ".join(website_result.notes)
        phase_started = time.monotonic()
        careers_url, careers_platform, careers_notes = find_careers_page(official_url, session)
        feed_url, feed_found, feed_notes = find_feed(official_url, careers_url, session)
        careers_seconds = time.monotonic() - phase_started
        platform = careers_platform or detect_job_platform(official_url, careers_url, feed_url)
        job_board_url = careers_url if detect_job_platform(careers_url) else ""
        job_board_method = "Static Link" if job_board_url else "Not Found"
        browser_notes = ""
        browser_seconds = 0.0

        if use_browser_discovery and careers_url and not job_board_url:
            from browser_tools import discover_job_board_with_browser

            phase_started = time.monotonic()
            discovery = discover_job_board_with_browser(careers_url, name)
            browser_seconds = time.monotonic() - phase_started
            browser_notes = str(discovery.get("notes") or "")
            if discovery.get("final_url"):
                job_board_url = str(discovery["final_url"])
                job_board_method = "Browser Click"
                platform = str(discovery.get("platform") or platform)

        status = status_for_result(official_url, careers_url, confidence, feed_found)

        base_result.update(
            {
                "Official Website": official_url,
                "Website Discovery Method": website_result.discovery_method,
                "Website Candidate URLs": "\n".join(website_result.candidate_urls),
                "Website Verification Notes": website_notes,
                "Website Verified": bool(website_result.verified),
                "Careers Page URL": careers_url,
                "Job Board URL": job_board_url,
                "Job Board Discovery Method": job_board_method,
                "Jobs RSS Feed URL": feed_url,
                "Job Platform": platform,
                "Feed Found": bool(feed_found),
                "Search Status": status,
                "Confidence": confidence,
                "Notes": combine_notes(company.get("Notes", ""), website_notes, careers_notes, feed_notes, browser_notes),
                "Timing": {
                    "websiteSearchSeconds": round(website_seconds, 2),
                    "careersDiscoverySeconds": round(careers_seconds, 2),
                    "browserDiscoverySeconds": round(browser_seconds, 2),
                },
            }
        )
        logger.info(
            "Enriched %s timing: website search time=%.2fs, careers discovery time=%.2fs, browser discovery time=%.2fs",
            name,
            website_seconds,
            careers_seconds,
            browser_seconds,
        )
    except Exception as exc:
        logger.exception("Failed to enrich %s", name)
        base_result["Notes"] = combine_notes(company.get("Notes", ""), f"enrichment failed: {exc}")

    return base_result


def ensure_default_sample_input(input_path: Path) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if not input_path.exists() and input_path == DEFAULT_INPUT.resolve():
        logging.info("Sample input workbook missing; creating %s", input_path)
        create_sample_workbook(input_path)

    if not input_path.exists():
        raise FileNotFoundError(f"Input workbook does not exist: {input_path}")


def export_json_only(input_path: Path, output_json_path: Path) -> int:
    logging.info("Mode: export-json")
    logging.info("Reading Excel: %s", input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input workbook does not exist: {input_path}")
    logging.info("Writing companies.json: %s", output_json_path)
    count = export_excel_to_json(input_path, output_json_path)
    logging.info("Wrote %s companies to %s", count, output_json_path)
    logging.info("No web search performed.")
    return count


def discover_job_board(company_name: str, careers_url: str) -> dict[str, str | None]:
    from browser_tools import discover_job_board_with_browser

    logging.info("Mode: discover-job-board")
    logging.info("Browser job board discovery started...")
    logging.info("Company: %s", company_name)
    logging.info("Careers URL: %s", careers_url)
    result = discover_job_board_with_browser(careers_url, company_name)
    logging.info("Discovery result: %s", result)
    return result


def bootstrap_enrich(
    input_path: Path,
    master_path: Path,
    output_json_path: Path,
    use_browser_discovery: bool = False,
    max_workers: int = 15,
    browser_workers: int = 3,
    skip_recent_days: int = 7,
    force: bool = False,
) -> list[dict[str, object]]:
    logging.info("Mode: bootstrap-enrich")
    logging.info("Web enrichment started in fast static mode.")
    if use_browser_discovery:
        logging.info("Browser job board discovery explicitly enabled; it will run only after static discovery misses a job board URL.")
    else:
        logging.info("Browser job board discovery disabled for bootstrap-enrich.")
    logging.info("Reading Excel: %s", input_path)
    ensure_default_sample_input(input_path)
    companies = read_companies(input_path)
    logging.info("Loaded %s companies from %s", len(companies), input_path)

    checked_at = datetime.now().astimezone().replace(microsecond=0).isoformat()
    existing_rows = read_company_rows(master_path) if master_path.exists() else []
    existing_by_key = {}
    for row in existing_rows:
        existing_by_key[company_cache_key(row)] = row
        existing_by_key[company_name_state_key(row)] = row
    work_items = []
    skipped = 0
    for company in companies:
        existing = existing_by_key.get(company_cache_key(company)) or existing_by_key.get(company_name_state_key(company))
        if existing and should_skip_enrichment(existing, skip_recent_days=skip_recent_days, force=force):
            skipped += 1
            continue
        work_items.append(company)
    logging.info("Rows skipped by cache/completeness: %s", skipped)
    logging.info("Rows queued for static enrichment: %s", len(work_items))

    static_started = time.monotonic()
    results = run_enrichment_static_phase(work_items, checked_at, max_workers=max_workers)
    logging.info("Static enrichment phase completed in %.2fs.", time.monotonic() - static_started)

    if use_browser_discovery:
        browser_candidates = [result for result in results if result.get("Careers Page URL") and not result.get("Job Board URL")]
        logging.info("Rows queued for browser discovery after static misses: %s", len(browser_candidates))
        browser_started = time.monotonic()
        run_enrichment_browser_phase(browser_candidates, browser_workers=browser_workers)
        logging.info("Browser discovery phase completed in %.2fs.", time.monotonic() - browser_started)

    write_started = time.monotonic()
    update_master(master_path, results)
    logging.info("Excel write time: %.2fs", time.monotonic() - write_started)
    logging.info("Updated master workbook: %s", master_path)
    export_started = time.monotonic()
    export_json_only(master_path, output_json_path)
    logging.info("JSON export time: %.2fs", time.monotonic() - export_started)
    return results


def run_enrichment_static_phase(companies: list[dict[str, str]], checked_at: str, max_workers: int) -> list[dict[str, object]]:
    if not companies:
        return []
    results: list[dict[str, object]] = []
    started = time.monotonic()
    total = len(companies)
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = {executor.submit(enrich_company, company, checked_at, False): company for company in companies}
        for completed, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            log_progress("Static enrichment", completed, total, started)
    return results


def run_enrichment_browser_phase(results: list[dict[str, object]], browser_workers: int) -> None:
    if not results:
        return
    started = time.monotonic()
    total = len(results)
    with ThreadPoolExecutor(max_workers=max(1, browser_workers)) as executor:
        futures = {executor.submit(apply_browser_discovery_to_result, result): result for result in results}
        for completed, future in enumerate(as_completed(futures), start=1):
            future.result()
            log_progress("Browser discovery", completed, total, started)


def apply_browser_discovery_to_result(result: dict[str, object]) -> None:
    from browser_tools import discover_job_board_with_browser
    from job_platforms import detect_job_platform

    company_name = str(result.get("Company Name") or "")
    careers_url = str(result.get("Careers Page URL") or "")
    if not careers_url or result.get("Job Board URL"):
        return
    started = time.monotonic()
    discovery = discover_job_board_with_browser(careers_url, company_name)
    browser_seconds = time.monotonic() - started
    if discovery.get("final_url"):
        final_url = str(discovery["final_url"])
        result["Job Board URL"] = final_url
        result["Job Board Discovery Method"] = "Browser Click"
        result["Job Platform"] = str(discovery.get("platform") or detect_job_platform(final_url) or result.get("Job Platform") or "")
        result["Notes"] = combine_notes(str(result.get("Notes") or ""), str(discovery.get("notes") or ""))
    timing = result.get("Timing") if isinstance(result.get("Timing"), dict) else {}
    timing["browserDiscoverySeconds"] = round(browser_seconds, 2)
    result["Timing"] = timing
    logging.info("Browser discovery time for %s: %.2fs", company_name, browser_seconds)


def log_progress(label: str, completed: int, total: int, started: float) -> None:
    elapsed = max(0.001, time.monotonic() - started)
    average = elapsed / completed
    remaining = max(0.0, average * (total - completed))
    logging.info(
        "%s: Processing %s of %s. Average seconds per company: %.2f. Estimated time remaining: %.1fs.",
        label,
        completed,
        total,
        average,
        remaining,
    )


def fill_missing_job_boards(
    master_path: Path,
    output_json_path: Path,
    limit: int = 0,
    use_browser_discovery: bool = False,
    company_filter: str = "",
    dry_run: bool = False,
    force: bool = False,
    debug_job_board_discovery: bool = False,
    max_pages: int = 5,
    max_workers: int = 15,
    browser_workers: int = 3,
    skip_recent_days: int = 7,
) -> dict[str, object]:
    logging.info("Mode: fill-missing-job-boards")
    if not use_browser_discovery:
        logging.info("Browser discovery disabled; static and verified-search discovery will still be attempted.")
    rows = read_company_rows(master_path)
    rows_reviewed = len(rows)
    candidates = [row for row in rows if needs_job_board_discovery(row, force=force)]
    if skip_recent_days > 0 and not force:
        before_recent_filter = len(candidates)
        candidates = [
            row for row in candidates
            if not last_checked_within_days(row, skip_recent_days)
        ]
        logging.info("Rows skipped because Last Checked is within %s day(s): %s", skip_recent_days, before_recent_filter - len(candidates))
    if company_filter:
        candidates = [
            row for row in candidates if company_filter.lower() in str(row.get("Company Name") or "").lower()
        ]
    if limit:
        candidates = candidates[:limit]
    logging.info("Rows needing job board URL: %s", len(candidates))

    found = 0
    skipped = 0
    rejected = 0
    needs_review = 0
    likely_incorrect = 0
    errors = 0
    audit_results = []

    from job_board_discovery import discover_job_board_for_row, write_job_board_audit

    started = time.monotonic()
    worker_count = browser_workers if use_browser_discovery else max_workers
    discovery_results = []
    with ThreadPoolExecutor(max_workers=max(1, worker_count)) as executor:
        futures = {}
        for row in candidates:
            company_name = str(row.get("Company Name") or "")
            logging.info("Discovering job board for %s", company_name)
            future = executor.submit(
                discover_job_board_for_row,
                row,
                use_browser_discovery=use_browser_discovery,
                max_pages=max_pages,
                force=force,
                debug=debug_job_board_discovery,
            )
            futures[future] = row

        for completed, future in enumerate(as_completed(futures), start=1):
            row = futures[future]
            company_name = str(row.get("Company Name") or "")
            try:
                discovery = future.result()
                discovery_results.append((row, discovery))
                audit_results.append(discovery)
            except Exception as exc:
                logging.exception("Job board discovery failed for %s", company_name)
                row["Notes"] = combine_notes(str(row.get("Notes") or ""), f"job board discovery failed: {exc}")
                errors += 1
            log_progress("Job board discovery", completed, len(candidates), started)

    for row, discovery in discovery_results:
        company_name = str(row.get("Company Name") or "")
        if discovery.likely_incorrect_existing:
            likely_incorrect += 1
        if discovery.status == "Skipped":
            skipped += 1
            continue
        if discovery.found and discovery.candidate_selected:
            found += 1
            if not dry_run:
                row["Job Board URL"] = discovery.candidate_selected
                row["Job Board Discovery Method"] = discovery.discovery_method
                if discovery.platform:
                    row["Job Platform"] = discovery.platform
                row["Notes"] = combine_notes(str(row.get("Notes") or ""), discovery.notes)
                if discovery.discovery_method == "Static Link":
                    row["Website Verification Notes"] = combine_notes(
                        str(row.get("Website Verification Notes") or ""),
                        "Found known job platform link in static HTML.",
                    )
            continue
        if discovery.status == "Needs Review":
            needs_review += 1
        elif discovery.status == "Likely Incorrect":
            rejected += 1

    write_job_board_audit(audit_results)

    if dry_run:
        logging.info("Dry run enabled; master workbook and JSON were not written.")
        exported = 0
    else:
        write_started = time.monotonic()
        write_master(master_path, rows)
        logging.info("Excel write time: %.2fs", time.monotonic() - write_started)
        exported = export_json_only(master_path, output_json_path)

    summary = {
        "rowsReviewed": rows_reviewed,
        "rowsSkipped": max(0, rows_reviewed - len(candidates)) + skipped,
        "missingRowsAttempted": len(candidates),
        "jobBoardUrlsFound": found,
        "rejected": rejected,
        "likelyIncorrectExisting": likely_incorrect,
        "needsReview": needs_review,
        "notFound": max(0, len(candidates) - found - errors - skipped - rejected - needs_review),
        "errors": errors,
        "companiesExported": exported,
        "dryRun": dry_run,
    }
    logging.info("Fill missing job board summary: %s", summary)
    return summary


def needs_job_board_discovery(row: dict[str, object], force: bool = False) -> bool:
    careers_url = str(row.get("Careers Page URL") or "").strip().rstrip("/")
    official_url = str(row.get("Official Website") or row.get("Known Website") or "").strip().rstrip("/")
    job_board_url = str(row.get("Job Board URL") or "").strip().rstrip("/")
    if not careers_url and not official_url:
        return False
    if force and (careers_url or official_url):
        return True
    if not job_board_url:
        return True
    if likely_non_job_board_url(job_board_url):
        return True
    if careers_url and job_board_url == careers_url:
        return True
    return False


def likely_non_job_board_url(url: str) -> bool:
    from job_board_discovery import rejection_reason

    return bool(rejection_reason(url))


def last_checked_within_days(row: dict[str, object], days: int) -> bool:
    last_checked = parse_datetime(str(row.get("Last Checked") or ""))
    if not last_checked:
        return False
    age_days = (datetime.now().astimezone() - last_checked).total_seconds() / 86400
    return age_days < days


def company_cache_key(row: dict[str, object]) -> str:
    name = str(row.get("Company Name") or "").strip().lower()
    state = str(row.get("State") or "").strip().lower()
    known = str(row.get("Known Website") or row.get("Official Website") or "").strip().lower().rstrip("/")
    return f"{name}|{state}|{known}"


def company_name_state_key(row: dict[str, object]) -> str:
    name = str(row.get("Company Name") or "").strip().lower()
    state = str(row.get("State") or "").strip().lower()
    return f"{name}|{state}"


def should_skip_enrichment(row: dict[str, object], skip_recent_days: int, force: bool) -> bool:
    if force:
        return False
    website_verified = str(row.get("Website Verified") or "").strip().lower() in {"true", "yes", "1"}
    has_job_board = bool(str(row.get("Job Board URL") or "").strip())
    if website_verified and has_job_board:
        return True
    if skip_recent_days <= 0:
        return False
    last_checked = parse_datetime(str(row.get("Last Checked") or ""))
    if not last_checked:
        return False
    age_days = (datetime.now().astimezone() - last_checked).total_seconds() / 86400
    return age_days < skip_recent_days


def parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def resolve_input_path(args: argparse.Namespace) -> Path:
    if args.input:
        return args.input.resolve()
    return DEFAULT_INPUT.resolve()


def main() -> int:
    args = parse_args()
    validate_cli_feature_flags(args)
    configure_logging()
    input_path = resolve_input_path(args)
    master_path = args.master.resolve()
    output_json_path = args.output_json.resolve()

    if args.mode == "export-json":
        export_json_only(master_path if not args.input else input_path, output_json_path)
    elif args.mode == "bootstrap-enrich":
        bootstrap_enrich(
            input_path,
            master_path,
            output_json_path,
            use_browser_discovery=args.use_browser_discovery,
            max_workers=args.max_workers,
            browser_workers=args.browser_workers,
            skip_recent_days=args.skip_recent_days,
            force=args.force,
        )
    elif args.mode == "fill-missing-job-boards":
        fill_missing_job_boards(
            master_path,
            output_json_path,
            args.limit,
            args.use_browser_discovery,
            company_filter=args.company,
            dry_run=args.dry_run,
            force=args.force,
            debug_job_board_discovery=args.debug_job_board_discovery,
            max_pages=args.max_pages,
            max_workers=args.max_workers,
            browser_workers=args.browser_workers,
            skip_recent_days=args.skip_recent_days,
        )
    elif args.mode == "collect-jobs":
        from job_tools import collect_jobs

        logging.info("Mode: collect-jobs")
        company_filters = parse_company_filters(args.company, args.companies)
        if company_filters:
            prepare_company_sources_for_collection(master_path, output_json_path, company_filters)
        collect_jobs(
            master_path=master_path,
            jobs_json_path=args.jobs_json.resolve(),
            jobs_xlsx_path=args.jobs_xlsx.resolve(),
            limit_companies=args.limit_companies or None,
            company_filter=args.company,
            company_filters=company_filters,
            max_workers=args.max_workers,
            browser_workers=args.browser_workers,
            delay_seconds=args.delay_seconds,
            dry_run=args.dry_run,
            debug_job_collection=args.debug_job_collection,
        )
    elif args.mode == "discover-job-board":
        if not args.company or not args.careers_url:
            raise ValueError("--company and --careers-url are required for discover-job-board mode.")
        result = discover_job_board(args.company, args.careers_url)
        print(json.dumps(result, indent=2))
    elif args.mode == "audit-websites":
        from website_audit import audit_websites

        audit_websites(
            master_path,
            company_filter=args.company,
            limit=args.limit or None,
            dry_run=args.dry_run,
        )
    elif args.mode == "repair-websites":
        from website_audit import repair_websites

        repair_websites(
            master_path,
            company_filter=args.company,
            limit=args.limit or None,
            dry_run=args.dry_run,
            allow_low_confidence=args.allow_low_confidence,
            force=args.force,
        )
    else:
        raise ValueError(f"Unsupported mode: {args.mode}")

    return 0


def validate_cli_feature_flags(args: argparse.Namespace) -> None:
    requirements: dict[str, tuple[tuple[bool, str], ...]] = {
        "bootstrap-enrich": (
            (APP_ENABLE_COMPANY_REFRESH, "APP_ENABLE_COMPANY_REFRESH"),
            (APP_ENABLE_DISCOVERY, "APP_ENABLE_DISCOVERY"),
        ),
        "fill-missing-job-boards": (
            (APP_ENABLE_COMPANY_REFRESH, "APP_ENABLE_COMPANY_REFRESH"),
            (APP_ENABLE_DISCOVERY, "APP_ENABLE_DISCOVERY"),
        ),
        "collect-jobs": ((APP_ENABLE_BROWSER_JOBS, "APP_ENABLE_BROWSER_JOBS"),),
        "discover-job-board": ((APP_ENABLE_DISCOVERY, "APP_ENABLE_DISCOVERY"),),
        "audit-websites": ((APP_ENABLE_DISCOVERY, "APP_ENABLE_DISCOVERY"),),
        "repair-websites": (
            (APP_ENABLE_COMPANY_REFRESH, "APP_ENABLE_COMPANY_REFRESH"),
            (APP_ENABLE_DISCOVERY, "APP_ENABLE_DISCOVERY"),
        ),
    }
    disabled = [name for enabled, name in requirements.get(args.mode, ()) if not enabled]
    if disabled:
        raise RuntimeError(
            f"{args.mode} is disabled. Explicitly enable the required feature flags: {', '.join(disabled)}."
        )


def parse_company_filters(company: str, companies: str) -> list[str]:
    filters = []
    if company:
        filters.append(company)
    if companies:
        filters.extend(part.strip() for part in companies.split(",") if part.strip())
    return list(dict.fromkeys(filters))


def prepare_company_sources_for_collection(master_path: Path, output_json_path: Path, company_filters: list[str]) -> None:
    rows = read_company_rows(master_path)
    selected = [
        row for row in rows
        if any(filter_value.lower() in str(row.get("Company Name") or "").lower() for filter_value in company_filters)
    ]
    for row in selected:
        log_company_source_data(row)
    needs_discovery = [row for row in selected if needs_job_board_discovery(row)]
    if not needs_discovery:
        return
    logging.info("Targeted source prep: %s selected companies need job board discovery.", len(needs_discovery))
    for row in needs_discovery:
        company_name = str(row.get("Company Name") or "")
        fill_missing_job_boards(
            master_path,
            output_json_path,
            limit=0,
            use_browser_discovery=True,
            company_filter=company_name,
            dry_run=False,
            force=False,
            debug_job_board_discovery=True,
            max_pages=5,
            max_workers=15,
            browser_workers=3,
            skip_recent_days=0,
        )


def log_company_source_data(row: dict[str, object]) -> None:
    logging.info("Company Name: %s", row.get("Company Name") or "")
    logging.info("Official Website: %s", row.get("Official Website") or "")
    logging.info("Careers Page URL: %s", row.get("Careers Page URL") or "")
    logging.info("Job Board URL: %s", row.get("Job Board URL") or "")
    logging.info("Job Platform: %s", row.get("Job Platform") or "")
    logging.info("Job Board Discovery Method: %s", row.get("Job Board Discovery Method") or "")
    logging.info("Search Status: %s", row.get("Search Status") or "")
    logging.info("Confidence: %s", row.get("Confidence") or "")
    logging.info("Last Checked: %s", row.get("Last Checked") or "")


if __name__ == "__main__":
    DEFAULT_APPLICATIONS_JSON.parent.mkdir(parents=True, exist_ok=True)
    if not DEFAULT_APPLICATIONS_JSON.exists():
        DEFAULT_APPLICATIONS_JSON.write_text("[]\n", encoding="utf-8")
    raise SystemExit(main())
