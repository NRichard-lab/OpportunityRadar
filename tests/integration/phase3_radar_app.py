from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import requests


def _require_isolated_phase3_runtime() -> None:
    if os.environ.get("PHASE3_SYNTHETIC_TEST_MODE") != "1":
        raise RuntimeError("The Phase 3 Radar test application is disabled.")
    if os.environ.get("APP_ENV") != "production" or os.environ.get("AUTH_MODE") != "portal_handoff":
        raise RuntimeError("The Phase 3 Radar test application requires production handoff settings.")
    portal_url = urlsplit(os.environ.get("BLUEASH_PORTAL_API_URL", ""))
    if (portal_url.scheme, portal_url.hostname, portal_url.port, portal_url.path) != (
        "https", "api.blueashdigital.tech", None, "",
    ):
        raise RuntimeError("The synthetic transport is restricted to the exact local Portal API hostname.")
    if os.environ.get("APP_PUBLIC_URL") != "https://radar.blueashdigital.tech":
        raise RuntimeError("The synthetic Radar origin must match the exact production-shaped hostname.")
    if os.environ.get("BLUEASH_AUTH_CLIENT_ID") != "opportunity-radar":
        raise RuntimeError("The synthetic client ID must match Opportunity Radar.")
    if len(os.environ.get("BLUEASH_AUTH_CLIENT_SECRET", "")) < 32:
        raise RuntimeError("The synthetic client secret must be ephemeral and high entropy.")

    expected_paths = {
        "APP_DATA_DIR": "/var/lib/opportunity-radar/data",
        "APP_IMPORT_DIR": "/var/lib/opportunity-radar/data/imports",
        "APP_EXPORT_DIR": "/var/lib/opportunity-radar/exports",
        "APP_BACKUP_DIR": "/var/lib/opportunity-radar/backups",
        "APP_LOG_DIR": "/var/log/opportunity-radar",
    }
    for name, expected in expected_paths.items():
        if Path(os.environ.get(name, "")).as_posix() != expected:
            raise RuntimeError(f"{name} is not the isolated Phase 3 container path.")
    if os.environ.get("DATABASE_URL") != "sqlite:////var/lib/opportunity-radar/database/opportunity_radar.db":
        raise RuntimeError("The synthetic Radar database must use the isolated mounted fixture.")
    if os.environ.get("REQUIRE_EXISTING_DATABASE", "").lower() != "true":
        raise RuntimeError("The integration backend must refuse a missing SQLite database.")
    for name in (
        "APP_ENABLE_BROWSER_JOBS",
        "APP_ENABLE_COMPANY_REFRESH",
        "APP_ENABLE_UTILITIES",
        "APP_ENABLE_SCHEDULES",
        "APP_ENABLE_DISCOVERY",
        "APP_WRITE_FRONTEND_MIRRORS",
    ):
        if os.environ.get(name, "").lower() != "false":
            raise RuntimeError(f"{name} must remain disabled in Phase 3 integration.")
    ca_bundle = Path(os.environ.get("REQUESTS_CA_BUNDLE", ""))
    if ca_bundle.as_posix() != "/phase3-ca/root.crt":
        raise RuntimeError("The synthetic transport must trust only the exported local Caddy CA certificate.")


_require_isolated_phase3_runtime()

# Production correctly rejects Docker-private destinations through its SSRF-safe
# transport. This wrapper exists only in tests/integration, is bind-mounted only
# by compose.phase3.yaml, and swaps the transport before server.app is imported.
import backend.blueash_auth as blueash_auth  # noqa: E402

blueash_auth.SSRFProtectedSession = requests.Session

from server import app  # noqa: E402,F401
