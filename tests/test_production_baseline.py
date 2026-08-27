from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.routing import APIRoute

from backend.blueash_auth import BlueAshIdentity
import excel_tools
import job_tools
import server


CONFIG_KEYS = {
    "APP_ENV", "AUTH_MODE", "APP_BASE_PATH", "APP_PUBLIC_URL", "APP_TRUSTED_ADMIN_USER_ID",
    "BLUEASH_API_URL", "BLUEASH_LOGIN_URL", "BLUEASH_SESSION_COOKIE", "BLUEASH_COOKIE_DOMAIN",
    "BLUEASH_APP_SLUG", "APP_ENABLE_BROWSER_JOBS", "APP_ENABLE_COMPANY_REFRESH",
    "APP_ENABLE_UTILITIES", "APP_ENABLE_SCHEDULES", "APP_ENABLE_DISCOVERY",
    "APP_WRITE_FRONTEND_MIRRORS", "APP_DATA_DIR", "APP_IMPORT_DIR", "APP_EXPORT_DIR",
    "APP_OUTPUT_DIR", "APP_BACKUP_DIR", "APP_LOG_DIR", "DATABASE_URL",
}
TRUSTED_ID = "17f975f1-e63a-4daa-985a-82d39ed60684"


class ProductionConfigurationTests(unittest.TestCase):
    def test_missing_and_unknown_environment_fail_closed(self) -> None:
        for values in ({}, {"APP_ENV": "prodution", "AUTH_MODE": "blueash"}):
            with self.subTest(values=values):
                result = run_validation(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("APP_ENV", result.stderr)

    def test_server_startup_rejects_missing_environment_before_runtime_initialization(self) -> None:
        result = run_python({}, "import server; server.start_maintenance_scheduler()")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("APP_ENV", result.stderr)

    def test_missing_auth_mode_and_production_local_mode_fail_closed(self) -> None:
        cases = [
            {"APP_ENV": "development", "APP_PUBLIC_URL": "http://127.0.0.1:5173"},
            {"APP_ENV": "production", "AUTH_MODE": "local", "APP_PUBLIC_URL": "https://radar.example.com"},
        ]
        for values in cases:
            with self.subTest(values=values):
                self.assertNotEqual(run_validation(values).returncode, 0)

    def test_explicit_local_development_requires_loopback(self) -> None:
        valid = run_validation({
            "APP_ENV": "development", "AUTH_MODE": "local",
            "APP_PUBLIC_URL": "http://127.0.0.1:5173",
        })
        invalid = run_validation({
            "APP_ENV": "development", "AUTH_MODE": "local",
            "APP_PUBLIC_URL": "https://development.example.com",
        })
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("loopback", invalid.stderr)

    def test_valid_production_blueash_configuration_succeeds(self) -> None:
        result = run_validation(valid_production_environment())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_required_blueash_setting_is_explicit(self) -> None:
        required = {
            "APP_PUBLIC_URL", "BLUEASH_API_URL", "BLUEASH_LOGIN_URL",
            "BLUEASH_SESSION_COOKIE", "BLUEASH_APP_SLUG",
        }
        for name in required:
            values = valid_production_environment()
            values.pop(name)
            with self.subTest(name=name):
                result = run_validation(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)

    def test_production_urls_trusted_id_and_public_mirrors_are_validated(self) -> None:
        cases = [
            ("APP_PUBLIC_URL", "http://radar.example.com"),
            ("BLUEASH_API_URL", "http://api.example.com"),
            ("BLUEASH_LOGIN_URL", "http://portal.example.com/login"),
            ("APP_TRUSTED_ADMIN_USER_ID", "not-a-uuid"),
            ("APP_WRITE_FRONTEND_MIRRORS", "true"),
        ]
        for name, value in cases:
            values = valid_production_environment()
            values[name] = value
            with self.subTest(name=name):
                self.assertNotEqual(run_validation(values).returncode, 0)

    def test_production_runtime_paths_cannot_point_into_frontend_public(self) -> None:
        values = valid_production_environment()
        values["APP_DATA_DIR"] = str(Path(__file__).resolve().parents[1] / "frontend" / "public" / "runtime")
        result = run_validation(values)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("frontend/public", result.stderr.replace("\\", "/"))

    def test_invalid_boolean_value_fails_during_import(self) -> None:
        result = run_python(
            {"APP_ENABLE_UTILITIES": "sometimes"},
            "import config",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("APP_ENABLE_UTILITIES", result.stderr)

    def test_production_feature_defaults_are_disabled(self) -> None:
        result = run_python(
            {"APP_ENV": "production"},
            "import json; from config import feature_flags_payload, APP_WRITE_FRONTEND_MIRRORS; "
            "print(json.dumps({'features': feature_flags_payload(), 'mirrors': APP_WRITE_FRONTEND_MIRRORS}))",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(any(payload["features"].values()))
        self.assertFalse(payload["mirrors"])

    def test_runtime_paths_are_independently_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            values = {
                "APP_DATA_DIR": str(root / "data"), "APP_IMPORT_DIR": str(root / "imports"),
                "APP_EXPORT_DIR": str(root / "exports"), "APP_BACKUP_DIR": str(root / "backups"),
                "APP_LOG_DIR": str(root / "logs"),
            }
            result = run_python(
                values,
                "import json, config; print(json.dumps([str(config.DATA_DIR), str(config.IMPORT_DIR), "
                "str(config.EXPORT_DIR), str(config.BACKUP_DIR), str(config.LOG_DIR)]))",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                [str((root / name).resolve()) for name in ("data", "imports", "exports", "backups", "logs")],
            )

    def test_production_api_documentation_is_disabled(self) -> None:
        result = run_python(
            valid_production_environment(),
            "import json, server; print(json.dumps([server.app.docs_url, server.app.redoc_url, server.app.openapi_url]))",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [None, None, None])


class RoutePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.routes = {
            (method, route.path): route
            for route in server.app.routes if isinstance(route, APIRoute)
            for method in route.methods
        }

    def dependencies(self, method: str, path: str) -> set[object]:
        return {dependency.call for dependency in self.routes[(method, path)].dependant.dependencies}

    def test_all_global_mutations_have_administrator_dependency(self) -> None:
        for (method, path), route in self.routes.items():
            if method not in {"POST", "PUT", "PATCH", "DELETE"} or path == "/api/auth/logout":
                continue
            with self.subTest(method=method, path=path):
                self.assertIn(server.require_administrator, {item.call for item in route.dependant.dependencies})

    def test_sensitive_reads_have_administrator_dependency(self) -> None:
        paths = {
            "/api/applications", "/api/resume", "/api/jobs/{job_id}/match",
            "/api/settings/email", "/api/email/status", "/api/email/history",
            "/api/maintenance/jobs", "/api/maintenance/jobs/{job_key}/history",
            "/api/maintenance/runs/{run_id}", "/api/user-utilities/runs/{run_id}",
        }
        for path in paths:
            with self.subTest(path=path):
                self.assertIn(server.require_administrator, self.dependencies("GET", path))

    def test_operational_routes_require_utilities(self) -> None:
        for (method, path), _route in self.routes.items():
            if path.startswith(("/api/maintenance", "/api/user-utilities", "/api/settings/email", "/api/email")):
                with self.subTest(method=method, path=path):
                    self.assertIn(server.require_utilities_enabled, self.dependencies(method, path))

    def test_specific_unsafe_routes_have_feature_dependencies(self) -> None:
        company_refresh = self.dependencies("POST", "/api/companies/{company_id}/refresh")
        self.assertTrue({
            server.require_company_refresh_enabled,
            server.require_discovery_enabled,
            server.require_browser_jobs_enabled,
        }.issubset(company_refresh))
        self.assertIn(server.require_browser_jobs_enabled, self.dependencies("POST", "/api/collect-jobs"))
        self.assertIn(server.require_schedules_enabled, self.dependencies("PUT", "/api/maintenance/jobs/{job_key}/schedule"))

    def test_health_is_public_and_production_managers_are_lazy(self) -> None:
        self.assertEqual(self.dependencies("GET", "/api/health"), set())
        result = run_python(
            {},
            "import json, server; print(json.dumps([server._utility_run_manager is None, "
            "server._maintenance_scheduler is None]))",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [True, True])

    def test_session_rejects_an_authenticated_untrusted_user(self) -> None:
        identity = BlueAshIdentity(
            id="bceda542-92f0-46b6-9bc8-38a67c3ba272", username="other-admin",
            email="other@example.com", display_name="Other", role="ADMINISTRATOR", permissions=(),
        )
        request = SimpleNamespace(cookies={"blueash_session": "opaque"})
        with patch.object(server, "APP_ENV", "production"), patch.object(
            server.BlueAshAuthClient, "authenticate", return_value=identity
        ), patch.object(server, "is_trusted_initial_administrator", return_value=False):
            with self.assertRaises(HTTPException) as caught:
                server.auth_session_endpoint(request)
        self.assertEqual(caught.exception.status_code, 403)

    def test_admin_dependency_denies_ordinary_user_and_accepts_admin(self) -> None:
        user = BlueAshIdentity(
            id=TRUSTED_ID, username="user", email="user@example.com", display_name="User",
            role="USER", permissions=("*",),
        )
        administrator = BlueAshIdentity(
            id=TRUSTED_ID, username="admin", email="admin@example.com", display_name="Admin",
            role="ADMINISTRATOR", permissions=(),
        )
        request = SimpleNamespace(state=SimpleNamespace(identity=user))
        with patch.object(server, "APP_ENV", "development"), patch.object(
            server, "is_administrator", side_effect=lambda identity: identity.role == "ADMINISTRATOR"
        ):
            with self.assertRaises(HTTPException) as caught:
                server.require_administrator(request)
            self.assertEqual(caught.exception.status_code, 403)
            request.state.identity = administrator
            self.assertEqual(server.require_administrator(request), administrator)

    def test_disabled_dynamic_action_fails_before_work_can_start(self) -> None:
        with patch.multiple(
            server,
            APP_ENABLE_BROWSER_JOBS=False, APP_ENABLE_COMPANY_REFRESH=False,
            APP_ENABLE_DISCOVERY=False,
        ):
            for action in (
                "refresh-missing-company-information", "refresh-company-discovery",
                "refresh-all-job-listings",
            ):
                with self.subTest(action=action), self.assertRaises(HTTPException) as caught:
                    server._require_action_features(action)
                self.assertEqual(caught.exception.status_code, 403)

    def test_background_worker_rechecks_feature_before_dispatch(self) -> None:
        worker = Mock(return_value={"unexpected": True})
        guarded = server._guarded_utility_worker("refresh-all-job-listings", worker)
        with patch.object(server, "APP_ENABLE_BROWSER_JOBS", False):
            with self.assertRaises(RuntimeError):
                guarded(Mock(), Mock())
        worker.assert_not_called()


class PublicMirrorTests(unittest.TestCase):
    def test_legacy_public_mirror_helpers_do_nothing_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "private.json"
            source.write_text("[]", encoding="utf-8")
            companies_target = root / "public" / "companies.json"
            jobs_target = root / "public" / "jobs.json"
            with patch.multiple(
                excel_tools,
                APP_WRITE_FRONTEND_MIRRORS=False,
                DEFAULT_FRONTEND_COMPANIES_JSON=companies_target,
            ):
                excel_tools.mirror_companies_json(source)
            with patch.multiple(
                job_tools,
                APP_WRITE_FRONTEND_MIRRORS=False,
                DEFAULT_FRONTEND_JOBS_JSON=jobs_target,
            ):
                job_tools.mirror_jobs_json(source)
            self.assertFalse(companies_target.exists())
            self.assertFalse(jobs_target.exists())


def valid_production_environment() -> dict[str, str]:
    return {
        "APP_ENV": "production", "AUTH_MODE": "blueash",
        "APP_PUBLIC_URL": "https://blueashdigital.tech/OpportunityRadar",
        "BLUEASH_API_URL": "https://api.blueashdigital.tech",
        "BLUEASH_LOGIN_URL": "https://blueashdigital.tech/",
        "BLUEASH_SESSION_COOKIE": "blueash_session",
        "BLUEASH_APP_SLUG": "opportunity-radar",
        "APP_TRUSTED_ADMIN_USER_ID": TRUSTED_ID,
        "APP_WRITE_FRONTEND_MIRRORS": "false",
    }


def run_validation(values: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return run_python(
        values,
        "from backend.blueash_auth import validate_auth_configuration; validate_auth_configuration(); print('ok')",
    )


def run_python(values: dict[str, str], code: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for key in CONFIG_KEYS:
        environment.pop(key, None)
    environment.update(values)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], env=environment,
        capture_output=True, text=True, check=False,
    )


if __name__ == "__main__":
    unittest.main()
