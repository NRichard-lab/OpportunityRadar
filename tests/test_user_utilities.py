from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from backend.exports import SnapshotExporter
from backend.maintenance_scheduler import MaintenanceScheduler
from backend.repository import OpportunityRepository
from backend.utility_runs import UtilityRunManager
from backend.utility_tasks import UtilityCancelled, import_data_file
import server


class UserUtilityTests(unittest.TestCase):
    def test_discovery_fills_blanks_without_overwriting_user_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = OpportunityRepository(Path(temp_dir) / "radar.db", initialize=True)
            company = repository.create_company({
                "name": "User Entered Bank", "companyWebsite": "https://user.example",
                "careersPageUrl": "", "jobBoardUrl": "", "industry": "Financial Services",
                "city": "Denver", "state": "", "country": "United States", "notes": "",
            })
            updated = repository.update_discovered_company_fields(company["id"], {
                "officialWebsite": "https://replacement.example", "city": "Boulder", "state": "CO",
                "foundedYear": 1910, "totalAssets": 2_500_000_000, "lastChecked": "2026-08-23T12:00:00Z",
            })
            self.assertEqual(updated["officialWebsite"], "https://user.example")
            self.assertEqual(updated["city"], "Denver")
            self.assertEqual(updated["state"], "CO")
            self.assertEqual(updated["foundedYear"], 1910)
            self.assertEqual(updated["totalAssets"], 2_500_000_000)

    def test_background_runs_report_progress_and_cancel(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        manager = UtilityRunManager(Path(temp_dir.name) / "radar.db")

        def completed_worker(progress, cancelled):
            progress(1, 2, "First Bank")
            progress(2, 2, "Second Bank")
            return {"updated": 2}

        started = manager.start(
            action="test", task_name="Test Run", progress_verb="Checking", progress_unit="companies",
            worker=completed_worker, format_summary=lambda summary: f"Updated {summary['updated']} companies.",
        )
        completed = wait_for_terminal(manager, started["id"])
        self.assertEqual(completed["status"], "Completed")
        self.assertEqual(completed["progressText"], "Checking 2 of 2 companies")
        self.assertEqual(completed["currentCompany"], "Second Bank")

        def cancellable_worker(progress, cancelled):
            for index in range(100):
                if cancelled.is_set():
                    raise UtilityCancelled()
                progress(index + 1, 100, f"Company {index + 1}")
                time.sleep(0.005)
            return {}

        started = manager.start(
            action="cancel", task_name="Cancel Run", progress_verb="Checking", progress_unit="companies",
            worker=cancellable_worker, format_summary=lambda summary: "Done.",
        )
        manager.cancel(started["id"])
        cancelled = wait_for_terminal(manager, started["id"])
        self.assertEqual(cancelled["status"], "Cancelled")

    def test_maintenance_runs_persist_serialize_unrelated_and_recover_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "radar.db"
            manager = UtilityRunManager(database)
            release = Event()

            def held_worker(progress, cancelled):
                progress(2, 5, "Second Bank")
                release.wait(timeout=2)
                return {"updated": 2}

            first = manager.start(
                action="refresh-jobs", task_name="Refresh Jobs", progress_verb="Checking",
                progress_unit="companies", worker=held_worker, format_summary=lambda _: "Done.",
            )
            for _ in range(100):
                active = manager.get(first["id"])
                if active["current"] == 2:
                    break
                time.sleep(0.005)
            self.assertEqual(active["status"], "Running")
            self.assertEqual(active["progress"], 40)
            with closing(sqlite3.connect(database)) as connection:
                persisted = connection.execute(
                    "SELECT status,progress_current,progress_total,current_item FROM maintenance_job_runs WHERE id=?",
                    (first["id"],),
                ).fetchone()
            self.assertEqual(persisted, ("Running", 2, 5, "Second Bank"))

            with self.assertRaisesRegex(RuntimeError, "Another mutating operation"):
                manager.start(
                    action="refresh-jobs", task_name="Refresh Jobs", progress_verb="Checking",
                    progress_unit="companies", worker=held_worker, format_summary=lambda _: "Done.",
                )

            with self.assertRaisesRegex(RuntimeError, "Another mutating operation"):
                manager.start(
                    action="export-data", task_name="Export Data", progress_verb="Exporting",
                    progress_unit="data sets", worker=lambda progress, cancelled: {"exported": 1},
                    format_summary=lambda _: "Export complete.",
                )
            release.set()
            completed = wait_for_terminal(manager, first["id"])
            self.assertEqual(completed["id"], first["id"])
            self.assertGreaterEqual(completed["runtimeSeconds"], 0)
            unrelated = manager.start(
                action="export-data", task_name="Export Data", progress_verb="Exporting",
                progress_unit="data sets", worker=lambda progress, cancelled: {"exported": 1},
                format_summary=lambda _: "Export complete.",
            )
            self.assertEqual(wait_for_terminal(manager, unrelated["id"])["status"], "Completed")

            with closing(sqlite3.connect(database)) as connection:
                now = "2026-08-25T12:00:00+00:00"
                connection.execute(
                    """INSERT INTO maintenance_job_runs
                    (id,job_key,task_name,status,current_message,started_at,created_at,updated_at)
                    VALUES ('left-running','backup','Create Backup','Running','Running',?,?,?)""",
                    (now, now, now),
                )
                connection.commit()
            restarted = UtilityRunManager(database)
            interrupted = restarted.get("left-running")
            self.assertEqual(interrupted["status"], "Failed")
            self.assertIn("backend restarted", interrupted["error"].lower())

    def test_daily_scheduler_runs_persists_and_skips_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "radar.db"
            manager = UtilityRunManager(database)
            executions: list[str] = []

            def scheduled_worker(progress, cancelled):
                executions.append("ran")
                progress(1, 1, "Database")
                return {"filesBackedUp": 1}

            def failed_follow_up(summary):
                executions.append("email")
                raise RuntimeError("email unavailable")

            def registry():
                return {"create-backup": {
                    "task_name": "Create Backup", "progress_verb": "Backing up",
                    "progress_unit": "files", "worker": scheduled_worker,
                    "format_summary": lambda summary: "Backup complete.",
                    "description": "Create a backup.", "supports_scheduling": True,
                    "after_scheduled_success": failed_follow_up,
                }}

            scheduler = MaintenanceScheduler(database, manager, registry, poll_seconds=0.01)
            schedule = scheduler.update_schedule(
                "create-backup", enabled=True, run_time="23:59", timezone="America/Denver"
            )
            self.assertTrue(schedule["enabled"])
            due_time = datetime(2026, 8, 26, 5, 59, tzinfo=timezone.utc)
            outcomes = scheduler.run_due_once(due_time)
            self.assertEqual(len(outcomes), 1)
            completed = wait_for_terminal(manager, outcomes[0]["id"])
            self.assertEqual(completed["triggerType"], "scheduled")
            self.assertEqual(completed["status"], "Completed")
            self.assertEqual(executions, ["ran", "email"])
            self.assertEqual(completed["summary"]["scheduledFollowUp"]["status"], "Failed")
            self.assertEqual(scheduler.run_due_once(due_time), [])

            restarted = MaintenanceScheduler(database, manager, registry)
            self.assertTrue(restarted.list_schedules()["create-backup"]["enabled"])
            restarted.update_schedule(
                "create-backup", enabled=False, run_time="23:59", timezone="America/Denver"
            )
            next_day = datetime(2026, 8, 27, 5, 59, tzinfo=timezone.utc)
            self.assertEqual(restarted.run_due_once(next_day), [])

            release = Event()

            def held_worker(progress, cancelled):
                release.wait(timeout=2)
                return {}

            held = manager.start(
                action="create-backup", task_name="Create Backup", progress_verb="Backing up",
                progress_unit="files", worker=held_worker,
                format_summary=lambda summary: "Done.", trigger_type="manual",
            )
            restarted.update_schedule(
                "create-backup", enabled=True, run_time="23:59", timezone="America/Denver"
            )
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "UPDATE maintenance_schedules SET last_scheduled_date='' WHERE job_key='create-backup'"
                )
                connection.commit()
            skipped = restarted.run_due_once(next_day)
            self.assertEqual(skipped[0]["status"], "Skipped")
            self.assertEqual(skipped[0]["triggerType"], "scheduled")
            self.assertIn("another mutating operation", skipped[0]["error"])
            self.assertEqual(
                restarted.list_schedules()["create-backup"]["lastScheduledDate"],
                "",
            )
            release.set()
            wait_for_terminal(manager, held["id"])
            wait_for_idle(manager)

            retry = restarted.run_due_once(next_day)
            self.assertEqual(len(retry), 1)
            retried = wait_for_terminal(manager, retry[0]["id"])
            self.assertEqual(retried["status"], "Completed")
            self.assertEqual(retried["triggerType"], "scheduled")
            self.assertEqual(
                restarted.list_schedules()["create-backup"]["lastScheduledDate"],
                "2026-08-26",
            )
            self.assertEqual(restarted.run_due_once(next_day), [])

    def test_scheduler_records_disabled_action_without_starting_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "radar.db"
            manager = UtilityRunManager(database)
            executions: list[str] = []

            def registry():
                return {"refresh-all-job-listings": {
                    "task_name": "Refresh All Job Listings", "progress_verb": "Checking",
                    "progress_unit": "companies",
                    "worker": lambda progress, cancelled: executions.append("unsafe") or {},
                    "format_summary": lambda summary: "Done.", "supports_scheduling": True,
                    "enabled": False, "disabled_reason": "Browser job collection is disabled for this release.",
                }}

            scheduler = MaintenanceScheduler(database, manager, registry)
            scheduler.update_schedule(
                "refresh-all-job-listings", enabled=True, run_time="00:00", timezone="UTC"
            )
            outcomes = scheduler.run_due_once(datetime.now(timezone.utc) + timedelta(days=1))

            self.assertEqual(executions, [])
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["status"], "Skipped")
            self.assertIn("disabled", outcomes[0]["error"].lower())

    def test_scheduler_claim_rejects_stale_schedule_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "radar.db"
            manager = UtilityRunManager(database)
            executions: list[str] = []

            def registry():
                return {"create-backup": {
                    "task_name": "Create Backup", "progress_verb": "Backing up",
                    "progress_unit": "files",
                    "worker": lambda progress, cancelled: executions.append("ran") or {},
                    "format_summary": lambda summary: "Done.", "supports_scheduling": True,
                }}

            scheduler = MaintenanceScheduler(database, manager, registry)
            scheduler.update_schedule("create-backup", enabled=True, run_time="09:00", timezone="UTC")
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "UPDATE maintenance_schedules SET last_scheduled_date='' WHERE job_key='create-backup'"
                )
                connection.commit()
            original_claim = scheduler._claim_occurrence

            def edit_before_claim(*args, **kwargs):
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute(
                        "UPDATE maintenance_schedules SET run_time='11:00',updated_at='new-revision' "
                        "WHERE job_key='create-backup'"
                    )
                    connection.commit()
                return original_claim(*args, **kwargs)

            with patch.object(scheduler, "_claim_occurrence", side_effect=edit_before_claim):
                outcomes = scheduler.run_due_once(datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc))

            self.assertEqual(outcomes, [])
            self.assertEqual(executions, [])
            self.assertEqual(scheduler.list_schedules()["create-backup"]["runTime"], "11:00")

    def test_concurrent_schedule_edit_waits_for_failed_claim_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "radar.db"
            manager = UtilityRunManager(database)

            def definition(name: str):
                return {
                    "task_name": name, "progress_verb": "Running", "progress_unit": "items",
                    "worker": lambda progress, cancelled: {},
                    "format_summary": lambda summary: "Done.", "supports_scheduling": True,
                }

            scheduler = MaintenanceScheduler(
                database,
                manager,
                lambda: {"first": definition("First"), "second": definition("Second")},
            )
            scheduler.ensure_schedule_rows()
            scheduler.update_schedule("first", enabled=True, run_time="00:00", timezone="UTC")
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE maintenance_schedules SET last_scheduled_date='' WHERE job_key='first'")
                connection.commit()

            entered_start = Event()
            edit_attempted = Event()
            edit_finished = Event()

            def edit_other_schedule() -> None:
                entered_start.wait(timeout=2)
                edit_attempted.set()
                scheduler.update_schedule("second", enabled=False, run_time="01:00", timezone="UTC")
                edit_finished.set()

            editor = Thread(target=edit_other_schedule)
            editor.start()

            def reject_start(**kwargs):
                del kwargs
                entered_start.set()
                self.assertTrue(edit_attempted.wait(timeout=2))
                self.assertFalse(edit_finished.wait(timeout=0.1))
                raise RuntimeError("Another mutating operation is active.")

            with patch.object(manager, "start", side_effect=reject_start):
                outcomes = scheduler.run_due_once(datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc))
            editor.join(timeout=2)

            self.assertFalse(editor.is_alive())
            self.assertTrue(edit_finished.is_set())
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["status"], "Skipped")
            self.assertEqual(scheduler.list_schedules()["first"]["lastScheduledDate"], "")

    def test_scheduler_releases_claim_when_start_raises_unexpected_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "radar.db"
            manager = UtilityRunManager(database)
            scheduler = MaintenanceScheduler(database, manager, lambda: {"backup": {
                "task_name": "Backup", "progress_verb": "Backing up", "progress_unit": "files",
                "worker": lambda progress, cancelled: {}, "format_summary": lambda summary: "Done.",
                "supports_scheduling": True,
            }})
            scheduler.update_schedule("backup", enabled=True, run_time="00:00", timezone="UTC")
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE maintenance_schedules SET last_scheduled_date='' WHERE job_key='backup'")
                connection.commit()

            with patch.object(manager, "start", side_effect=OSError("simulated start failure")):
                with self.assertRaisesRegex(OSError, "simulated start failure"):
                    scheduler.run_due_once(datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc))

            self.assertEqual(scheduler.list_schedules()["backup"]["lastScheduledDate"], "")

    def test_json_import_adds_records_without_removing_existing_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = OpportunityRepository(root / "radar.db", initialize=True)
            existing = repository.create_company({"name": "Existing Bank"})
            exporter = make_exporter(repository, root)
            source = root / "import.json"
            source.write_text(json.dumps([{"id": "company-imported", "name": "Imported Bank", "state": "CO"}]), encoding="utf-8")
            summary = import_data_file(repository, exporter, source, lambda *_: None, Event())
            self.assertEqual(summary["companiesImported"], 1)
            self.assertEqual(repository.get_company(existing["id"])["name"], "Existing Bank")
            self.assertEqual(repository.get_company("company-imported")["state"], "CO")

    def test_single_company_refresh_updates_only_target_company_and_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = OpportunityRepository(root / "radar.db", initialize=True)
            target = repository.create_company({"name": "Target Bank", "jobBoardUrl": "https://jobs.target.example"})
            other = repository.create_company({"name": "Other Bank", "jobBoardUrl": "https://jobs.other.example"})
            repository.replace_jobs([
                {"id": "target-update", "companyId": target["id"], "companyName": target["name"], "title": "Analyst", "sourceUrl": "https://jobs.target.example/1", "description": "Old", "status": "Open"},
                {"id": "target-remove", "companyId": target["id"], "companyName": target["name"], "title": "Teller", "sourceUrl": "https://jobs.target.example/2", "description": "Old", "status": "Open"},
                {"id": "other-keep", "companyId": other["id"], "companyName": other["name"], "title": "Manager", "sourceUrl": "https://jobs.other.example/1", "description": "Keep", "status": "Open"},
            ])
            exporter = make_exporter(repository, root)
            jobs_json = root / "jobs.json"

            def fake_company_refresh(repo, company_id):
                company = repo.update_discovered_company_fields(company_id, {"city": "Denver"})
                return {"company": company, "metadataChanged": True, "warnings": []}

            def fake_collect_jobs(**kwargs):
                self.assertEqual(kwargs["company_ids"], {target["id"]})
                jobs_json.write_text(json.dumps([
                    {"id": "target-update", "companyId": target["id"], "companyName": target["name"], "title": "Analyst", "sourceUrl": "https://jobs.target.example/1", "description": "Updated", "status": "Open"},
                    {"id": "target-new", "companyId": target["id"], "companyName": target["name"], "title": "Specialist", "sourceUrl": "https://jobs.target.example/3", "description": "New", "status": "Open"},
                ]), encoding="utf-8")
                return {"companies_attempted": 1, "jobs_found": 2, "jobs_saved": 2, "errors": 0}

            with (
                patch.object(server, "repository", return_value=repository),
                patch.object(server, "exporter", return_value=exporter),
                patch.object(server, "refresh_single_company_information", side_effect=fake_company_refresh),
                patch.object(server, "collect_jobs", side_effect=fake_collect_jobs),
                patch.object(server, "DEFAULT_JOBS_JSON", jobs_json),
                patch.object(server, "DEFAULT_JOBS_XLSX", root / "jobs.xlsx"),
                patch.object(server, "DEFAULT_MASTER", root / "master.xlsx"),
                patch.object(server, "LOG_DIR", root / "logs"),
            ):
                result = server.run_single_company_refresh(target["id"])

            self.assertTrue(result["companyMetadataChanged"])
            self.assertEqual((result["totalJobsDiscovered"], result["newJobs"], result["updatedJobs"], result["removedOrClosedJobs"]), (2, 1, 1, 1))
            self.assertEqual(repository.get_company(target["id"])["city"], "Denver")
            other_jobs = [job for job in repository.list_jobs() if job["companyId"] == other["id"]]
            self.assertEqual([(job["id"], job["description"]) for job in other_jobs], [("other-keep", "Keep")])


def wait_for_terminal(manager: UtilityRunManager, run_id: str) -> dict:
    for _ in range(500):
        run = manager.get(run_id)
        if run["status"] in {"Completed", "Cancelled", "Failed"}:
            return run
        time.sleep(0.01)
    raise AssertionError("Utility run did not finish.")


def wait_for_idle(manager: UtilityRunManager) -> None:
    for _ in range(500):
        if manager._mutation_gate.active_status() is None:
            return
        time.sleep(0.01)
    raise AssertionError("Utility manager did not become idle.")


def make_exporter(repository: OpportunityRepository, root: Path) -> SnapshotExporter:
    return SnapshotExporter(
        repository, master_path=root / "master.xlsx", companies_json_path=root / "companies.json",
        frontend_companies_json_path=root / "frontend-companies.json", jobs_json_path=root / "jobs.json",
        frontend_jobs_json_path=root / "frontend-jobs.json", applications_json_path=root / "applications.json",
        jobs_xlsx_path=root / "jobs.xlsx", write_frontend_mirrors=False,
    )


if __name__ == "__main__":
    unittest.main()
