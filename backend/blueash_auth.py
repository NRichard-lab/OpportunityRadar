from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urlparse, urlsplit
from uuid import UUID

import requests

from backend.outbound_security import OutboundSecurityError, SSRFProtectedSession
from config import (
    APP_BASE_PATH,
    APP_ENABLE_BROWSER_JOBS,
    APP_ENV,
    APP_PUBLIC_URL,
    APP_TRUSTED_ADMIN_USER_ID,
    APP_WRITE_FRONTEND_MIRRORS,
    AUTH_MODE,
    BACKUP_DIR,
    BASE_DIR,
    BLUEASH_AUTH_CLIENT_ID,
    BLUEASH_AUTH_CLIENT_SECRET,
    BLUEASH_PORTAL_API_URL,
    BLUEASH_PORTAL_PUBLIC_URL,
    DATA_DIR,
    DEFAULT_DATABASE,
    EXPORT_DIR,
    IMPORT_DIR,
    LOG_DIR,
    OPPORTUNITY_RADAR_SECRET_KEY,
    RADAR_HANDOFF_STATE_TTL_SECONDS,
    RADAR_INTROSPECTION_CACHE_SECONDS,
    RADAR_SESSION_ABSOLUTE_MAX_SECONDS,
    RADAR_SESSION_COOKIE_NAME,
    RADAR_SESSION_IDLE_SECONDS,
    REQUIRE_EXISTING_DATABASE,
    SUPPORTED_APP_ENVS,
    SUPPORTED_AUTH_MODES,
)


AUTHORIZE_PATH = "/api/app-auth/authorize"
EXCHANGE_PATH = "/api/app-auth/exchange"
INTROSPECT_PATH = "/api/app-auth/introspect"
REVOKE_PATH = "/api/app-auth/revoke"
PORTAL_HEALTH_PATH = "/api/health"
AUTH_CALLBACK_PATH = "/api/auth/callback"
AUTH_START_PATH = "/api/auth/start"
PRODUCTION_SESSION_COOKIE_NAME = "__Host-opportunity_radar_session"
PRODUCTION_HANDOFF_COOKIE_NAME = "__Host-opportunity_radar_handoff"
DEVELOPMENT_HANDOFF_COOKIE_NAME = "opportunity_radar_handoff"
MAX_RETURN_PATH_LENGTH = 2_048
MAX_AUTH_RESPONSE_BYTES = 64 * 1024
_CLOCK_SKEW_SECONDS = 60
_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SIGNING_PURPOSE = b"opportunity-radar-portal-handoff-v1\0"


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


@dataclass(frozen=True)
class HandoffAttempt:
    state: str
    code_verifier: str
    code_challenge: str
    return_path: str
    expires_at: int


@dataclass(frozen=True)
class PortalExchange:
    access_token: str
    expires_in: int
    idle_expires_at: datetime
    absolute_expires_at: datetime


@dataclass(frozen=True)
class _CachedIdentity:
    identity: BlueAshIdentity
    expires_at_monotonic: float


def portal_handoff_enabled() -> bool:
    return AUTH_MODE == "portal_handoff"


def local_identity() -> BlueAshIdentity:
    validate_auth_configuration()
    if AUTH_MODE != "local":
        raise BlueAshConfigurationError("Local authentication is not configured.")
    return BlueAshIdentity(
        id="local-development",
        username="local",
        email="local@development.invalid",
        display_name="Local Development",
        role="ADMINISTRATOR",
        permissions=("*",),
        development_bypass=True,
    )


def validate_auth_configuration() -> None:
    if APP_ENV not in SUPPORTED_APP_ENVS:
        choices = ", ".join(sorted(SUPPORTED_APP_ENVS))
        raise BlueAshConfigurationError(f"APP_ENV must be explicitly set to one of: {choices}.")
    if AUTH_MODE not in SUPPORTED_AUTH_MODES:
        choices = ", ".join(sorted(SUPPORTED_AUTH_MODES))
        raise BlueAshConfigurationError(f"AUTH_MODE must be explicitly set to one of: {choices}.")

    require_https = APP_ENV == "production"
    public_url = _validated_url("APP_PUBLIC_URL", APP_PUBLIC_URL, require_https=require_https)
    if AUTH_MODE == "local":
        if APP_ENV != "development":
            raise BlueAshConfigurationError("Local authentication is allowed only when APP_ENV=development.")
        if public_url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise BlueAshConfigurationError("Local authentication requires a loopback APP_PUBLIC_URL.")
        return

    required = {
        "BLUEASH_PORTAL_PUBLIC_URL": BLUEASH_PORTAL_PUBLIC_URL,
        "BLUEASH_PORTAL_API_URL": BLUEASH_PORTAL_API_URL,
        "BLUEASH_AUTH_CLIENT_ID": BLUEASH_AUTH_CLIENT_ID,
        "BLUEASH_AUTH_CLIENT_SECRET": BLUEASH_AUTH_CLIENT_SECRET,
        "OPPORTUNITY_RADAR_SECRET_KEY": OPPORTUNITY_RADAR_SECRET_KEY,
        "RADAR_SESSION_COOKIE_NAME": RADAR_SESSION_COOKIE_NAME,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise BlueAshConfigurationError(
            "Portal handoff authentication requires explicit values for: " + ", ".join(missing) + "."
        )
    _validated_url("BLUEASH_PORTAL_PUBLIC_URL", BLUEASH_PORTAL_PUBLIC_URL, require_https=require_https)
    _validated_url("BLUEASH_PORTAL_API_URL", BLUEASH_PORTAL_API_URL, require_https=require_https)
    if not _CLIENT_ID_PATTERN.fullmatch(BLUEASH_AUTH_CLIENT_ID):
        raise BlueAshConfigurationError("BLUEASH_AUTH_CLIENT_ID is invalid.")
    _validate_secret("BLUEASH_AUTH_CLIENT_SECRET", BLUEASH_AUTH_CLIENT_SECRET)
    _validate_secret("OPPORTUNITY_RADAR_SECRET_KEY", OPPORTUNITY_RADAR_SECRET_KEY)
    if any(character in RADAR_SESSION_COOKIE_NAME for character in "()<>@,;:\\\\[]?={} \t\r\n"):
        raise BlueAshConfigurationError("RADAR_SESSION_COOKIE_NAME is invalid.")

    if APP_ENV == "production":
        production_urls = {
            "APP_PUBLIC_URL": (APP_PUBLIC_URL, "https://radar.blueashdigital.tech"),
            "BLUEASH_PORTAL_PUBLIC_URL": (BLUEASH_PORTAL_PUBLIC_URL, "https://blueashdigital.tech"),
            "BLUEASH_PORTAL_API_URL": (BLUEASH_PORTAL_API_URL, "https://api.blueashdigital.tech"),
        }
        for name, (actual, expected) in production_urls.items():
            if actual != expected:
                raise BlueAshConfigurationError(f"Production {name} must be exactly {expected}.")
        if APP_BASE_PATH:
            raise BlueAshConfigurationError("Production APP_BASE_PATH must be empty for the dedicated Radar origin.")
        if RADAR_SESSION_COOKIE_NAME != PRODUCTION_SESSION_COOKIE_NAME:
            raise BlueAshConfigurationError(
                f"Production RADAR_SESSION_COOKIE_NAME must be {PRODUCTION_SESSION_COOKIE_NAME}."
            )
        if RADAR_SESSION_IDLE_SECONDS != 30 * 60:
            raise BlueAshConfigurationError("Production Radar sessions require a 30-minute idle timeout.")
        if RADAR_SESSION_ABSOLUTE_MAX_SECONDS < RADAR_SESSION_IDLE_SECONDS:
            raise BlueAshConfigurationError(
                "RADAR_SESSION_ABSOLUTE_MAX_SECONDS cannot be shorter than RADAR_SESSION_IDLE_SECONDS."
            )
        if not REQUIRE_EXISTING_DATABASE:
            raise BlueAshConfigurationError(
                "Production requires REQUIRE_EXISTING_DATABASE=true so a missing persistent "
                "SQLite mount cannot be replaced by an empty database."
            )
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
    return bool(
        trusted_id
        and identity_id
        and identity.role == "ADMINISTRATOR"
        and hmac.compare_digest(identity_id, trusted_id)
    )


def safe_return_path(return_to: str) -> str:
    fallback = APP_BASE_PATH or "/"
    if not isinstance(return_to, str):
        return fallback
    value = return_to.strip()
    if not value or len(value) > MAX_RETURN_PATH_LENGTH:
        return fallback

    decoded_values = [value]
    for _ in range(4):
        decoded = unquote(decoded_values[-1])
        if decoded == decoded_values[-1]:
            break
        decoded_values.append(decoded)
    else:
        if unquote(decoded_values[-1]) != decoded_values[-1]:
            return fallback
    for candidate in decoded_values:
        if (
            not candidate.startswith("/")
            or candidate.startswith("//")
            or "\\" in candidate
            or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
        ):
            return fallback
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return fallback
        if parsed.scheme or parsed.netloc:
            return fallback
        if parsed.fragment or any(segment in {".", ".."} for segment in parsed.path.split("/")):
            return fallback
        api_prefix = f"{APP_BASE_PATH}/api" if APP_BASE_PATH else "/api"
        if parsed.path == api_prefix or parsed.path.startswith(f"{api_prefix}/"):
            return fallback
        if APP_BASE_PATH and not (
            parsed.path == APP_BASE_PATH or parsed.path.startswith(f"{APP_BASE_PATH}/")
        ):
            return fallback
    return value


def callback_url() -> str:
    return f"{APP_PUBLIC_URL.rstrip('/')}{AUTH_CALLBACK_PATH}"


def auth_start_url(return_to: str = "") -> str:
    path = f"{APP_BASE_PATH}{AUTH_START_PATH}" if APP_BASE_PATH else AUTH_START_PATH
    return f"{path}?{urlencode({'returnTo': safe_return_path(return_to)})}"


def portal_root_url() -> str:
    return f"{BLUEASH_PORTAL_PUBLIC_URL.rstrip('/')}/"


def create_handoff_attempt(return_to: str = "", *, now: int | None = None) -> tuple[HandoffAttempt, str]:
    validate_auth_configuration()
    if AUTH_MODE != "portal_handoff":
        raise BlueAshConfigurationError("Portal handoff authentication is not configured.")
    issued_at = int(time.time() if now is None else now)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    attempt = HandoffAttempt(
        state=state,
        code_verifier=verifier,
        code_challenge=challenge,
        return_path=safe_return_path(return_to),
        expires_at=issued_at + RADAR_HANDOFF_STATE_TTL_SECONDS,
    )
    payload = {
        "v": 1,
        "state": attempt.state,
        "code_verifier": attempt.code_verifier,
        "return_path": attempt.return_path,
        "issued_at": issued_at,
        "expires_at": attempt.expires_at,
    }
    return attempt, _sign_payload(payload)


def consume_handoff_cookie(cookie_value: str, state: str, *, now: int | None = None) -> HandoffAttempt:
    current_time = int(time.time() if now is None else now)
    if not cookie_value or not state or len(state) > 512:
        raise BlueAshAuthenticationError("The authentication handoff is invalid or has expired.")
    payload = _verify_payload(cookie_value)
    try:
        version = payload["v"]
        stored_state = payload["state"]
        verifier = payload["code_verifier"]
        return_path = payload["return_path"]
        issued_at = int(payload["issued_at"])
        expires_at = int(payload["expires_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BlueAshAuthenticationError("The authentication handoff is invalid or has expired.") from exc
    if (
        version != 1
        or not isinstance(stored_state, str)
        or not isinstance(verifier, str)
        or not isinstance(return_path, str)
        or not hmac.compare_digest(stored_state, state)
        or current_time < issued_at - _CLOCK_SKEW_SECONDS
        or current_time > expires_at
        or expires_at - issued_at > RADAR_HANDOFF_STATE_TTL_SECONDS
        or len(verifier) < 43
        or safe_return_path(return_path) != return_path
    ):
        raise BlueAshAuthenticationError("The authentication handoff is invalid or has expired.")
    challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return HandoffAttempt(stored_state, verifier, challenge, return_path, expires_at)


def build_authorize_url(attempt: HandoffAttempt) -> str:
    query = urlencode(
        {
            "client_id": BLUEASH_AUTH_CLIENT_ID,
            "redirect_uri": callback_url(),
            "response_type": "code",
            "state": attempt.state,
            "code_challenge": attempt.code_challenge,
            "code_challenge_method": "S256",
            "return_path": attempt.return_path,
        }
    )
    return f"{BLUEASH_PORTAL_API_URL.rstrip('/')}{AUTHORIZE_PATH}?{query}"


def handoff_cookie_name() -> str:
    return PRODUCTION_HANDOFF_COOKIE_NAME if APP_ENV == "production" else DEVELOPMENT_HANDOFF_COOKIE_NAME


def set_handoff_cookie(response: Any, cookie_value: str) -> None:
    response.set_cookie(
        handoff_cookie_name(),
        cookie_value,
        max_age=RADAR_HANDOFF_STATE_TTL_SECONDS,
        path="/",
        secure=APP_ENV == "production",
        httponly=True,
        samesite="lax",
    )


def clear_handoff_cookie(response: Any) -> None:
    response.delete_cookie(
        handoff_cookie_name(),
        path="/",
        secure=APP_ENV == "production",
        httponly=True,
        samesite="lax",
    )


def set_session_cookie(response: Any, access_token: str, *, max_age: int) -> None:
    response.set_cookie(
        RADAR_SESSION_COOKIE_NAME,
        access_token,
        max_age=max(1, min(int(max_age), RADAR_SESSION_ABSOLUTE_MAX_SECONDS)),
        path="/",
        secure=APP_ENV == "production",
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Any) -> None:
    response.delete_cookie(
        RADAR_SESSION_COOKIE_NAME,
        path="/",
        secure=APP_ENV == "production",
        httponly=True,
        samesite="lax",
    )


class PortalHandoffClient:
    def __init__(self, timeout_seconds: float = 5.0) -> None:
        self.timeout_seconds = timeout_seconds
        self._cache: dict[str, _CachedIdentity] = {}
        self._cache_lock = threading.RLock()

    def probe(self) -> None:
        validate_auth_configuration()
        if AUTH_MODE == "local":
            return
        try:
            with SSRFProtectedSession() as session:
                response = session.get(
                    f"{BLUEASH_PORTAL_API_URL}{PORTAL_HEALTH_PATH}",
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                )
        except (OutboundSecurityError, requests.RequestException) as exc:
            raise BlueAshUnavailableError("Blue Ash authentication is temporarily unavailable.") from exc
        if response.status_code != 200:
            raise BlueAshUnavailableError("Blue Ash authentication is temporarily unavailable.")

    def authenticate(self, session_token: str) -> BlueAshIdentity:
        if AUTH_MODE == "local":
            return local_identity()
        return self.introspect(session_token)

    def exchange(self, code: str, code_verifier: str) -> PortalExchange:
        validate_auth_configuration()
        if AUTH_MODE != "portal_handoff":
            raise BlueAshConfigurationError("Portal handoff authentication is not configured.")
        if not _valid_opaque_value(code) or not _valid_opaque_value(code_verifier):
            raise BlueAshAuthenticationError("The authentication handoff could not be completed.")
        response = self._post(
            EXCHANGE_PATH,
            {"code": code, "code_verifier": code_verifier, "redirect_uri": callback_url()},
            operation="authentication handoff",
        )
        if response.status_code in {400, 401}:
            raise BlueAshAuthenticationError("The authentication handoff is invalid or has expired.")
        if response.status_code == 403:
            raise BlueAshAuthorizationError("Your Blue Ash account does not have access to Opportunity Radar.")
        if response.status_code != 200:
            raise BlueAshUnavailableError("Blue Ash authentication is temporarily unavailable.")
        payload = _response_json(response, "authentication handoff")
        try:
            token = payload["access_token"]
            token_type = payload["token_type"]
            expires_in = payload["expires_in"]
            idle_expires_at = _parse_portal_datetime(payload["idle_expires_at"])
            absolute_expires_at = _parse_portal_datetime(payload["absolute_expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BlueAshUnavailableError("Blue Ash returned an invalid authentication response.") from exc
        now = datetime.now(timezone.utc)
        if (
            not _valid_opaque_value(token)
            or token_type != "Bearer"
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or expires_in < 1
            or expires_in > RADAR_SESSION_ABSOLUTE_MAX_SECONDS
            or idle_expires_at <= now
            or absolute_expires_at <= now
            or idle_expires_at > absolute_expires_at
        ):
            raise BlueAshUnavailableError("Blue Ash returned an invalid authentication response.")
        return PortalExchange(token, expires_in, idle_expires_at, absolute_expires_at)

    def introspect(self, session_token: str) -> BlueAshIdentity:
        validate_auth_configuration()
        if AUTH_MODE == "local":
            return local_identity()
        if AUTH_MODE != "portal_handoff":
            raise BlueAshConfigurationError("Authentication is not configured.")
        if not _valid_opaque_value(session_token):
            raise BlueAshAuthenticationError("Blue Ash authentication is required.")

        cache_key = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        current_monotonic = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None and cached.expires_at_monotonic > current_monotonic:
                return cached.identity
            self._cache.pop(cache_key, None)

        response = self._post(INTROSPECT_PATH, {"token": session_token}, operation="authentication")
        if response.status_code == 403:
            raise BlueAshAuthorizationError("Your Blue Ash account does not have access to Opportunity Radar.")
        if response.status_code != 200:
            raise BlueAshUnavailableError("Blue Ash authentication is temporarily unavailable.")
        payload = _response_json(response, "authentication")
        if payload.get("active") is not True:
            raise BlueAshAuthenticationError("Your Opportunity Radar session has expired.")
        identity, idle_expires_at, absolute_expires_at = self._identity_from_introspection(payload)
        now = datetime.now(timezone.utc)
        success_ttl = min(
            float(RADAR_INTROSPECTION_CACHE_SECONDS),
            max(0.0, (idle_expires_at - now).total_seconds()),
            max(0.0, (absolute_expires_at - now).total_seconds()),
        )
        if success_ttl > 0:
            with self._cache_lock:
                if cache_key not in self._cache and len(self._cache) >= 2_048:
                    oldest_key = min(
                        self._cache,
                        key=lambda key: self._cache[key].expires_at_monotonic,
                    )
                    self._cache.pop(oldest_key, None)
                self._cache[cache_key] = _CachedIdentity(identity, time.monotonic() + success_ttl)
        return identity

    def revoke(self, session_token: str) -> None:
        validate_auth_configuration()
        if AUTH_MODE == "local" or not session_token:
            return
        cache_key = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        with self._cache_lock:
            self._cache.pop(cache_key, None)
        if not _valid_opaque_value(session_token):
            return
        response = self._post(REVOKE_PATH, {"token": session_token}, operation="sign out")
        if response.status_code == 403:
            raise BlueAshAuthorizationError("Opportunity Radar could not revoke this session.")
        if response.status_code != 204:
            raise BlueAshUnavailableError("Blue Ash sign out is temporarily unavailable.")

    def _identity_from_introspection(
        self, payload: dict[str, Any]
    ) -> tuple[BlueAshIdentity, datetime, datetime]:
        try:
            user_id = str(payload["user_id"]).strip()
            username = str(payload["username"]).strip()
            email = str(payload["email"]).strip()
            display_name = str(payload["display_name"]).strip()
            role = str(payload["role"]).strip().upper()
            permissions_payload = payload["permissions"]
            application_slug = str(payload["application_slug"]).strip()
            idle_expires_at = _parse_portal_datetime(payload["idle_expires_at"])
            absolute_expires_at = _parse_portal_datetime(payload["absolute_expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BlueAshUnavailableError("Blue Ash returned an invalid authentication response.") from exc
        if (
            not isinstance(permissions_payload, list)
            or len(permissions_payload) > 256
            or not all(
                isinstance(value, str) and 0 < len(value) <= 256
                for value in permissions_payload
            )
        ):
            raise BlueAshUnavailableError("Blue Ash returned an invalid authentication response.")
        if application_slug != BLUEASH_AUTH_CLIENT_ID:
            raise BlueAshAuthorizationError("Your Blue Ash account does not have access to Opportunity Radar.")
        if role not in {"ADMINISTRATOR", "USER"}:
            raise BlueAshAuthorizationError("Blue Ash returned an unrecognized account role.")
        canonical_user_id = _canonical_uuid(user_id)
        if (
            not canonical_user_id
            or user_id != canonical_user_id
            or not username
            or len(username) > 320
            or not email
            or len(email) > 320
            or len(display_name) > 320
        ):
            raise BlueAshUnavailableError("Blue Ash returned an invalid authentication response.")
        now = datetime.now(timezone.utc)
        if (
            idle_expires_at <= now
            or absolute_expires_at <= now
            or idle_expires_at > absolute_expires_at
            or (idle_expires_at - now).total_seconds() > RADAR_SESSION_IDLE_SECONDS + _CLOCK_SKEW_SECONDS
            or (absolute_expires_at - now).total_seconds()
            > RADAR_SESSION_ABSOLUTE_MAX_SECONDS + _CLOCK_SKEW_SECONDS
        ):
            raise BlueAshAuthenticationError("Your Opportunity Radar session has expired.")
        return (
            BlueAshIdentity(
                id=canonical_user_id,
                username=username,
                email=email,
                display_name=display_name or username,
                role=role,
                permissions=tuple(permissions_payload),
            ),
            idle_expires_at,
            absolute_expires_at,
        )

    def _post(self, path: str, payload: dict[str, Any], *, operation: str) -> Any:
        try:
            with SSRFProtectedSession() as session:
                return session.post(
                    f"{BLUEASH_PORTAL_API_URL}{path}",
                    json=payload,
                    auth=(BLUEASH_AUTH_CLIENT_ID, BLUEASH_AUTH_CLIENT_SECRET),
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                )
        except (OutboundSecurityError, requests.RequestException) as exc:
            raise BlueAshUnavailableError(f"Blue Ash {operation} is temporarily unavailable.") from exc


# Compatibility name retained for internal imports while server wiring is migrated.
BlueAshAuthClient = PortalHandoffClient


def _response_json(response: Any, operation: str) -> dict[str, Any]:
    content = getattr(response, "content", b"")
    if isinstance(content, (bytes, bytearray)) and len(content) > MAX_AUTH_RESPONSE_BYTES:
        raise BlueAshUnavailableError(f"Blue Ash returned an invalid {operation} response.")
    content_length = str(getattr(response, "headers", {}).get("content-length") or "")
    if content_length.isdigit() and int(content_length) > MAX_AUTH_RESPONSE_BYTES:
        raise BlueAshUnavailableError(f"Blue Ash returned an invalid {operation} response.")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise BlueAshUnavailableError(f"Blue Ash returned an invalid {operation} response.") from exc
    if not isinstance(payload, dict):
        raise BlueAshUnavailableError(f"Blue Ash returned an invalid {operation} response.")
    return payload


def _parse_portal_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("Invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _valid_opaque_value(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 16 <= len(value) <= 4_096
        and value == value.strip()
        and not any(ord(character) < 33 or ord(character) == 127 for character in value)
    )


def _sign_payload(payload: dict[str, Any]) -> str:
    encoded = _base64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(
        OPPORTUNITY_RADAR_SECRET_KEY.encode("utf-8"),
        _SIGNING_PURPOSE + encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded}.{_base64url(signature)}"


def _verify_payload(cookie_value: str) -> dict[str, Any]:
    try:
        encoded, supplied_signature = cookie_value.split(".", 1)
        if len(encoded) > 4_096 or len(supplied_signature) > 128:
            raise ValueError("oversized handoff cookie")
        expected_signature = _base64url(
            hmac.new(
                OPPORTUNITY_RADAR_SECRET_KEY.encode("utf-8"),
                _SIGNING_PURPOSE + encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("invalid handoff signature")
        payload = json.loads(_base64url_decode(encoded))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise BlueAshAuthenticationError("The authentication handoff is invalid or has expired.") from exc
    if not isinstance(payload, dict):
        raise BlueAshAuthenticationError("The authentication handoff is invalid or has expired.")
    return payload


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _validate_secret(name: str, value: str) -> None:
    lowered = value.casefold()
    if (
        len(value) < 32
        or value.startswith("<")
        or value.endswith(">")
        or "set securely" in lowered
        or "change-me" in lowered
        or "changeme" in lowered
        or "replace-me" in lowered
        or "replace_me" in lowered
        or "replace me" in lowered
        or "placeholder" in lowered
        or "development-only" in lowered
        or "dev-only" in lowered
        or "example-secret" in lowered
    ):
        raise BlueAshConfigurationError(f"{name} must be a non-placeholder secret of at least 32 characters.")


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
