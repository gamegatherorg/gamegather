import logging
import os
import smtplib
import ssl
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from smtplib import SMTPRecipientsRefused, SMTPResponseException
from typing import Any
from urllib.parse import quote

from src.database import get_connection
from src.services.notification_methods import require_enabled_method, send_notification


logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
MAX_BATCH_SIZE = 10
SMTP_TIMEOUT_SECONDS = 20
SENDING_LEASE = timedelta(minutes=2)


def queue_notification(
    game_id: int,
    recipient_role: str,
    recipient_name: str,
    notification_method: str,
    notification_target: str,
    event_type: str,
    action_token: str | None = None,
    *,
    connection: sqlite3.Connection | None = None,
) -> None:
    """Record a notification that should be sent later."""
    require_enabled_method(notification_method)
    def insert(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT INTO notifications (
                game_id,
                recipient_role,
                recipient_name,
                notification_method,
                notification_target,
                event_type,
                action_token,
                status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')
            """,
            (
                game_id,
                recipient_role,
                recipient_name,
                notification_method,
                notification_target,
                event_type,
                action_token,
            ),
        )
    if connection is not None:
        insert(connection)
    else:
        with get_connection() as conn:
            insert(conn)


def _smtp_settings() -> dict[str, Any]:
    host = os.environ["MAILJET_SMTP_HOST"]
    username = os.environ["MAILJET_SMTP_USERNAME"]
    password = os.environ["MAILJET_SMTP_PASSWORD"]
    sender = os.environ["MAIL_FROM_ADDRESS"]
    port = int(os.environ["MAILJET_SMTP_PORT"])
    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "sender": sender,
    }


def _notification_content(item: Any, game: Any, base_url: str) -> tuple[str, str]:
    game_title = game["title"]
    public_url = f"{base_url}/games/{game['id']}"

    if item["event_type"] == "game_created":
        organizer_url = f"{base_url}/organizer/games/{quote(game['organizer_token'])}"
        subject = f"Your GameGather game is listed: {game_title}"
        body = (
            f"Hi {item['recipient_name']},\n\n"
            f"Your game, {game_title}, has been created and is open for registrations.\n\n"
            f"Public game page: {public_url}\n"
            f"Private organizer page: {organizer_url}\n\n"
            "Keep the organizer link private; anyone with it can manage the game.\n"
        )
    elif item["event_type"] == "registration_created":
        management_url = f"{base_url}/attendance/{quote(item['action_token'])}"
        subject = f"Registration confirmed: {game_title}"
        body = (
            f"Hi {item['recipient_name']},\n\n"
            f"You have registered your interest in {game_title}.\n"
            f"Manage your registration: {management_url}\n\n"
            "You can withdraw while the game is open, or confirm or decline attendance "
            "once gathering starts. Keep this link private; anyone with it can manage "
            "your registration.\n"
        )
    elif item["event_type"] == "minimum_players_reached":
        organizer_url = f"{base_url}/organizer/games/{quote(game['organizer_token'])}"
        subject = f"Minimum players reached: {game_title}"
        body = (
            f"Hi {item['recipient_name']},\n\n"
            f"Your game, {game_title}, has reached its minimum of {game['min_players']} registrations.\n"
            f"You can review registrations and start gathering here: {organizer_url}\n"
        )
    elif item["event_type"] == "gathering_started":
        attendance_url = f"{base_url}/attendance/{quote(item['action_token'])}"
        subject = f"Gathering started: {game_title}"
        body = (
            f"Hi {item['recipient_name']},\n\n"
            f"The organizer has started gathering for {game_title}.\n"
            f"Please respond to the attendance request: {attendance_url}\n\n"
            f"Game page: {public_url}\n"
        )
    elif item["event_type"] == "game_started":
        subject = f"Game started: {game_title}"
        body = (
            f"Hi {item['recipient_name']},\n\n"
            f"{game_title} has started. Attendance responses are now closed.\n"
            f"Game page: {public_url}\n"
        )
    else:
        raise ValueError(f"Unsupported notification event: {item['event_type']}")

    return subject, body


def notification_content(item: Any) -> tuple[str, str]:
    """Build the shared message and private links for any delivery channel."""
    base_url = os.environ.get("BASE_URL", "http://localhost:3000").rstrip("/")
    with get_connection() as conn:
        game = conn.execute("SELECT * FROM games WHERE id = ?", (item["game_id"],)).fetchone()
    if not game:
        raise ValueError("Notification game no longer exists")

    return _notification_content(item, game, base_url)


def _send_email(item: Any, settings: dict[str, Any]) -> None:
    subject, body = notification_content(item)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings["sender"]
    message["To"] = item["notification_target"]
    message.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP(settings["host"], settings["port"], timeout=SMTP_TIMEOUT_SECONDS) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
        smtp.login(settings["username"], settings["password"])
        refused = smtp.send_message(message)
        if refused:
            raise SMTPRecipientsRefused(refused)


def _claim_notification() -> Any | None:
    now = datetime.now(timezone.utc).isoformat()
    lease_until = (datetime.now(timezone.utc) + SENDING_LEASE).isoformat()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute(
            """
            SELECT * FROM notifications
            WHERE (status = 'queued' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
               OR (status = 'sending' AND next_attempt_at <= ?)
            ORDER BY created_at, id
            LIMIT 1
            """,
            (now, now),
        ).fetchone()
        if not item:
            return None

        attempt_count = int(item["attempt_count"]) + 1
        conn.execute(
            "UPDATE notifications SET status = 'sending', attempt_count = ?, next_attempt_at = ? WHERE id = ?",
            (attempt_count, lease_until, item["id"]),
        )
        conn.commit()
        return dict(item) | {"attempt_count": attempt_count}


def _record_failure(item: dict[str, Any], error: Exception) -> None:
    error_message = str(error)[:1000] or error.__class__.__name__
    permanent = isinstance(error, ValueError) or (
        isinstance(error, SMTPResponseException) and 500 <= error.smtp_code < 600
    )
    if isinstance(error, SMTPRecipientsRefused):
        response_codes = [response[0] for response in error.recipients.values()]
        permanent = bool(response_codes) and all(500 <= code < 600 for code in response_codes)
    exhausted = item["attempt_count"] >= MAX_ATTEMPTS

    if permanent or exhausted:
        with get_connection() as conn:
            conn.execute(
                "UPDATE notifications SET status = 'failed', last_error = ?, next_attempt_at = NULL WHERE id = ?",
                (error_message, item["id"]),
            )
            conn.commit()
        logger.warning("Notification %s permanently failed: %s", item["id"], error_message)
        return

    delay_seconds = min(30 * (2 ** (item["attempt_count"] - 1)), 300)
    next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE notifications
            SET status = 'queued', last_error = ?, next_attempt_at = ?
            WHERE id = ?
            """,
            (error_message, next_attempt, item["id"]),
        )
        conn.commit()
    logger.warning("Notification %s failed; retry scheduled in %s seconds", item["id"], delay_seconds)


def process_notifications() -> int:
    """Send a batch of queued notifications and return the sent count."""
    sent_count = 0
    for _ in range(MAX_BATCH_SIZE):
        item = _claim_notification()
        if not item:
            break

        try:
            send_notification(item)
        except Exception as exc:
            _record_failure(item, exc)
            continue

        with get_connection() as conn:
            conn.execute(
                """
                UPDATE notifications
                SET status = 'sent', sent_at = CURRENT_TIMESTAMP, last_error = NULL, next_attempt_at = NULL
                WHERE id = ?
                """,
                (item["id"],),
            )
            conn.commit()
        sent_count += 1

    return sent_count
