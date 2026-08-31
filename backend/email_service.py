from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.repository import OpportunityRepository, utc_now


WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
DEFAULT_SCHEDULE_DAYS = list(WEEKDAYS[:5])
DEFAULT_SCHEDULE_TIME = "07:00"
DEFAULT_TIMEZONE = "America/Denver"


class EmailConfigurationError(ValueError):
    pass


class EmailDeliveryError(RuntimeError):
    pass


class EmailService:
    def __init__(self, repository: OpportunityRepository) -> None:
        self.repository = repository
        self.cipher = SecretCipher(repository.database_path)

    def get_settings(self) -> dict[str, Any]:
        with self.repository.connection(readonly=True) as connection:
            row = connection.execute("SELECT * FROM email_settings WHERE id='default'").fetchone()
        if row is None:
            settings = default_settings()
            settings["scheduleTimezone"] = self.application_timezone()
            return settings
        return settings_snapshot(row)

    def application_timezone(self) -> str:
        with self.repository.connection(readonly=True) as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key='scheduler_timezone'"
            ).fetchone()
        if row:
            try:
                timezone_name = str(json.loads(row["value_json"]))
                ZoneInfo(timezone_name)
                return timezone_name
            except (json.JSONDecodeError, ZoneInfoNotFoundError):
                pass
        return DEFAULT_TIMEZONE

    def bootstrap_from_environment(self) -> bool:
        if self.get_settings()["configured"] or not os.environ.get("SMTP_PASSWORD"):
            return False
        self.save_settings(
            {
                "smtpHost": os.environ.get("SMTP_HOST", ""),
                "smtpPort": int(os.environ.get("SMTP_PORT", "465")),
                "security": os.environ.get("SMTP_SECURITY", "ssl_tls"),
                "smtpUsername": os.environ.get("SMTP_USERNAME", ""),
                "smtpPassword": os.environ.get("SMTP_PASSWORD", ""),
                "fromEmail": os.environ.get("SMTP_FROM_EMAIL", ""),
                "fromName": os.environ.get("SMTP_FROM_NAME", "Opportunity Radar"),
                "replyToEmail": os.environ.get("SMTP_REPLY_TO", ""),
                "enabled": False,
                "recipients": [],
                "scheduleDays": DEFAULT_SCHEDULE_DAYS,
                "scheduleTime": DEFAULT_SCHEDULE_TIME,
                "scheduleTimezone": self.application_timezone(),
            }
        )
        return True

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self._settings_row()
        current_snapshot = settings_snapshot(current) if current else default_settings()
        merged = {**default_settings(), **current_snapshot, **payload}
        security = str(merged.get("security") or "ssl_tls")
        if security not in {"ssl_tls", "starttls", "none"}:
            raise EmailConfigurationError("Choose SSL/TLS, STARTTLS, or None for SMTP security.")
        try:
            port = int(merged.get("smtpPort") or 0)
        except (TypeError, ValueError) as exc:
            raise EmailConfigurationError("SMTP Port must be a number between 1 and 65535.") from exc
        if not 1 <= port <= 65535:
            raise EmailConfigurationError("SMTP Port must be a number between 1 and 65535.")

        for field, label in (("fromEmail", "From Email"), ("replyToEmail", "Reply-To Email")):
            value = str(merged.get(field) or "").strip()
            if value and not valid_email(value):
                raise EmailConfigurationError(f"Enter a valid {label}.")

        recipients = normalize_recipients(merged.get("recipients", []))
        schedule_days = normalize_schedule_days(merged.get("scheduleDays", []))
        schedule_time = validate_schedule_time(str(merged.get("scheduleTime") or ""))
        schedule_timezone = str(merged.get("scheduleTimezone") or self.application_timezone()).strip()
        try:
            zone = ZoneInfo(schedule_timezone)
        except ZoneInfoNotFoundError as exc:
            raise EmailConfigurationError(
                "Choose a valid IANA timezone, such as America/Denver."
            ) from exc
        enabled = bool(merged.get("enabled"))
        if enabled and not recipients:
            raise EmailConfigurationError("Add at least one email recipient before enabling the digest.")
        if enabled and not schedule_days:
            raise EmailConfigurationError("Choose at least one scheduled day before enabling the digest.")

        password = str(payload.get("smtpPassword") or "")
        ciphertext = self.cipher.encrypt(password) if password else str(
            current["smtp_password_ciphertext"] if current else ""
        )
        now = utc_now()
        tracking_started = str(current["tracking_started_at"] if current else "") or now
        last_scheduled_date = str(current["last_scheduled_date"] if current else "")
        local_now = datetime.now(zone)
        if (
            enabled
            and not current_snapshot.get("enabled")
            and WEEKDAYS[local_now.weekday()] in schedule_days
            and local_now.strftime("%H:%M") >= schedule_time
        ):
            # Enabling a schedule after today's time should not cause an unexpected
            # immediate send. The next configured day remains eligible.
            last_scheduled_date = local_now.date().isoformat()

        values = {
            "id": "default",
            "smtp_host": str(merged.get("smtpHost") or "").strip(),
            "smtp_port": port,
            "security": security,
            "smtp_username": str(merged.get("smtpUsername") or "").strip(),
            "smtp_password_ciphertext": ciphertext,
            "from_email": str(merged.get("fromEmail") or "").strip(),
            "from_name": str(merged.get("fromName") or "Opportunity Radar").strip(),
            "reply_to_email": str(merged.get("replyToEmail") or "").strip(),
            "daily_enabled": int(enabled),
            "recipient_email": recipients[0] if recipients else "",
            "send_after_refresh": 0,
            "send_when_empty": 0,
            "tracking_started_at": tracking_started,
            "schedule_days_json": json.dumps(schedule_days),
            "schedule_time": schedule_time,
            "schedule_timezone": schedule_timezone,
            "recipients_json": json.dumps(recipients),
            "last_scheduled_date": last_scheduled_date,
            "checkpoint_established_at": str(
                current["checkpoint_established_at"] if current else ""
            ),
            "last_successful_at": str(current["last_successful_at"] if current else ""),
            "updated_at": now,
        }
        with self.repository.connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO settings (key,value_json,updated_at) VALUES ('scheduler_timezone',?,?)",
                (json.dumps(schedule_timezone), now),
            )
            connection.execute(
                "UPDATE maintenance_schedules SET timezone=?,updated_at=? WHERE timezone<>?",
                (schedule_timezone, now, schedule_timezone),
            )
            connection.execute(
                """INSERT INTO email_settings
                (id,smtp_host,smtp_port,security,smtp_username,smtp_password_ciphertext,
                from_email,from_name,reply_to_email,daily_enabled,recipient_email,
                send_after_refresh,send_when_empty,tracking_started_at,schedule_days_json,
                schedule_time,schedule_timezone,recipients_json,last_scheduled_date,
                checkpoint_established_at,last_successful_at,updated_at)
                VALUES (:id,:smtp_host,:smtp_port,:security,:smtp_username,
                :smtp_password_ciphertext,:from_email,:from_name,:reply_to_email,
                :daily_enabled,:recipient_email,:send_after_refresh,:send_when_empty,
                :tracking_started_at,:schedule_days_json,:schedule_time,
                :schedule_timezone,:recipients_json,:last_scheduled_date,
                :checkpoint_established_at,:last_successful_at,:updated_at)
                ON CONFLICT(id) DO UPDATE SET smtp_host=excluded.smtp_host,
                smtp_port=excluded.smtp_port,security=excluded.security,
                smtp_username=excluded.smtp_username,
                smtp_password_ciphertext=excluded.smtp_password_ciphertext,
                from_email=excluded.from_email,from_name=excluded.from_name,
                reply_to_email=excluded.reply_to_email,daily_enabled=excluded.daily_enabled,
                recipient_email=excluded.recipient_email,send_after_refresh=0,send_when_empty=0,
                tracking_started_at=excluded.tracking_started_at,
                schedule_days_json=excluded.schedule_days_json,
                schedule_time=excluded.schedule_time,schedule_timezone=excluded.schedule_timezone,
                recipients_json=excluded.recipients_json,
                last_scheduled_date=excluded.last_scheduled_date,
                checkpoint_established_at=excluded.checkpoint_established_at,
                last_successful_at=excluded.last_successful_at,updated_at=excluded.updated_at""",
                values,
            )
        return self.get_settings()

    def send_test_email(self, recipient: str) -> dict[str, Any]:
        recipients = normalize_recipients([recipient])
        settings = self._delivery_settings()
        self.send_email(
            settings,
            recipients,
            "OpportunityRadar Test Email",
            "OpportunityRadar email delivery is configured and working.",
            test_email_html(),
        )
        return {"message": "Test email sent successfully."}

    def send_job_digest(self, *, trigger_type: str, scheduled_for: str = "") -> dict[str, Any]:
        public_settings = self.get_settings()
        if trigger_type == "scheduled" and not public_settings["enabled"]:
            return digest_result("", "Disabled", 0, 0)

        digest_id = f"digest-{uuid4()}"
        started = utc_now()
        recipients = list(public_settings["recipients"])
        try:
            settings = self._delivery_settings(require_recipients=True)
        except EmailConfigurationError as exc:
            completed = utc_now()
            self._record_digest(
                digest_id, started, completed, recipients, 0, 0, "Failed", str(exc),
                trigger_type, scheduled_for,
            )
            return digest_result(digest_id, "Failed", 0, 0, str(exc))

        current_jobs = self.current_active_jobs()
        if not public_settings["checkpointEstablishedAt"]:
            completed = utc_now()
            self._store_baseline(
                digest_id, started, completed, settings["recipients"], current_jobs,
                trigger_type, scheduled_for,
            )
            return digest_result(digest_id, "Baseline Established", 0, 0)

        previous_jobs = self.snapshot_jobs()
        added = [current_jobs[key] for key in sorted(current_jobs.keys() - previous_jobs.keys())]
        removed = [previous_jobs[key] for key in sorted(previous_jobs.keys() - current_jobs.keys())]
        if not added and not removed:
            completed = utc_now()
            self._record_digest(
                digest_id, started, completed, settings["recipients"], 0, 0,
                "Skipped - No Changes", "", trigger_type, scheduled_for,
            )
            return digest_result(digest_id, "Skipped - No Changes", 0, 0)

        self._record_digest(
            digest_id, started, "", settings["recipients"], len(added), len(removed),
            "Sending", "", trigger_type, scheduled_for,
        )
        date_label = digest_date_label(settings["scheduleTimezone"])
        subject = f"OpportunityRadar Job Update: {len(added)} Added, {len(removed)} Removed"
        try:
            self.send_email(
                settings,
                settings["recipients"],
                subject,
                digest_text(added, removed, date_label),
                digest_html(added, removed, date_label),
            )
        except (EmailConfigurationError, EmailDeliveryError) as exc:
            completed = utc_now()
            self._finish_digest(digest_id, completed, "Failed", str(exc))
            return digest_result(digest_id, "Failed", len(added), len(removed), str(exc))

        completed = utc_now()
        self._advance_checkpoint(digest_id, completed, current_jobs, added, removed)
        return digest_result(digest_id, "Sent", len(added), len(removed))

    def send_email(
        self,
        settings: dict[str, Any],
        recipients: list[str],
        subject: str,
        text: str,
        html_body: str,
    ) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((settings["fromName"], settings["fromEmail"]))
        message["To"] = ", ".join(recipients)
        if settings.get("replyToEmail"):
            message["Reply-To"] = settings["replyToEmail"]
        message.set_content(text)
        message.add_alternative(html_body, subtype="html")
        context = ssl.create_default_context()
        try:
            if settings["security"] == "ssl_tls":
                with smtplib.SMTP_SSL(
                    settings["smtpHost"], settings["smtpPort"], timeout=20, context=context
                ) as smtp:
                    self._authenticate_and_send(smtp, settings, message)
            else:
                with smtplib.SMTP(settings["smtpHost"], settings["smtpPort"], timeout=20) as smtp:
                    smtp.ehlo()
                    if settings["security"] == "starttls":
                        smtp.starttls(context=context)
                        smtp.ehlo()
                    self._authenticate_and_send(smtp, settings, message)
        except smtplib.SMTPAuthenticationError as exc:
            raise EmailDeliveryError(
                "Authentication failed. Check your SMTP username and password."
            ) from exc
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            raise EmailDeliveryError(
                "The email server could not be reached or did not accept the message. "
                "Check the SMTP settings and try again."
            ) from exc

    def current_active_jobs(self) -> dict[str, dict[str, str]]:
        with self.repository.connection(readonly=True) as connection:
            rows = connection.execute(
                """SELECT id,company_name,title,pay_min,pay_max,pay_text,pay_period,
                pay_currency,source_url FROM jobs WHERE LOWER(status)='open' ORDER BY id"""
            ).fetchall()
        return {
            job_identity(str(row["id"])): {
                "identityKey": job_identity(str(row["id"])),
                "jobId": str(row["id"]),
                "companyName": str(row["company_name"] or "Company not listed"),
                "title": str(row["title"] or "Title not listed"),
                "payDisplay": pay_display(row),
                "sourceUrl": str(row["source_url"] or "").strip(),
            }
            for row in rows
        }

    def snapshot_jobs(self) -> dict[str, dict[str, str]]:
        with self.repository.connection(readonly=True) as connection:
            rows = connection.execute(
                "SELECT * FROM email_snapshot_jobs ORDER BY identity_key"
            ).fetchall()
        return {
            str(row["identity_key"]): {
                "identityKey": str(row["identity_key"]),
                "jobId": str(row["job_id"]),
                "companyName": str(row["company_name"]),
                "title": str(row["title"]),
                "payDisplay": str(row["pay_display"]),
                "sourceUrl": str(row["source_url"]),
            }
            for row in rows
        }

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.repository.connection(readonly=True) as connection:
            rows = connection.execute(
                "SELECT * FROM email_digests ORDER BY started_at DESC,rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [digest_snapshot(row) for row in rows]

    def status(self, *, scheduler_running: bool = False) -> dict[str, Any]:
        settings = self.get_settings()
        history = self.history(1)
        with self.repository.connection(readonly=True) as connection:
            successful = connection.execute(
                """SELECT * FROM email_digests WHERE status='Sent'
                ORDER BY completed_at DESC,rowid DESC LIMIT 1"""
            ).fetchone()
        last_email = history[0] if history else None
        last_successful = digest_snapshot(successful) if successful else None
        return {
            "configured": settings["configured"],
            "enabled": settings["enabled"],
            "scheduleDays": settings["scheduleDays"],
            "scheduleTime": settings["scheduleTime"],
            "scheduleTimezone": settings["scheduleTimezone"],
            "recipients": settings["recipients"],
            "schedulerRunning": scheduler_running,
            "lastEmail": last_email,
            "lastRunAt": (last_email or {}).get("completedAt") or (last_email or {}).get("startedAt", ""),
            "lastSuccessfulEmail": last_successful,
            "lastSuccessfulSentAt": settings["lastSuccessfulAt"],
            "lastStatus": (last_email or {}).get("status", "Never Run"),
            "lastError": (last_email or {}).get("error", ""),
            "lastAddedCount": int((last_email or {}).get("addedCount", 0)),
            "lastRemovedCount": int((last_email or {}).get("removedCount", 0)),
            "checkpointEstablishedAt": settings["checkpointEstablishedAt"],
        }

    def claim_scheduled_occurrence(self, settings: dict[str, Any], local_date: str) -> bool:
        with self.repository.connection() as connection:
            cursor = connection.execute(
                """UPDATE email_settings SET last_scheduled_date=?,updated_at=?
                WHERE id='default' AND daily_enabled=1 AND last_scheduled_date<>?
                AND schedule_time=? AND schedule_timezone=? AND updated_at=?""",
                (
                    local_date,
                    utc_now(),
                    local_date,
                    settings["scheduleTime"],
                    settings["scheduleTimezone"],
                    settings["updatedAt"],
                ),
            )
            return cursor.rowcount == 1

    def _delivery_settings(self, *, require_recipients: bool = False) -> dict[str, Any]:
        row = self._settings_row()
        if row is None:
            raise EmailConfigurationError("Save the email provider settings first.")
        settings = settings_snapshot(row)
        required = [
            settings["smtpHost"], settings["smtpPort"], settings["smtpUsername"], settings["fromEmail"]
        ]
        if not all(required) or not settings["hasSmtpPassword"]:
            raise EmailConfigurationError(
                "Complete the SMTP host, port, username, password, and From Email settings."
            )
        if require_recipients and not settings["recipients"]:
            raise EmailConfigurationError("Add at least one recipient for the job digest.")
        try:
            settings["smtpPassword"] = self.cipher.decrypt(
                str(row["smtp_password_ciphertext"])
            )
        except InvalidSecretToken as exc:
            raise EmailConfigurationError(
                "The saved SMTP password cannot be read. Enter and save it again."
            ) from exc
        return settings

    def _settings_row(self) -> Any:
        with self.repository.connection(readonly=True) as connection:
            return connection.execute("SELECT * FROM email_settings WHERE id='default'").fetchone()

    @staticmethod
    def _authenticate_and_send(smtp: Any, settings: dict[str, Any], message: EmailMessage) -> None:
        smtp.login(settings["smtpUsername"], settings["smtpPassword"])
        refused = smtp.send_message(message)
        if refused:
            raise EmailDeliveryError(
                "The email server rejected one or more configured recipient addresses."
            )

    def _record_digest(
        self,
        digest_id: str,
        started: str,
        completed: str,
        recipients: list[str],
        added_count: int,
        removed_count: int,
        status: str,
        error: str,
        trigger: str,
        scheduled_for: str,
    ) -> None:
        with self.repository.connection() as connection:
            connection.execute(
                """INSERT INTO email_digests
                (id,started_at,completed_at,recipient,recipients_json,job_count,
                added_count,removed_count,status,error,trigger_type,scheduled_for)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    digest_id, started, completed, ", ".join(recipients), json.dumps(recipients),
                    added_count, added_count, removed_count, status, error, trigger, scheduled_for,
                ),
            )

    def _finish_digest(self, digest_id: str, completed: str, status: str, error: str) -> None:
        with self.repository.connection() as connection:
            connection.execute(
                "UPDATE email_digests SET completed_at=?,status=?,error=? WHERE id=?",
                (completed, status, error, digest_id),
            )

    def _store_baseline(
        self,
        digest_id: str,
        started: str,
        completed: str,
        recipients: list[str],
        current_jobs: dict[str, dict[str, str]],
        trigger: str,
        scheduled_for: str,
    ) -> None:
        with self.repository.connection() as connection:
            connection.execute(
                """INSERT INTO email_digests
                (id,started_at,completed_at,recipient,recipients_json,job_count,
                added_count,removed_count,status,error,trigger_type,scheduled_for)
                VALUES (?,?,?,?,?,0,0,0,'Baseline Established','',?,?)""",
                (
                    digest_id, started, completed, ", ".join(recipients),
                    json.dumps(recipients), trigger, scheduled_for,
                ),
            )
            replace_snapshot(connection, current_jobs, completed)
            connection.execute(
                "UPDATE email_settings SET checkpoint_established_at=?,updated_at=? WHERE id='default'",
                (completed, completed),
            )

    def _advance_checkpoint(
        self,
        digest_id: str,
        completed: str,
        current_jobs: dict[str, dict[str, str]],
        added: list[dict[str, str]],
        removed: list[dict[str, str]],
    ) -> None:
        with self.repository.connection() as connection:
            connection.execute(
                "UPDATE email_digests SET completed_at=?,status='Sent',error='' WHERE id=?",
                (completed, digest_id),
            )
            connection.executemany(
                """INSERT INTO email_digest_job_changes
                (digest_id,identity_key,change_type,job_id,company_name,title,pay_display,source_url)
                VALUES (?,?,?,?,?,?,?,?)""",
                [
                    (
                        digest_id, job["identityKey"], change_type, job["jobId"],
                        job["companyName"], job["title"], job["payDisplay"], job["sourceUrl"],
                    )
                    for change_type, jobs in (("added", added), ("removed", removed))
                    for job in jobs
                ],
            )
            replace_snapshot(connection, current_jobs, completed)
            connection.execute(
                "UPDATE email_settings SET last_successful_at=?,updated_at=? WHERE id='default'",
                (completed, completed),
            )


class SecretCipher:
    def __init__(self, database_path: Path) -> None:
        key_value = os.environ.get("OPPORTUNITY_RADAR_SECRET_KEY", "").strip()
        if key_value:
            key = hashlib.sha256(key_value.encode()).digest()
        else:
            key_path = Path(database_path).with_name(".email_secret.key")
            if key_path.exists():
                key = base64.urlsafe_b64decode(key_path.read_bytes().strip())
            else:
                key = secrets.token_bytes(32)
                key_path.write_bytes(base64.urlsafe_b64encode(key))
                try:
                    key_path.chmod(0o600)
                except OSError:
                    pass
        self.encryption_key = hmac.new(
            key, b"opportunity-radar-email-encryption", hashlib.sha256
        ).digest()
        self.authentication_key = hmac.new(
            key, b"opportunity-radar-email-authentication", hashlib.sha256
        ).digest()

    def encrypt(self, value: str) -> str:
        nonce = secrets.token_bytes(16)
        plaintext = value.encode()
        ciphertext = xor_bytes(plaintext, keystream(self.encryption_key, nonce, len(plaintext)))
        tag = hmac.new(self.authentication_key, nonce + ciphertext, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(nonce + ciphertext + tag).decode()

    def decrypt(self, value: str) -> str:
        try:
            payload = base64.urlsafe_b64decode(value.encode())
            nonce, ciphertext, tag = payload[:16], payload[16:-32], payload[-32:]
            expected = hmac.new(
                self.authentication_key, nonce + ciphertext, hashlib.sha256
            ).digest()
            if len(payload) < 48 or not hmac.compare_digest(tag, expected):
                raise InvalidSecretToken()
            return xor_bytes(
                ciphertext, keystream(self.encryption_key, nonce, len(ciphertext))
            ).decode()
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidSecretToken() from exc


class InvalidSecretToken(Exception):
    pass


def replace_snapshot(connection: Any, jobs: dict[str, dict[str, str]], snapshot_at: str) -> None:
    connection.execute("DELETE FROM email_snapshot_jobs")
    connection.executemany(
        """INSERT INTO email_snapshot_jobs
        (identity_key,job_id,company_name,title,pay_display,source_url,snapshot_at)
        VALUES (?,?,?,?,?,?,?)""",
        [
            (
                job["identityKey"], job["jobId"], job["companyName"], job["title"],
                job["payDisplay"], job["sourceUrl"], snapshot_at,
            )
            for job in jobs.values()
        ],
    )


def keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(output[:length])


def xor_bytes(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


def default_settings() -> dict[str, Any]:
    return {
        "smtpHost": "", "smtpPort": 465, "security": "ssl_tls", "smtpUsername": "",
        "fromEmail": "", "fromName": "Opportunity Radar", "replyToEmail": "",
        "enabled": False, "recipients": [], "scheduleDays": DEFAULT_SCHEDULE_DAYS.copy(),
        "scheduleTime": DEFAULT_SCHEDULE_TIME, "scheduleTimezone": DEFAULT_TIMEZONE,
        "hasSmtpPassword": False, "trackingStartedAt": "", "checkpointEstablishedAt": "",
        "lastSuccessfulAt": "", "lastScheduledDate": "", "updatedAt": "", "configured": False,
    }


def settings_snapshot(row: Any) -> dict[str, Any]:
    configured = bool(
        row["smtp_host"] and row["smtp_port"] and row["smtp_username"]
        and row["smtp_password_ciphertext"] and row["from_email"]
    )
    return {
        "smtpHost": row["smtp_host"], "smtpPort": row["smtp_port"],
        "security": row["security"], "smtpUsername": row["smtp_username"],
        "fromEmail": row["from_email"], "fromName": row["from_name"],
        "replyToEmail": row["reply_to_email"], "enabled": bool(row["daily_enabled"]),
        "recipients": json_list(row["recipients_json"]),
        "scheduleDays": ordered_schedule_days(json_list(row["schedule_days_json"])),
        "scheduleTime": row["schedule_time"], "scheduleTimezone": row["schedule_timezone"],
        "hasSmtpPassword": bool(row["smtp_password_ciphertext"]),
        "trackingStartedAt": row["tracking_started_at"],
        "checkpointEstablishedAt": row["checkpoint_established_at"],
        "lastSuccessfulAt": row["last_successful_at"],
        "lastScheduledDate": row["last_scheduled_date"], "updatedAt": row["updated_at"],
        "configured": configured,
    }


def digest_snapshot(row: Any) -> dict[str, Any]:
    recipients = json_list(row["recipients_json"])
    if not recipients and row["recipient"]:
        recipients = [item.strip() for item in str(row["recipient"]).split(",") if item.strip()]
    return {
        "id": row["id"], "startedAt": row["started_at"], "completedAt": row["completed_at"],
        "recipients": recipients, "recipient": row["recipient"],
        "jobCount": int(row["added_count"]), "addedCount": int(row["added_count"]),
        "removedCount": int(row["removed_count"]), "status": row["status"],
        "error": row["error"], "triggerType": row["trigger_type"],
        "scheduledFor": row["scheduled_for"],
    }


def digest_result(
    digest_id: str, status: str, added_count: int, removed_count: int, error: str = ""
) -> dict[str, Any]:
    result = {
        "id": digest_id, "status": status, "jobCount": added_count,
        "addedCount": added_count, "removedCount": removed_count,
    }
    if error:
        result["error"] = error
    return result


def json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, str)]


def normalize_recipients(value: Any) -> list[str]:
    if value is None:
        return []
    candidates = [value] if isinstance(value, str) else value
    if not isinstance(candidates, (list, tuple)):
        raise EmailConfigurationError("Recipients must be provided as an email address list.")
    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            raise EmailConfigurationError("Recipient email addresses cannot be blank.")
        recipient = candidate.strip().lower()
        if not valid_email(recipient):
            raise EmailConfigurationError(f"Enter a valid recipient email address: {candidate.strip()}")
        if recipient in seen:
            raise EmailConfigurationError(f"Duplicate recipient email address: {recipient}")
        seen.add(recipient)
        normalized.append(recipient)
    if len(normalized) > 50:
        raise EmailConfigurationError("No more than 50 digest recipients may be configured.")
    return normalized


def normalize_schedule_days(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise EmailConfigurationError("Scheduled days must be provided as a list.")
    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in value:
        day = str(candidate or "").strip().lower()
        if day not in WEEKDAYS:
            raise EmailConfigurationError("Choose valid days of the week for the email schedule.")
        if day in seen:
            raise EmailConfigurationError(f"Duplicate scheduled day: {day.title()}")
        seen.add(day)
        normalized.append(day)
    return ordered_schedule_days(normalized)


def ordered_schedule_days(days: list[str]) -> list[str]:
    selected = set(days)
    return [day for day in WEEKDAYS if day in selected]


def validate_schedule_time(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise EmailConfigurationError("Send Time must use a valid 24-hour HH:MM value.") from exc
    if parsed.strftime("%H:%M") != value:
        raise EmailConfigurationError("Send Time must use a valid 24-hour HH:MM value.")
    return value


def valid_email(value: str) -> bool:
    candidate = value.strip()
    parsed = parseaddr(candidate)[1]
    if parsed != candidate or candidate.count("@") != 1 or any(character.isspace() for character in candidate):
        return False
    local, domain = candidate.rsplit("@", 1)
    return bool(local and "." in domain and not domain.startswith(".") and not domain.endswith("."))


def job_identity(job_id: str) -> str:
    # Repository job IDs are the application's strongest stable identity. Each
    # collector derives them from a source/external identifier or canonical URL.
    return f"id:{job_id}"


def pay_display(row: Any) -> str:
    posted = str(row["pay_text"] or "").strip()
    if posted and posted.casefold() not in {"not listed", "unknown", "n/a"}:
        return posted
    minimum = row["pay_min"]
    maximum = row["pay_max"]
    if minimum is None and maximum is None:
        return "Pay not posted"
    currency = str(row["pay_currency"] or "USD").upper()
    prefix = "$" if currency == "USD" else f"{currency} "
    if minimum is not None and maximum is not None:
        value = f"{prefix}{format_money(minimum)} - {prefix}{format_money(maximum)}"
    elif minimum is not None:
        value = f"From {prefix}{format_money(minimum)}"
    else:
        value = f"Up to {prefix}{format_money(maximum)}"
    period = str(row["pay_period"] or "").strip().casefold()
    suffix = {
        "year": " per year", "annual": " per year", "hour": " per hour",
        "hourly": " per hour", "month": " per month", "monthly": " per month",
    }.get(period, "")
    return f"{value}{suffix}"


def format_money(value: Any) -> str:
    numeric = float(value)
    return f"{numeric:,.0f}" if numeric.is_integer() else f"{numeric:,.2f}"


def digest_date_label(timezone_name: str) -> str:
    return datetime.now(ZoneInfo(timezone_name)).strftime("%B %d, %Y").replace(" 0", " ")


def digest_text(
    added: list[dict[str, str]], removed: list[dict[str, str]], date_label: str
) -> str:
    lines = ["OpportunityRadar Job Update", date_label, "", "NEW JOBS", f"{len(added)} jobs added", ""]
    if added:
        for job in added:
            lines.extend(text_job_entry(job, "View Job"))
    else:
        lines.extend(["No jobs were added.", ""])
    lines.extend(["REMOVED JOBS", f"{len(removed)} jobs removed", ""])
    if removed:
        for job in removed:
            lines.extend(text_job_entry(job, "Original Posting"))
    else:
        lines.extend(["No jobs were removed.", ""])
    lines.extend(["SUMMARY", f"{len(added)} Added", f"{len(removed)} Removed"])
    return "\n".join(lines)


def text_job_entry(job: dict[str, str], link_label: str) -> list[str]:
    lines = [job["companyName"], job["title"], job["payDisplay"]]
    url = safe_job_url(job["sourceUrl"])
    lines.append(f"{link_label}: {url}" if url else "Posting URL unavailable")
    lines.append("")
    return lines


def digest_html(
    added: list[dict[str, str]], removed: list[dict[str, str]], date_label: str
) -> str:
    return f'''<!doctype html>
<html><body style="margin:0;background:#eef3f8;font-family:Arial,sans-serif;color:#1e2f43">
<div style="max-width:680px;margin:0 auto;background:#ffffff">
<header style="background:#163b67;padding:24px 28px;color:#ffffff">
<div style="font-size:22px;font-weight:700">OpportunityRadar Job Update</div>
<div style="margin-top:5px;color:#d9e7f5">{html.escape(date_label)}</div>
</header>
<main style="padding:24px 28px">
{html_section("NEW JOBS", len(added), "added", added, "View Job")}
{html_section("REMOVED JOBS", len(removed), "removed", removed, "Original Posting")}
<section style="margin-top:26px;padding:18px;background:#eef3f8;border-radius:6px">
<div style="font-size:13px;font-weight:700;letter-spacing:.08em;color:#52657a">SUMMARY</div>
<div style="margin-top:10px;font-size:18px"><strong>{len(added)}</strong> Added &nbsp;&nbsp; <strong>{len(removed)}</strong> Removed</div>
</section>
</main></div></body></html>'''


def html_section(
    heading: str, count: int, verb: str, jobs: list[dict[str, str]], link_label: str
) -> str:
    cards = "".join(html_job_card(job, link_label) for job in jobs)
    if not cards:
        cards = f'<p style="color:#52657a">No jobs were {html.escape(verb)}.</p>'
    return (
        f'<section style="margin-bottom:28px"><div style="font-size:13px;font-weight:700;'
        f'letter-spacing:.08em;color:#52657a">{html.escape(heading)}</div>'
        f'<h2 style="margin:7px 0 8px;font-size:21px;color:#163b67">{count} jobs {html.escape(verb)}</h2>'
        f'{cards}</section>'
    )


def html_job_card(job: dict[str, str], link_label: str) -> str:
    url = safe_job_url(job["sourceUrl"])
    link = (
        f'<a href="{html.escape(url, quote=True)}" style="display:inline-block;margin-top:10px;'
        f'color:#245b93;font-weight:700">{html.escape(link_label)}</a>'
        if url
        else '<div style="margin-top:10px;color:#738398">Posting URL unavailable</div>'
    )
    return (
        '<div style="padding:16px 0;border-bottom:1px solid #d5deea">'
        f'<div style="font-size:16px;font-weight:700;color:#27384d">{html.escape(job["companyName"])}</div>'
        f'<div style="margin-top:4px;font-size:18px;color:#163b67">{html.escape(job["title"])}</div>'
        f'<div style="margin-top:7px;color:#52657a">{html.escape(job["payDisplay"])}</div>'
        f'{link}</div>'
    )


def safe_job_url(value: str) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    return candidate if parsed.scheme in {"http", "https"} and parsed.hostname else ""


def test_email_html() -> str:
    return (
        '<div style="font-family:Arial,sans-serif;max-width:640px">'
        '<h1 style="color:#163b67">OpportunityRadar</h1>'
        '<p>Email delivery is configured and working.</p></div>'
    )
