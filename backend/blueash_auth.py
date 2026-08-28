from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.parse import urlparse
from uuid import UUID

import requests

from backend.outbound_security import OutboundSecurityError, SSRFProtectedSession
from config import (
    APP_BASE_PATH,
    APP_ENABLE_BROWSER_JOBS,
    APP_ENV,
    APP_PUBLIC_URL,
    APP_TRUSTED_ADMIN_USER_ID,
    AUTH_MODE,
    BLUEASH_API_URL,
    BLUEASH_APP_SLUG,
    BLUEASH_COOKIE_DOMAIN,
    BLUEASH_LOGIN_URL,
    BLUEASH_SESSION_COOKIE,
    APP_WRITE_FRONTEND_MIRRORS,
    BACKUP_DIR,
    BASE_DIR,
    DATA_DIR,
    DEFAULT_DATABASE,
    EXPORT_DIR,
    IMPORT_DIR,
    LOG_DIR,
    SUPPORTED_APP_ENVS,
    SUPPORTED_AUTH_MODES,
)


class BlueAshAuthenticationError(RuntimeError):
    pass


class BlueAshAuthorizationError(RuntimeError):
    pass


class BlueAshUnavailableError(RuntimeError):
    pass


class BlueAshConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class BlueAshIdentity:
    id: str
    username: str
    email: str
    display_name: str
    role: str
    permissions: tuple[str, ...]
    development_bypass: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "authenticated": True,
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "displayName": self.display_name,
            "role": self.role,
            "permissions": list(self.permissions),
            "developmentBypass": self.development_bypass,
        }


def blueash_auth_enabled() -> bool:
    return AUTH_MODE == "blueash"


def validate_auth_configuration() -> None:
    if APP_ENV not in SUPPORTED_APP_ENVS:
        choices = ", ".join(sorted(SUPPORTED_APP_ENVS))
        raise BlueAshConfigurationError(f"APP_ENV must be explicitly set to one of: {choices}.")
    if AUTH_MODE not in SUPPORTED_AUTH_MODES:
        choices = ", ".join(sorted(SUPPORTED_AUTH_MODES))
        raise BlueAshConfigurationError(f"AUTH_MODE must be explicitly set to one of: {choices}.")
    if AUTH_MODE == "local":
        if APP_ENV != "development":
            raise BlueAshConfigurationError("Local authentication is allowed only when APP_ENV=development.")
        parsed_public_url = _validated_url("APP_PUBLIC_URL", APP_PUBLIC_URL, require_https=False)
        if parsed_public_url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise BlueAshConfigurationError("Local authentication requires a loopback APP_PUBLIC_URL.")
        return

    required = {
        "APP_PUBLIC_URL": APP_PUBLIC_URL,
        "BLUEASH_API_URL": BLUEASH_API_URL,
        "BLUEASH_LOGIN_URL": BLUEASH_LOGIN_URL,
        "BLUEASH_SESSION_COOKIE": BLUEASH_SESSION_COOKIE,
        "BLUEASH_APP_SLUG": BLUEASH_APP_SLUG,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise BlueAshConfigurationError(
            "Blue Ash authentication requires explicit values for: " + ", ".join(missing) + "."
        )
    require_https = APP_ENV == "production"
    _validated_url("APP_PUBLIC_URL", APP_PUBLIC_URL, require_https=require_https)
    _validated_url("BLUEASH_API_URL", BLUEASH_API_URL, require_https=require_https)
    _validated_url("BLUEASH_LOGIN_URL", BLUEASH_LOGIN_URL, require_https=require_https)
    if BLUEASH_COOKIE_DOMAIN and any(character in BLUEASH_COOKIE_DOMAIN for character in "/:@?# "):
        raise BlueAshConfigurationError("BLUEASH_COOKIE_DOMAIN is invalid.")
    if APP_ENV == "production":
        if APP_ENABLE_BROWSER_JOBS:
            raise BlueAshConfigurationError(
                "APP_ENABLE_BROWSER_JOBS cannot be enabled in production in this release; "
                "browser traffic does not yet have a DNS-pinned network egress boundary."
            )
        if not _canonical_uuid(APP_TRUSTED_ADMIN_USER_ID):
            raise BlueAshConfigurationError("Production requires APP_TRUSTED_ADMIN_USER_ID to be a valid UUID.")
        if APP_WRITE_FRONTEND_MIRRORS:
            raise BlueAshConfigurationError("APP_WRITE_FRONTEND_MIRRORS cannot be enabled in production.")
        public_directory = (BASE_DIR / "frontend" / "public").resolve()
        runtime_paths = {
            "APP_DATA_DIR": DATA_DIR,
            "DATABASE_URL": DEFAULT_DATABASE,
            "APP_IMPORT_DIR": IMPORT_DIR,
            "APP_EXPORT_DIR": EXPORT_DIR,
            "APP_BACKUP_DIR": BACKUP_DIR,
            "APP_LOG_DIR": LOG_DIR,
        }
        for name, path in runtime_paths.items():
            if _is_within(Path(path).resolve(), public_directory):
                raise BlueAshConfigurationError(f"{name} cannot point into frontend/public in production.")


def is_administrator(identity: BlueAshIdentity) -> bool:
    if identity.development_bypass:
        return APP_ENV == "development" and AUTH_MODE == "local"
    return identity.role == "ADMINISTRATOR"


def is_trusted_initial_administrator(identity: BlueAshIdentity) -> bool:
    if identity.development_bypass:
        return APP_ENV == "development" and AUTH_MODE == "local"
    trusted_id = _canonical_uuid(APP_TRUSTED_ADMIN_USER_ID)
    identity_id = _canonical_uuid(identity.id)
    return bool(trusted_id and identity_id and identity.role == "ADMINISTRATOR" and identity_id == trusted_id)


def login_url(return_to: str = "") -> str:
    destination = _safe_return_url(return_to)
    if AUTH_MODE == "local":
        return destination
    separator = "&" if "?" in BLUEASH_LOGIN_URL else "?"
    return f"{BLUEASH_LOGIN_URL}{separator}{urlencode({'returnTo': destination})}"


class BlueAshAuthClient:
    def __init__(self, timeout_seconds: float = 5.0) -> None:
        self.timeout_seconds = timeout_seconds

    def authenticate(self, session_token: str, *, require_application: bool = True) -> BlueAshIdentity:
        validate_auth_configuration()
        if AUTH_MODE == "local":
            return BlueAshIdentity(
                id="local-development", username="local", email="local@development.invalid",
                display_name="Local Development", role="ADMINISTRATOR", permissions=("*",),
                development_bypass=True,
            )
        if AUTH_MODE != "blueash":
            raise BlueAshConfigurationError("Authentication is not configured.")
        if not session_token:
            raise BlueAshAuthenticationError("Blue Ash authentication is required.")

        identity_payload = self._get("/api/profile/me", session_token)
        if require_application:
            applications = self._get("/api/apps", session_token)
            if not isinstance(applications, list) or not any(
                isinstance(item, dict) and item.get("slug") == BLUEASH_APP_SLUG for item in applications
            ):
                raise BlueAshAuthorizationError("Your Blue Ash account does not have access to Opportunity Radar.")

        identity_id = str(identity_payload.get("id") or "").strip()
        role = str(identity_payload.get("role") or "").strip().upper()
        if not identity_id:
            raise BlueAshUnavailableError("Blue Ash returned an identity without an ID.")
        if role not in {"ADMINISTRATOR", "USER"}:
            raise BlueAshAuthorizationError("Blue Ash returned an unrecognized account role.")
        return BlueAshIdentity(
            id=identity_id,
            username=str(identity_payload.get("username") or ""),
            email=str(identity_payload.get("email") or ""),
            display_name=str(identity_payload.get("display_name") or identity_payload.get("username") or ""),
            role=role,
            permissions=tuple(str(value) for value in (identity_payload.get("permissions") or [])),
        )

    def logout(self, session_token: str) -> None:
        validate_auth_configuration()
        if AUTH_MODE == "local" or not session_token:
            return
        try:
            with SSRFProtectedSession() as session:
                response = session.post(
                    f"{BLUEASH_API_URL}/api/auth/logout",
                    headers={"Cookie": f"{BLUEASH_SESSION_COOKIE}={session_token}"},
                    timeout=self.timeout_seconds,
                    allow_redirects=True,
                )
        except (OutboundSecurityError, requests.RequestException) as exc:
            raise BlueAshUnavailableError("Blue Ash logout is temporarily unavailable.") from exc
        if response.status_code >= 500:
            raise BlueAshUnavailableError("Blue Ash logout is temporarily unavailable.")

    def _get(self, path: str, session_token: str) -> Any:
        try:
            with SSRFProtectedSession() as session:
                response = session.get(
                    f"{BLUEASH_API_URL}{path}",
                    headers={"Cookie": f"{BLUEASH_SESSION_COOKIE}={session_token}"},
                    timeout=self.timeout_seconds,
                    allow_redirects=True,
                )
        except (OutboundSecurityError, requests.RequestException) as exc:
            raise BlueAshUnavailableError("Blue Ash authentication is temporarily unavailable.") from exc
        if response.status_code == 401:
            raise BlueAshAuthenticationError("Your Blue Ash session has expired.")
        if response.status_code == 403:
            raise BlueAshAuthorizationError("Your Blue Ash account is not authorized for this request.")
        if not response.ok:
            raise BlueAshUnavailableError("Blue Ash authentication is temporarily unavailable.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BlueAshUnavailableError("Blue Ash returned an invalid authentication response.") from exc
        if path.endswith("/me") and not isinstance(payload, dict):
            raise BlueAshUnavailableError("Blue Ash returned an invalid identity response.")
        return payload


def _safe_return_url(return_to: str) -> str:
    value = return_to.strip()
    allowed_prefix = APP_BASE_PATH or "/"
    if not value.startswith(allowed_prefix) or value.startswith("//"):
        value = allowed_prefix
    public_root = APP_PUBLIC_URL[:-len(allowed_prefix)] if APP_BASE_PATH and APP_PUBLIC_URL.endswith(APP_BASE_PATH) else APP_PUBLIC_URL
    return f"{public_root}{value}"


def _validated_url(name: str, value: str, *, require_https: bool) -> Any:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc or parsed.username or parsed.password:
        raise BlueAshConfigurationError(f"{name} must be a valid absolute URL without user credentials.")
    if require_https and parsed.scheme != "https":
        raise BlueAshConfigurationError(f"{name} must use HTTPS in production.")
    if parsed.scheme not in {"http", "https"} or parsed.query or parsed.fragment:
        raise BlueAshConfigurationError(f"{name} must be an HTTP(S) URL without a query or fragment.")
    return parsed


def _canonical_uuid(value: str) -> str:
    try:
        return str(UUID(value.strip()))
    except (AttributeError, TypeError, ValueError):
        return ""


def _is_within(path: Any, directory: Any) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False
