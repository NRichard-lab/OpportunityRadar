from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ContainerArtifactTests(unittest.TestCase):
    def test_backend_image_is_pinned_non_root_and_single_worker(self) -> None:
        dockerfile = (ROOT / "docker" / "backend" / "Dockerfile").read_text(encoding="utf-8")

        self.assertRegex(dockerfile, r"FROM python:3\.12-slim-bookworm@sha256:[0-9a-f]{64}")
        self.assertIn("USER ${APP_UID}:${APP_GID}", dockerfile)
        self.assertIn('ARG APP_UID=10001', dockerfile)
        self.assertIn('"--workers", "1"', dockerfile)
        self.assertIn('"--no-access-log"', dockerfile)
        self.assertNotIn("requirements.txt /tmp", dockerfile)
        self.assertNotIn("playwright install", dockerfile.lower())

    def test_production_python_lock_is_exact_and_browser_free(self) -> None:
        lock = (ROOT / "requirements-production.txt").read_text(encoding="utf-8")
        requirements = [
            line.strip()
            for line in lock.splitlines()
            if line.strip() and not line.lstrip().startswith("#") and not line[0].isspace()
        ]

        self.assertGreater(len(requirements), 20)
        self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s]+", item) for item in requirements))
        self.assertFalse(any(item.lower().startswith("playwright==") for item in requirements))

    def test_frontend_server_separates_spa_api_data_and_assets(self) -> None:
        dockerfile = (ROOT / "docker" / "frontend" / "Dockerfile").read_text(encoding="utf-8")
        nginx = (ROOT / "docker" / "frontend" / "default.conf").read_text(encoding="utf-8")

        self.assertRegex(dockerfile, r"FROM node:22-bookworm-slim@sha256:[0-9a-f]{64}")
        self.assertRegex(dockerfile, r"FROM nginxinc/nginx-unprivileged:[^\s]+@sha256:[0-9a-f]{64}")
        self.assertIn("ARG VITE_BASE_PATH=/", dockerfile)
        self.assertIn("USER ${APP_UID}:${APP_GID}", dockerfile)
        self.assertIn("location ^~ /api/", nginx)
        self.assertIn("location ^~ /data/", nginx)
        self.assertIn("try_files $uri /index.html", nginx)
        self.assertIn("try_files $uri =404", nginx)
        self.assertIn("immutable", nginx)

    def test_production_compose_has_private_edge_and_explicit_writes(self) -> None:
        compose = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")

        self.assertIn("opportunity-radar-backend:", compose)
        self.assertIn("opportunity-radar-frontend:", compose)
        self.assertIn("external: true", compose)
        self.assertIn('name: "${BLUEASH_EDGE_NETWORK:-blueash-edge}"', compose)
        self.assertNotRegex(compose, r"(?m)^\s+ports:\s*$")
        self.assertEqual(compose.count("read_only: true"), 2)
        self.assertEqual(compose.count("init: true"), 2)
        self.assertIn("stop_grace_period: 60s", compose)
        self.assertIn('REQUIRE_EXISTING_DATABASE: "true"', compose)
        self.assertIn('APP_ENABLE_BROWSER_JOBS: "false"', compose)
        self.assertIn("/srv/opportunity-radar", compose)

    def test_production_environment_is_safe_template_only(self) -> None:
        template = (ROOT / "deploy" / "opportunity-radar.env.example").read_text(encoding="utf-8")
        expected = {
            "APP_ENV": "production",
            "AUTH_MODE": "portal_handoff",
            "APP_PUBLIC_URL": "https://radar.blueashdigital.tech",
            "APP_BASE_PATH": "",
            "BLUEASH_PORTAL_PUBLIC_URL": "https://blueashdigital.tech",
            "BLUEASH_PORTAL_API_URL": "https://api.blueashdigital.tech",
            "BLUEASH_AUTH_CLIENT_ID": "opportunity-radar",
            "RADAR_SESSION_COOKIE_NAME": "__Host-opportunity_radar_session",
            "RADAR_SESSION_IDLE_SECONDS": "1800",
            "RADAR_SESSION_ABSOLUTE_MAX_SECONDS": "28800",
            "RADAR_INTROSPECTION_CACHE_SECONDS": "0",
            "REQUIRE_EXISTING_DATABASE": "true",
            "APP_ENABLE_BROWSER_JOBS": "false",
            "APP_ENABLE_COMPANY_REFRESH": "false",
            "APP_ENABLE_UTILITIES": "false",
            "APP_ENABLE_SCHEDULES": "false",
            "APP_ENABLE_DISCOVERY": "false",
            "APP_WRITE_FRONTEND_MIRRORS": "false",
        }
        values = {
            key: value
            for line in template.splitlines()
            if line and not line.startswith("#") and "=" in line
            for key, value in [line.split("=", 1)]
        }

        for key, value in expected.items():
            self.assertEqual(values.get(key), value)
        self.assertEqual(values["OPPORTUNITY_RADAR_SECRET_KEY"], "<set securely>")
        self.assertEqual(values["BLUEASH_AUTH_CLIENT_SECRET"], "<set securely>")

    def test_docker_context_excludes_private_runtime_categories(self) -> None:
        ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        required_patterns = {
            ".env",
            ".env.*",
            "**/.env",
            "**/.env.*",
            "data/",
            "Input/",
            "backups/",
            "logs/",
            "output/",
            "frontend/public/data/",
            "*.db",
            "*-wal",
            "*-shm",
            "*.pdf",
            "*.docx",
            ".playwright-browsers/",
            "frontend/node_modules/",
            "**/__pycache__/",
            "**/*.py[cod]",
        }

        self.assertTrue(required_patterns.issubset(set(ignored)))

    def test_auth_sources_do_not_forward_parent_cookie_or_enable_callback_access_logs(self) -> None:
        auth_source = (ROOT / "backend" / "blueash_auth.py").read_text(encoding="utf-8")
        server_source = (ROOT / "server.py").read_text(encoding="utf-8")
        dockerfile = (ROOT / "docker" / "backend" / "Dockerfile").read_text(encoding="utf-8")

        self.assertNotIn("blueash_session", auth_source + server_source)
        self.assertNotIn('headers={"Cookie"', auth_source)
        self.assertIn('auth=(BLUEASH_AUTH_CLIENT_ID, BLUEASH_AUTH_CLIENT_SECRET)', auth_source)
        self.assertIn('"--no-access-log"', dockerfile)
        self.assertIn('response.headers["Referrer-Policy"] = "no-referrer"', server_source)

    def test_frontend_callback_state_rechecks_an_existing_session_without_idle_polling(self) -> None:
        app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertNotIn("window.setInterval", app_source)
        self.assertIn("if (response.status === 401) {\n          if (callbackAuthState) return;", app_source)
        self.assertIn("removeCallbackAuthState();\n            setCallbackAuthState(null);", app_source)
        self.assertIn("parsed.origin === window.location.origin", app_source)


if __name__ == "__main__":
    unittest.main()
