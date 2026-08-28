from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event, Lock
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.db import connect, initialize_schema
from backend.health import (
    DEGRADED,
    HEALTHY,
    NOT_CHECKED,
    ReadinessCache,
    ReadinessResult,
    UNHEALTHY,
    _check_directory,
    check_readiness,
)
import server


class ReadinessChecksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.data_dir = self.root / "data"
        self.export_dir = self.root / "exports"
        self.data_dir.mkdir()
        self.export_dir.mkdir()
        self.database_path = self.data_dir / "opportunity_radar.db"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def initialize_database(self) -> None:
        with closing(connect(self.database_path)) as connection:
            initialize_schema(connection)

    def check(self, *, export_dir: Path | None = None):
        return check_readiness(
            database_path=self.database_path,
            data_dir=self.data_dir,
            export_dir=self.export_dir if export_dir is None else export_dir,
            version="release-849e139",
            checked_at=datetime(2026, 8, 27, 12, 30, tzinfo=UTC),
        )

    def test_healthy_report_checks_schema_read_write_and_cleans_up(self) -> None:
        self.initialize_database()
        with closing(sqlite3.connect(self.database_path)) as connection:
            tables_before = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()

        result = self.check()

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.payload["status"], HEALTHY)
        self.assertEqual(result.payload["service"], "opportunity-radar")
        self.assertEqual(result.payload["version"], "release-849e139")
        self.assertEqual(result.payload["checkedAt"], "2026-08-27T12:30:00Z")
        self.assertEqual(
            result.payload["components"]["database"]["checks"],
            {"exists": HEALTHY, "read": HEALTHY, "schema": HEALTHY, "write": HEALTHY},
        )
        self.assertEqual(result.payload["components"]["dataStorage"]["status"], HEALTHY)
        self.assertEqual(result.payload["components"]["exportStorage"]["status"], HEALTHY)

        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT key FROM settings WHERE key LIKE '__health_probe__:%'"
                ).fetchall(),
                [],
            )
            self.assertEqual(
                connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall(),
                tables_before,
            )
        self.assertEqual(list(self.data_dir.glob(".opportunity-radar-health-*")), [])
        self.assertEqual(list(self.export_dir.glob(".opportunity-radar-health-*")), [])

    def test_missing_database_is_unhealthy_and_is_not_created(self) -> None:
        result = self.check()

        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.payload["status"], UNHEALTHY)
        database = result.payload["components"]["database"]
        self.assertEqual(database["checks"]["exists"], UNHEALTHY)
        self.assertEqual(database["checks"]["read"], NOT_CHECKED)
        self.assertFalse(self.database_path.exists())

    def test_database_disappearance_changes_health_to_unhealthy_without_replacement(self) -> None:
        self.initialize_database()
        cache = ReadinessCache(ttl_seconds=60)
        first = cache.get(
            database_path=self.database_path,
            data_dir=self.data_dir,
            export_dir=self.export_dir,
            version="database-disappearance-test",
        )
        self.assertEqual(first.status_code, 200)

        self.database_path.unlink()
        cache.clear()
        second = cache.get(
            database_path=self.database_path,
            data_dir=self.data_dir,
            export_dir=self.export_dir,
            version="database-disappearance-test",
        )

        self.assertEqual(second.status_code, 503)
        self.assertEqual(second.payload["components"]["database"]["checks"]["exists"], UNHEALTHY)
        self.assertFalse(self.database_path.exists())

    def test_incomplete_schema_is_unhealthy_and_not_migrated(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute("PRAGMA user_version = 6")
            connection.commit()

        result = self.check()

        self.assertEqual(result.status_code, 503)
        database = result.payload["components"]["database"]
        self.assertEqual(database["checks"]["read"], HEALTHY)
        self.assertEqual(database["checks"]["schema"], UNHEALTHY)
        self.assertEqual(database["checks"]["write"], NOT_CHECKED)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                [("settings",)],
            )

    def test_core_table_with_missing_required_columns_is_unhealthy(self) -> None:
        self.initialize_database()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("DROP TABLE companies")
            connection.execute("CREATE TABLE companies (fake TEXT)")
            connection.commit()

        result = self.check()

        self.assertEqual(result.status_code, 503)
        database = result.payload["components"]["database"]
        self.assertEqual(database["checks"]["read"], HEALTHY)
        self.assertEqual(database["checks"]["schema"], UNHEALTHY)
        self.assertEqual(database["checks"]["write"], NOT_CHECKED)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA table_info(companies)").fetchall()[0][1],
                "fake",
            )

    def test_missing_required_data_directory_is_unhealthy_and_not_created(self) -> None:
        self.initialize_database()
        missing_data_dir = self.root / "missing-data"

        result = check_readiness(
            database_path=self.database_path,
            data_dir=missing_data_dir,
            export_dir=self.export_dir,
            version="test",
        )

        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.payload["components"]["dataStorage"]["status"], UNHEALTHY)
        self.assertFalse(missing_data_dir.exists())

    def test_missing_optional_export_directory_is_degraded_and_not_created(self) -> None:
        self.initialize_database()
        missing_export_dir = self.root / "missing-exports"

        result = self.check(export_dir=missing_export_dir)

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.payload["status"], DEGRADED)
        self.assertEqual(result.payload["components"]["exportStorage"]["status"], DEGRADED)
        self.assertFalse(missing_export_dir.exists())

    def test_directory_probe_removes_file_when_write_fails(self) -> None:
        secret_error = "do not expose C:/private/runtime/path"
        with patch("backend.health.os.fsync", side_effect=OSError(secret_error)):
            component = _check_directory(self.data_dir, required=True)

        self.assertEqual(component["status"], UNHEALTHY)
        self.assertEqual(list(self.data_dir.glob(".opportunity-radar-health-*")), [])
        self.assertNotIn(secret_error, json.dumps(component))

    def test_response_sanitizes_version_and_never_exposes_database_errors(self) -> None:
        self.database_path.write_bytes(b"not-a-sqlite-database C:/private/secret")

        result = check_readiness(
            database_path=self.database_path,
            data_dir=self.data_dir,
            export_dir=self.export_dir,
            version="C:/deployments/private/release",
        )
        serialized = json.dumps(result.payload)

        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.payload["version"], "unknown")
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("private/secret", serialized)
        self.assertNotIn("file is not a database", serialized)

    def test_readiness_check_makes_no_network_requests(self) -> None:
        self.initialize_database()
        with patch("socket.create_connection", side_effect=AssertionError("network is forbidden")), patch(
            "requests.sessions.Session.request", side_effect=AssertionError("HTTP is forbidden")
        ):
            result = self.check()

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.payload["status"], HEALTHY)


class HealthEndpointTests(unittest.TestCase):
    def test_endpoint_delegates_without_repository_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            export_dir = root / "exports"
            data_dir.mkdir()
            export_dir.mkdir()
            database_path = data_dir / "opportunity_radar.db"
            with closing(connect(database_path)) as connection:
                initialize_schema(connection)

            with patch.multiple(
                server,
                DEFAULT_DATABASE=database_path,
                DATA_DIR=data_dir,
                EXPORT_DIR=export_dir,
                DEPLOYMENT_VERSION="test-release",
                _health_readiness_cache=ReadinessCache(ttl_seconds=60),
            ), patch.object(server, "repository", side_effect=AssertionError("repository must not be called")):
                response = server.health_endpoint()

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(payload["status"], HEALTHY)
        self.assertEqual(payload["version"], "test-release")

    def test_repeated_endpoint_calls_reuse_cached_readiness_result(self) -> None:
        result = ReadinessResult(
            status_code=503,
            payload={"status": UNHEALTHY, "service": "opportunity-radar", "components": {}},
        )
        calls = 0

        def checker(**_kwargs: object) -> ReadinessResult:
            nonlocal calls
            calls += 1
            return result

        cache = ReadinessCache(ttl_seconds=60, checker=checker)
        with tempfile.TemporaryDirectory() as temp_dir, patch.multiple(
            server,
            DEFAULT_DATABASE=Path(temp_dir) / "missing.db",
            DATA_DIR=Path(temp_dir),
            EXPORT_DIR=Path(temp_dir),
            DEPLOYMENT_VERSION="cache-test",
            _health_readiness_cache=cache,
        ):
            first = server.health_endpoint()
            second = server.health_endpoint()

        self.assertEqual(first.status_code, 503)
        self.assertEqual(second.status_code, 503)
        self.assertEqual(calls, 1)


class ReadinessCacheTests(unittest.TestCase):
    @staticmethod
    def result() -> ReadinessResult:
        return ReadinessResult(status_code=200, payload={"status": HEALTHY, "components": {}})

    def test_cache_key_includes_paths_and_version_and_ttl_expires(self) -> None:
        calls = 0
        now = [10.0]

        def checker(**_kwargs: object) -> ReadinessResult:
            nonlocal calls
            calls += 1
            return self.result()

        cache = ReadinessCache(ttl_seconds=2, checker=checker, clock=lambda: now[0])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            arguments = {
                "database_path": root / "database.db",
                "data_dir": root,
                "export_dir": root / "exports",
                "version": "v1",
            }
            cache.get(**arguments)
            cache.get(**arguments)
            cache.get(**{**arguments, "version": "v2"})
            now[0] = 13.0
            cache.get(**arguments)

        self.assertEqual(calls, 3)

    def test_concurrent_calls_for_same_key_share_one_probe(self) -> None:
        calls = 0
        calls_lock = Lock()
        checker_started = Event()
        release_checker = Event()
        callers = Barrier(4)

        def checker(**_kwargs: object) -> ReadinessResult:
            nonlocal calls
            with calls_lock:
                calls += 1
            checker_started.set()
            if not release_checker.wait(timeout=5):
                raise AssertionError("The test did not release the readiness checker.")
            return self.result()

        cache = ReadinessCache(ttl_seconds=60, checker=checker)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            arguments = {
                "database_path": root / "database.db",
                "data_dir": root,
                "export_dir": root / "exports",
                "version": "single-flight",
            }

            def call_cache() -> ReadinessResult:
                callers.wait(timeout=5)
                return cache.get(**arguments)

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(call_cache) for _ in range(4)]
                started = checker_started.wait(timeout=5)
                release_checker.set()
                self.assertTrue(started)
                results = [future.result(timeout=5) for future in futures]

        self.assertEqual(calls, 1)
        self.assertTrue(all(result.status_code == 200 for result in results))


class StartupReadinessTests(unittest.TestCase):
    def test_missing_database_does_not_bootstrap_db_services_and_health_stays_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            export_dir = root / "exports"
            data_dir.mkdir()
            export_dir.mkdir()
            missing_database = data_dir / "missing.db"

            with patch.multiple(
                server,
                DEFAULT_DATABASE=missing_database,
                DATA_DIR=data_dir,
                EXPORT_DIR=export_dir,
                DEPLOYMENT_VERSION="missing-database-test",
                APP_ENABLE_SCHEDULES=False,
                APP_ENABLE_UTILITIES=False,
                REQUIRE_EXISTING_DATABASE=False,
                _utility_run_manager=None,
                _health_readiness_cache=ReadinessCache(ttl_seconds=60),
            ), patch.object(server, "validate_auth_configuration") as validate_auth, patch.object(
                server, "reconcile_interrupted_runs"
            ) as reconcile, patch.object(server, "email_service") as email_factory:
                server.start_maintenance_scheduler()
                response = server.health_endpoint()
                self.assertFalse(missing_database.exists())

        validate_auth.assert_called_once_with()
        reconcile.assert_not_called()
        email_factory.assert_not_called()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body)["status"], UNHEALTHY)

    def test_required_existing_database_makes_startup_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            export_dir = root / "exports"
            data_dir.mkdir()
            export_dir.mkdir()
            missing_database = data_dir / "missing.db"

            with patch.multiple(
                server,
                DEFAULT_DATABASE=missing_database,
                DATA_DIR=data_dir,
                EXPORT_DIR=export_dir,
                DEPLOYMENT_VERSION="required-database-test",
                APP_ENABLE_SCHEDULES=False,
                APP_ENABLE_UTILITIES=False,
                REQUIRE_EXISTING_DATABASE=True,
                _utility_run_manager=None,
                _health_readiness_cache=ReadinessCache(ttl_seconds=60),
            ), patch.object(server, "validate_auth_configuration"), patch.object(
                server, "reconcile_interrupted_runs"
            ) as reconcile:
                with self.assertRaisesRegex(RuntimeError, "persistent database mount"):
                    server.start_maintenance_scheduler()

            reconcile.assert_not_called()
            self.assertFalse(missing_database.exists())

    def test_required_existing_connection_cannot_create_replacement_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "missing-parent" / "missing.db"

            with self.assertRaises(sqlite3.OperationalError):
                connect(database, require_existing=True)

            self.assertFalse(database.parent.exists())
            self.assertFalse(database.exists())

    def test_required_missing_database_is_reported_as_persistence_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "missing.db"
            with patch.multiple(
                server,
                DEFAULT_DATABASE=database,
                REQUIRE_EXISTING_DATABASE=True,
            ):
                self.assertEqual(
                    server.status(),
                    {
                        "status": "persistence-unavailable",
                        "storage": "sqlite",
                        "message": "Opportunity Radar backend is running.",
                    },
                )
                with self.assertRaises(server.HTTPException) as raised:
                    server.repository()

            self.assertEqual(raised.exception.status_code, 503)
            self.assertIn("database mount", raised.exception.detail)
            self.assertFalse(database.exists())

    def test_writable_sqlite_connections_apply_production_safety_pragmas(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "production.db"
            with closing(connect(database)) as connection:
                initialize_schema(connection)

            with closing(connect(database, require_existing=True)) as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
                self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_existing_unready_database_is_not_initialized_during_startup(self) -> None:
        for contents in (b"", b"not a sqlite database"):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                data_dir = root / "data"
                export_dir = root / "exports"
                data_dir.mkdir()
                export_dir.mkdir()
                database = data_dir / "unready.db"
                database.write_bytes(contents)
                cache = ReadinessCache(ttl_seconds=60)

                with patch.multiple(
                    server,
                    DEFAULT_DATABASE=database,
                    DATA_DIR=data_dir,
                    EXPORT_DIR=export_dir,
                    DEPLOYMENT_VERSION="unready-database-test",
                    APP_ENABLE_SCHEDULES=False,
                    APP_ENABLE_UTILITIES=False,
                    REQUIRE_EXISTING_DATABASE=False,
                    _utility_run_manager=None,
                    _health_readiness_cache=cache,
                ), patch.object(server, "validate_auth_configuration"), patch.object(
                    server, "reconcile_interrupted_runs"
                ) as reconcile, patch.object(server, "email_service") as email_factory:
                    server.start_maintenance_scheduler()
                    response = server.health_endpoint()

                self.assertEqual(database.read_bytes(), contents)
                reconcile.assert_not_called()
                email_factory.assert_not_called()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(json.loads(response.body)["status"], UNHEALTHY)

    def test_database_connection_closes_when_pragma_setup_fails(self) -> None:
        connection = Mock()
        connection.execute.side_effect = sqlite3.DatabaseError("synthetic pragma failure")
        with patch("backend.db.sqlite3.connect", return_value=connection):
            with self.assertRaises(sqlite3.DatabaseError):
                connect(Path("synthetic.db"), readonly=True)
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
