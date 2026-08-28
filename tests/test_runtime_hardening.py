from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock, patch

from pydantic import ValidationError

import main
import server
from backend import utility_tasks
from backend.operation_gate import GLOBAL_MUTATION_GATE, MutationGate, OperationConflictError
from backend.repository import OpportunityRepository
from backend.utility_runs import UtilityRunManager
from config import APP_MAX_BROWSER_WORKERS, APP_MAX_HTTP_WORKERS


class RuntimeHardeningTests(unittest.TestCase):
    def test_repository_reads_do_not_initialize_schema_or_wait_for_active_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "radar.db"
            OpportunityRepository(database, initialize=True)
            writer = sqlite3.connect(database)
            writer.execute("BEGIN IMMEDIATE")
            completed = Event()
            result: list[object] = []

            def read_companies() -> None:
                try:
                    result.append(
                        OpportunityRepository(
                            database, require_existing=True
                        ).list_companies()
                    )
                except BaseException as exc:
                    result.append(exc)
                finally:
                    completed.set()

            reader = Thread(target=read_companies)
            reader.start()
            try:
                self.assertTrue(
                    completed.wait(timeout=0.5),
                    "A read-only repository request waited for the active writer.",
                )
            finally:
                writer.rollback()
                writer.close()
                reader.join(timeout=2)

            self.assertEqual(result, [[]])

    def test_lazy_utility_manager_construction_is_nonmutating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "radar.db"
            UtilityRunManager(database)
            writer = sqlite3.connect(database)
            writer.execute("BEGIN IMMEDIATE")
            completed = Event()
            result: list[object] = []

            def read_runs() -> None:
                try:
                    manager = UtilityRunManager(database, initialize=False, reconcile=False)
                    result.append(manager.list_runs())
                except BaseException as exc:
                    result.append(exc)
                finally:
                    completed.set()

            reader = Thread(target=read_runs)
            reader.start()
            try:
                self.assertTrue(
                    completed.wait(timeout=0.5),
                    "Lazy maintenance status construction waited for the active writer.",
                )
            finally:
                writer.rollback()
                writer.close()
                reader.join(timeout=2)

            self.assertEqual(result, [[]])

    def test_oversized_request_chunk_is_rejected_before_buffering(self) -> None:
        extended = False

        class StreamingRequest:
            headers: dict[str, str] = {}

            async def stream(self):
                yield b"12345"

        class TrackingBytearray(bytearray):
            def extend(self, chunk: bytes) -> None:
                nonlocal extended
                extended = True
                super().extend(chunk)

        with patch.object(server, "bytearray", TrackingBytearray, create=True):
            with self.assertRaises(server.HTTPException) as raised:
                asyncio.run(
                    server.read_limited_request_body(
                        StreamingRequest(),
                        maximum_bytes=4,
                        limit_message="The upload is too large.",
                    )
                )

        self.assertEqual(raised.exception.status_code, 413)
        self.assertEqual(raised.exception.detail, "The upload is too large.")
        self.assertFalse(extended)

    def test_api_and_cli_worker_counts_are_bounded(self) -> None:
        with self.assertRaises(ValidationError):
            server.UtilityRequest(maxWorkers=APP_MAX_HTTP_WORKERS + 1)
        with self.assertRaises(ValidationError):
            server.UtilityRequest(browserWorkers=APP_MAX_BROWSER_WORKERS + 1)
        with self.assertRaises(ValidationError):
            server.UtilityRequest(maxWorkers=0)
        self.assertEqual(main.clamp_http_workers(-100), 1)
        self.assertEqual(main.clamp_http_workers(10_000), APP_MAX_HTTP_WORKERS)
        self.assertEqual(main.clamp_browser_workers(-100), 1)
        self.assertEqual(main.clamp_browser_workers(10_000), APP_MAX_BROWSER_WORKERS)

    def test_browser_disabled_flag_blocks_every_browser_discovery_path(self) -> None:
        with patch.object(main, "APP_ENABLE_BROWSER_JOBS", False):
            with self.assertRaisesRegex(RuntimeError, "APP_ENABLE_BROWSER_JOBS"):
                main.bootstrap_enrich(
                    Path("unused.xlsx"),
                    Path("unused-master.xlsx"),
                    Path("unused.json"),
                    use_browser_discovery=True,
                )

        with patch.object(server, "APP_ENABLE_COMPANY_REFRESH", True), patch.object(
            server, "APP_ENABLE_DISCOVERY", True
        ), patch.object(server, "APP_ENABLE_BROWSER_JOBS", False):
            self.assertIn(
                "Browser job collection",
                server._disabled_action_reason("refresh-company-discovery"),
            )

        repository = Mock()
        repository.get_company.return_value = {
            "id": "company-1",
            "name": "Example Bank",
            "officialWebsite": "",
            "knownWebsite": "",
        }
        repository.update_discovered_company_fields.return_value = repository.get_company.return_value
        with patch.object(utility_tasks, "APP_ENABLE_BROWSER_JOBS", False), patch.object(
            utility_tasks,
            "enrich_company",
            return_value={"Company ID": "company-1", "Company Name": "Example Bank", "Search Status": "Completed"},
        ) as enrich:
            utility_tasks.refresh_single_company_information(repository, "company-1")
        self.assertFalse(enrich.call_args.kwargs["use_browser_discovery"])

    def test_different_background_mutations_conflict_and_reads_continue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gate = MutationGate()
            manager = UtilityRunManager(Path(temporary) / "radar.db", mutation_gate=gate)
            release = Event()

            first = manager.start(
                action="collection",
                task_name="Collection",
                progress_verb="Checking",
                progress_unit="companies",
                worker=lambda _progress, _cancelled: release.wait(timeout=2) or {},
                format_summary=lambda _summary: "Done.",
            )
            self.assertEqual(manager.get(first["id"])["id"], first["id"])
            self.assertGreaterEqual(len(manager.list_runs()), 1)
            with self.assertRaisesRegex(OperationConflictError, "Another mutating operation"):
                manager.start(
                    action="export",
                    task_name="Export",
                    progress_verb="Exporting",
                    progress_unit="files",
                    worker=lambda *_args: {},
                    format_summary=lambda _summary: "Done.",
                )
            release.set()
            wait_for_terminal(manager, first["id"])

    def test_gate_cleans_up_after_worker_exception_and_error_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gate = MutationGate()
            manager = UtilityRunManager(Path(temporary) / "radar.db", mutation_gate=gate)
            failed = manager.start(
                action="import",
                task_name="Import",
                progress_verb="Importing",
                progress_unit="rows",
                worker=lambda *_args: (_ for _ in ()).throw(RuntimeError(r"secret C:\private\resume.docx")),
                format_summary=lambda _summary: "Done.",
            )
            terminal = wait_for_terminal(manager, failed["id"])
            self.assertEqual(terminal["status"], "Failed")
            self.assertNotIn("private", terminal["error"])
            self.assertIsNone(gate.active_status())
            next_run = manager.start(
                action="export",
                task_name="Export",
                progress_verb="Exporting",
                progress_unit="files",
                worker=lambda *_args: {},
                format_summary=lambda _summary: "Done.",
            )
            self.assertEqual(wait_for_terminal(manager, next_run["id"])["status"], "Completed")

    def test_gate_cleans_up_when_initial_state_bookkeeping_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gate = MutationGate()
            manager = UtilityRunManager(Path(temporary) / "radar.db", mutation_gate=gate)
            original_update = manager._update_run
            calls = 0

            def fail_first_update(run_id: str, **updates):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("simulated state write failure")
                return original_update(run_id, **updates)

            with patch.object(manager, "_update_run", side_effect=fail_first_update):
                run = manager.start(
                    action="collection",
                    task_name="Collection",
                    progress_verb="Checking",
                    progress_unit="companies",
                    worker=lambda *_args: {},
                    format_summary=lambda _summary: "Done.",
                )
                terminal = wait_for_terminal(manager, run["id"])

            self.assertEqual(terminal["status"], "Failed")
            self.assertIsNone(gate.active_status())

    def test_shutdown_prevents_new_work_and_requests_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gate = MutationGate()
            manager = UtilityRunManager(Path(temporary) / "radar.db", mutation_gate=gate)

            def cooperative(_progress, cancelled):
                while not cancelled.wait(0.005):
                    pass
                raise InterruptedError("cancelled")

            run = manager.start(
                action="refresh",
                task_name="Refresh",
                progress_verb="Checking",
                progress_unit="companies",
                worker=cooperative,
                format_summary=lambda _summary: "Done.",
            )
            manager.shutdown(join_timeout=1)
            self.assertEqual(wait_for_terminal(manager, run["id"])["status"], "Cancelled")
            with self.assertRaisesRegex(OperationConflictError, "shutting down"):
                manager.start(
                    action="export",
                    task_name="Export",
                    progress_verb="Exporting",
                    progress_unit="files",
                    worker=lambda *_args: {},
                    format_summary=lambda _summary: "Done.",
                )

    def test_pre_start_cancellation_runs_cleanup_without_invoking_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gate = MutationGate()
            manager = UtilityRunManager(Path(temporary) / "radar.db", mutation_gate=gate)
            created_threads = []
            worker = Mock(return_value={})
            cleanup = Mock()

            class ManualThread:
                def __init__(self, *, target, args, name, daemon):
                    self.target = target
                    self.args = args
                    self.name = name
                    self.daemon = daemon
                    created_threads.append(self)

                def start(self):
                    return None

                def run(self):
                    self.target(*self.args)

            with patch("backend.utility_runs.threading.Thread", ManualThread):
                run = manager.start(
                    action="import",
                    task_name="Import",
                    progress_verb="Importing",
                    progress_unit="records",
                    worker=worker,
                    format_summary=lambda _summary: "Done.",
                    after_finish=cleanup,
                )
                manager.cancel(run["id"])
                created_threads[0].run()

            self.assertEqual(manager.get(run["id"])["status"], "Cancelled")
            worker.assert_not_called()
            cleanup.assert_called_once_with()
            self.assertIsNone(gate.active_status())

    def test_read_only_endpoint_remains_available_during_global_mutation(self) -> None:
        lease = GLOBAL_MUTATION_GATE.acquire("test-operation")
        try:
            fake_repository = Mock()
            fake_repository.list_jobs.return_value = [{"id": "job-1"}]
            with patch.object(server, "repository", return_value=fake_repository):
                self.assertEqual(server.list_jobs_endpoint(), [{"id": "job-1"}])
        finally:
            lease.release()

    def test_lazy_utility_manager_initialization_is_singleton_under_concurrency(self) -> None:
        previous = server._utility_run_manager
        created: list[object] = []

        class FakeManager:
            def __init__(self, _path, *, initialize, reconcile, require_existing):
                self.initialize = initialize
                self.reconcile = reconcile
                self.require_existing = require_existing
                time.sleep(0.02)
                created.append(self)

        try:
            server._utility_run_manager = None
            with patch.object(server, "validate_auth_configuration"), patch.object(
                server, "UtilityRunManager", FakeManager
            ):
                with ThreadPoolExecutor(max_workers=8) as executor:
                    managers = list(executor.map(lambda _index: server.utility_runs(), range(16)))
            self.assertEqual(len(created), 1)
            self.assertTrue(all(manager is created[0] for manager in managers))
            self.assertFalse(created[0].initialize)
            self.assertFalse(created[0].reconcile)
            self.assertEqual(created[0].require_existing, server.REQUIRE_EXISTING_DATABASE)
        finally:
            server._utility_run_manager = previous


def wait_for_terminal(manager: UtilityRunManager, run_id: str) -> dict:
    for _ in range(500):
        run = manager.get(run_id)
        if run["status"] in {"Completed", "Cancelled", "Failed"}:
            return run
        time.sleep(0.01)
    raise AssertionError("Run did not reach a terminal state.")


if __name__ == "__main__":
    unittest.main()
