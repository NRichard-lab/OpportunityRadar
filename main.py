from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

from config import DEFAULT_INPUT, DEFAULT_OUTPUT, LOG_DIR, OUTPUT_DIR
from excel_tools import create_sample_workbook, read_companies, write_results
from job_platforms import detect_job_platform
from search_tools import choose_official_website
from website_tools import find_careers_page, find_feed, make_session


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
    parser = argparse.ArgumentParser(description="Enrich a bank/credit union Excel list with career site data.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Input Excel workbook path.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output Excel workbook path.")
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


def enrich_company(company: dict[str, str], checked_at: str) -> dict[str, object]:
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
        "Careers Page URL": "",
        "Jobs RSS Feed URL": "",
        "Job Platform": "",
        "Feed Found": False,
        "Search Status": "Failed",
        "Confidence": "Low",
        "Last Checked": checked_at,
        "Notes": company.get("Notes", ""),
    }

    try:
        official_url, confidence, website_notes = choose_official_website(
            name,
            company.get("Known Website", ""),
            session,
            company.get("City", ""),
            company.get("State", ""),
        )
        careers_url, careers_platform, careers_notes = find_careers_page(official_url, session)
        feed_url, feed_found, feed_notes = find_feed(official_url, careers_url, session)
        platform = careers_platform or detect_job_platform(official_url, careers_url, feed_url)
        status = status_for_result(official_url, careers_url, confidence, feed_found)

        base_result.update(
            {
                "Official Website": official_url,
                "Careers Page URL": careers_url,
                "Jobs RSS Feed URL": feed_url,
                "Job Platform": platform,
                "Feed Found": bool(feed_found),
                "Search Status": status,
                "Confidence": confidence,
                "Notes": combine_notes(company.get("Notes", ""), website_notes, careers_notes, feed_notes),
            }
        )
    except Exception as exc:
        logger.exception("Failed to enrich %s", name)
        base_result["Notes"] = combine_notes(company.get("Notes", ""), f"enrichment failed: {exc}")

    return base_result


def main() -> int:
    configure_logging()
    args = parse_args()
    input_path = args.input.resolve()
    output_path = args.output.resolve()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if not input_path.exists() and input_path == DEFAULT_INPUT.resolve():
        logging.info("Sample input workbook missing; creating %s", input_path)
        create_sample_workbook(input_path)

    if not input_path.exists():
        raise FileNotFoundError(f"Input workbook does not exist: {input_path}")

    companies = read_companies(input_path)
    logging.info("Loaded %s companies from %s", len(companies), input_path)

    checked_at = datetime.now().astimezone().replace(microsecond=0).isoformat()
    results = [enrich_company(company, checked_at) for company in companies]
    write_results(output_path, results)
    logging.info("Wrote enriched workbook to %s", output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
