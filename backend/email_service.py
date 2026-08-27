from __future__ import annotations

import html
import base64
import hashlib
import hmac
import os
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.repository import OpportunityRepository, utc_now


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
            return default_settings()
        return settings_snapshot(row)

    def bootstrap_from_environment(self) -> bool:
        if self.get_settings()["configured"] or not os.environ.get("SMTP_PASSWORD"):
            return False
        self.save_settings({
            "smtpHost": os.environ.get("SMTP_HOST", ""), "smtpPort": int(os.environ.get("SMTP_PORT", "465")),
            "security": os.environ.get("SMTP_SECURITY", "ssl_tls"), "smtpUsername": os.environ.get("SMTP_USERNAME", ""),
            "smtpPassword": os.environ.get("SMTP_PASSWORD", ""), "fromEmail": os.environ.get("SMTP_FROM_EMAIL", ""),
            "fromName": os.environ.get("SMTP_FROM_NAME", "Opportunity Radar"), "replyToEmail": os.environ.get("SMTP_REPLY_TO", ""),
            "dailyEnabled": False, "recipientEmail": "", "sendAfterRefresh": True, "sendWhenEmpty": False,
        })
        return True

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self._settings_row()
        merged = {**default_settings(), **(settings_snapshot(current) if current else {}), **payload}
        security = str(merged.get("security") or "ssl_tls")
        if security not in {"ssl_tls", "starttls", "none"}:
            raise EmailConfigurationError("Choose SSL/TLS, STARTTLS, or None for SMTP security.")
        try:
            port = int(merged.get("smtpPort") or 0)
        except (TypeError, ValueError) as exc:
            raise EmailConfigurationError("SMTP Port must be a number between 1 and 65535.") from exc
        if not 1 <= port <= 65535:
            raise EmailConfigurationError("SMTP Port must be a number between 1 and 65535.")
        for field, label in (("fromEmail", "From Email"), ("replyToEmail", "Reply-To Email"), ("recipientEmail", "Recipient Email")):
            if merged.get(field) and not valid_email(str(merged[field])):
                raise EmailConfigurationError(f"Enter a valid {label}.")

        password = str(payload.get("smtpPassword") or "")
        ciphertext = self.cipher.encrypt(password) if password else str(current["smtp_password_ciphertext"] if current else "")
        now = utc_now()
        tracking_started = str(current["tracking_started_at"] if current else "") or now
        with self.repository.connection() as connection:
            connection.execute(
                """INSERT INTO email_settings
                (id,smtp_host,smtp_port,security,smtp_username,smtp_password_ciphertext,
                from_email,from_name,reply_to_email,daily_enabled,recipient_email,
                send_after_refresh,send_when_empty,tracking_started_at,updated_at)
                VALUES ('default',?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET smtp_host=excluded.smtp_host,smtp_port=excluded.smtp_port,
                security=excluded.security,smtp_username=excluded.smtp_username,
                smtp_password_ciphertext=excluded.smtp_password_ciphertext,from_email=excluded.from_email,
                from_name=excluded.from_name,reply_to_email=excluded.reply_to_email,
                daily_enabled=excluded.daily_enabled,recipient_email=excluded.recipient_email,
                send_after_refresh=excluded.send_after_refresh,send_when_empty=excluded.send_when_empty,
                tracking_started_at=excluded.tracking_started_at,updated_at=excluded.updated_at""",
                (
                    str(merged.get("smtpHost") or "").strip(), port, security,
                    str(merged.get("smtpUsername") or "").strip(), ciphertext,
                    str(merged.get("fromEmail") or "").strip(), str(merged.get("fromName") or "Opportunity Radar").strip(),
                    str(merged.get("replyToEmail") or "").strip(), int(bool(merged.get("dailyEnabled"))),
                    str(merged.get("recipientEmail") or "").strip(), int(bool(merged.get("sendAfterRefresh", True))),
                    int(bool(merged.get("sendWhenEmpty"))), tracking_started, now,
                ),
            )
        return self.get_settings()

    def send_test_email(self, recipient: str) -> dict[str, Any]:
        if not valid_email(recipient):
            raise EmailConfigurationError("Enter a valid test recipient email address.")
        settings = self._delivery_settings()
        self.send_email(
            settings, recipient, "Opportunity Radar Test Email",
            "Opportunity Radar email is configured and ready to send daily job updates.",
            test_email_html(),
        )
        return {"message": "Test email sent successfully."}

    def send_new_jobs_digest(self, *, trigger_type: str) -> dict[str, Any]:
        public_settings = self.get_settings()
        if trigger_type == "scheduled" and (not public_settings["dailyEnabled"] or not public_settings["sendAfterRefresh"]):
            return {"status": "Disabled", "jobCount": 0}
        settings = self._delivery_settings(require_recipient=True)
        jobs = self.pending_jobs(settings["trackingStartedAt"])
        digest_id = f"digest-{uuid4()}"
        started = utc_now()
        self._record_digest(digest_id, started, settings["recipientEmail"], len(jobs), "Sending", "", trigger_type)
        if not jobs and not settings["sendWhenEmpty"]:
            completed = utc_now()
            self._finish_digest(digest_id, completed, "Skipped - No New Jobs", "")
            return {"id": digest_id, "status": "Skipped - No New Jobs", "jobCount": 0}
        try:
            subject = f"Opportunity Radar: {len(jobs)} New {'Opportunity' if len(jobs) == 1 else 'Opportunities'}"
            if not jobs:
                subject = "Opportunity Radar Daily Update"
            self.send_email(
                settings, settings["recipientEmail"], subject,
                digest_text(jobs), digest_html(jobs),
            )
        except (EmailConfigurationError, EmailDeliveryError) as exc:
            completed = utc_now()
            self._finish_digest(digest_id, completed, "Failed", str(exc))
            return {"id": digest_id, "status": "Failed", "jobCount": len(jobs), "error": str(exc)}
        completed = utc_now()
        with self.repository.connection() as connection:
            connection.execute(
                "UPDATE email_digests SET completed_at=?,status='Success',error='' WHERE id=?",
                (completed, digest_id),
            )
            connection.executemany(
                "INSERT INTO email_digest_jobs (digest_id,job_id,sent_at) VALUES (?,?,?)",
                [(digest_id, job["id"], completed) for job in jobs],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO email_sent_jobs (job_id,digest_id,sent_at) VALUES (?,?,?)",
                [(job["id"], digest_id, completed) for job in jobs],
            )
        return {"id": digest_id, "status": "Success", "jobCount": len(jobs)}

    def send_email(self, settings: dict[str, Any], recipient: str, subject: str, text: str, html_body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((settings["fromName"], settings["fromEmail"]))
        message["To"] = recipient
        if settings.get("replyToEmail"):
            message["Reply-To"] = settings["replyToEmail"]
        message.set_content(text)
        message.add_alternative(html_body, subtype="html")
        context = ssl.create_default_context()
        try:
            if settings["security"] == "ssl_tls":
                with smtplib.SMTP_SSL(settings["smtpHost"], settings["smtpPort"], timeout=20, context=context) as smtp:
                    self._authenticate_and_send(smtp, settings, message)
            else:
                with smtplib.SMTP(settings["smtpHost"], settings["smtpPort"], timeout=20) as smtp:
                    smtp.ehlo()
                    if settings["security"] == "starttls":
                        smtp.starttls(context=context)
                        smtp.ehlo()
                    self._authenticate_and_send(smtp, settings, message)
        except smtplib.SMTPAuthenticationError as exc:
            raise EmailDeliveryError("Authentication failed. Check your SMTP username and password.") from exc
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            raise EmailDeliveryError("The email server could not be reached or did not accept the message. Check the SMTP settings and try again.") from exc

    def pending_jobs(self, tracking_started_at: str) -> list[dict[str, Any]]:
        with self.repository.connection(readonly=True) as connection:
            rows = connection.execute(
                """SELECT id FROM jobs j WHERE LOWER(status)='open' AND source_url<>''
                AND first_seen_at>=? AND NOT EXISTS (
                    SELECT 1 FROM email_sent_jobs sent WHERE sent.job_id=j.id
                ) ORDER BY first_seen_at,id""",
                (tracking_started_at,),
            ).fetchall()
        return [self.repository.get_job(row["id"]) for row in rows]

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.repository.connection(readonly=True) as connection:
            rows = connection.execute("SELECT * FROM email_digests ORDER BY started_at DESC,rowid DESC LIMIT ?", (limit,)).fetchall()
        return [digest_snapshot(row) for row in rows]

    def status(self) -> dict[str, Any]:
        settings = self.get_settings()
        history = self.history(1)
        with self.repository.connection(readonly=True) as connection:
            schedule = connection.execute("SELECT enabled,run_time,timezone FROM maintenance_schedules WHERE job_key='refresh-all-job-listings'").fetchone()
        return {
            "configured": settings["configured"], "dailyEnabled": settings["dailyEnabled"],
            "recipientEmail": settings["recipientEmail"], "lastEmail": history[0] if history else None,
            "scheduledRefreshEnabled": bool(schedule and schedule["enabled"]),
            "scheduledRefreshTime": schedule["run_time"] if schedule else "",
            "scheduledRefreshTimezone": schedule["timezone"] if schedule else "",
        }

    def _delivery_settings(self, *, require_recipient: bool = False) -> dict[str, Any]:
        row = self._settings_row()
        if row is None:
            raise EmailConfigurationError("Save the email provider settings first.")
        settings = settings_snapshot(row)
        required = [settings["smtpHost"], settings["smtpPort"], settings["smtpUsername"], settings["fromEmail"]]
        if not all(required) or not settings["hasSmtpPassword"]:
            raise EmailConfigurationError("Complete the SMTP host, port, username, password, and From Email settings.")
        if require_recipient and not settings["recipientEmail"]:
            raise EmailConfigurationError("Enter a recipient for the daily job email.")
        try:
            settings["smtpPassword"] = self.cipher.decrypt(str(row["smtp_password_ciphertext"]))
        except InvalidSecretToken as exc:
            raise EmailConfigurationError("The saved SMTP password cannot be read. Enter and save it again.") from exc
        return settings

    def _settings_row(self) -> Any:
        with self.repository.connection(readonly=True) as connection:
            return connection.execute("SELECT * FROM email_settings WHERE id='default'").fetchone()

    @staticmethod
    def _authenticate_and_send(smtp: Any, settings: dict[str, Any], message: EmailMessage) -> None:
        smtp.login(settings["smtpUsername"], settings["smtpPassword"])
        smtp.send_message(message)

    def _record_digest(self, digest_id: str, started: str, recipient: str, count: int, status: str, error: str, trigger: str) -> None:
        with self.repository.connection() as connection:
            connection.execute(
                "INSERT INTO email_digests (id,started_at,recipient,job_count,status,error,trigger_type) VALUES (?,?,?,?,?,?,?)",
                (digest_id, started, recipient, count, status, error, trigger),
            )

    def _finish_digest(self, digest_id: str, completed: str, status: str, error: str) -> None:
        with self.repository.connection() as connection:
            connection.execute("UPDATE email_digests SET completed_at=?,status=?,error=? WHERE id=?", (completed, status, error, digest_id))


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
        self.encryption_key = hmac.new(key, b"opportunity-radar-email-encryption", hashlib.sha256).digest()
        self.authentication_key = hmac.new(key, b"opportunity-radar-email-authentication", hashlib.sha256).digest()

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
            expected = hmac.new(self.authentication_key, nonce + ciphertext, hashlib.sha256).digest()
            if len(payload) < 48 or not hmac.compare_digest(tag, expected):
                raise InvalidSecretToken()
            return xor_bytes(ciphertext, keystream(self.encryption_key, nonce, len(ciphertext))).decode()
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidSecretToken() from exc


class InvalidSecretToken(Exception):
    pass


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
        "dailyEnabled": False, "recipientEmail": "", "sendAfterRefresh": True,
        "sendWhenEmpty": False, "hasSmtpPassword": False, "trackingStartedAt": "", "configured": False,
    }


def settings_snapshot(row: Any) -> dict[str, Any]:
    configured = bool(row["smtp_host"] and row["smtp_port"] and row["smtp_username"] and row["smtp_password_ciphertext"] and row["from_email"])
    return {
        "smtpHost": row["smtp_host"], "smtpPort": row["smtp_port"], "security": row["security"],
        "smtpUsername": row["smtp_username"], "fromEmail": row["from_email"], "fromName": row["from_name"],
        "replyToEmail": row["reply_to_email"], "dailyEnabled": bool(row["daily_enabled"]),
        "recipientEmail": row["recipient_email"], "sendAfterRefresh": bool(row["send_after_refresh"]),
        "sendWhenEmpty": bool(row["send_when_empty"]), "hasSmtpPassword": bool(row["smtp_password_ciphertext"]),
        "trackingStartedAt": row["tracking_started_at"], "configured": configured,
    }


def digest_snapshot(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"], "startedAt": row["started_at"], "completedAt": row["completed_at"],
        "recipient": row["recipient"], "jobCount": row["job_count"], "status": row["status"],
        "error": row["error"], "triggerType": row["trigger_type"],
    }


def valid_email(value: str) -> bool:
    parsed = parseaddr(value.strip())[1]
    return parsed == value.strip() and "@" in parsed and "." in parsed.rsplit("@", 1)[-1]


def digest_text(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return "Opportunity Radar Daily Update\n\nNo new opportunities were found during today's refresh."
    lines = ["Opportunity Radar", "", f"{len(jobs)} New Opportunities Found", ""]
    for job in jobs:
        lines.extend([
            str(job.get("title") or "Title Not Listed"), str(job.get("companyName") or "Company Not Listed"),
            useful_details(job), f"Resume Match: {match_text(job)}", str(job.get("sourceUrl") or ""), "",
        ])
    return "\n".join(lines)


def digest_html(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        body = "<p>No new opportunities were found during today's refresh.</p>"
    else:
        cards = []
        for job in jobs:
            url = html.escape(str(job.get("sourceUrl") or ""), quote=True)
            cards.append(
                f'<div style="padding:18px 0;border-bottom:1px solid #d5deea">'
                f'<h2 style="margin:0 0 5px;font-size:18px;color:#163b67">{html.escape(str(job.get("title") or "Title Not Listed"))}</h2>'
                f'<p style="margin:0 0 8px;font-weight:600;color:#27384d">{html.escape(str(job.get("companyName") or "Company Not Listed"))}</p>'
                f'<p style="margin:0 0 8px;color:#52657a">{html.escape(useful_details(job))}</p>'
                f'<p style="margin:0 0 12px;color:#52657a">Resume Match: {html.escape(match_text(job))}</p>'
                f'<a href="{url}" style="display:inline-block;padding:9px 14px;background:#245b93;color:white;text-decoration:none;border-radius:5px">View Job</a></div>'
            )
        body = "".join(cards)
    heading = f"{len(jobs)} New Opportunities Found" if jobs else "Daily Update"
    return f'''<!doctype html><html><body style="margin:0;background:#eef3f8;font-family:Arial,sans-serif;color:#1e2f43"><div style="max-width:680px;margin:0 auto;background:white"><header style="background:#163b67;padding:22px 28px;color:white"><strong style="font-size:21px">Opportunity Radar</strong></header><main style="padding:24px 28px"><h1 style="margin:0 0 12px;font-size:24px;color:#163b67">{heading}</h1>{body}</main></div></body></html>'''


def test_email_html() -> str:
    return '<div style="font-family:Arial,sans-serif;max-width:640px"><h1 style="color:#163b67">Opportunity Radar</h1><p>Email is configured and ready to send daily job updates.</p></div>'


def useful_details(job: dict[str, Any]) -> str:
    values = [job.get("location"), job.get("payText"), job.get("workType")]
    listed = [str(value) for value in values if value and str(value).casefold() not in {"not listed", "unknown"}]
    return " | ".join(listed) or "Details Not Listed"


def match_text(job: dict[str, Any]) -> str:
    score = job.get("matchScore")
    return f"{score}% {job.get('matchLabel', '')}".strip() if score is not None else "Not Available"
