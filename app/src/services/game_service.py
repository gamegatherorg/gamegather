import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.database import get_connection
from src.services.notification_service import queue_notification
from src.services.notification_methods import validate_notification_target


def create_game(
    title: str,
    min_players: int,
    organizer_name: str,
    organizer_notification_method: str,
    organizer_notification_target: str,
) -> dict[str, Any]:
    """Create a new game and queue an organizer confirmation notification."""
    organizer_notification_target = validate_notification_target(
        organizer_notification_method, organizer_notification_target,
    )
    organizer_token = secrets.token_urlsafe(24)
    created_at = datetime.utcnow().isoformat()

    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO games (
                title,
                status,
                min_players,
                organizer_name,
                organizer_notification_method,
                organizer_notification_target,
                organizer_token,
                created_at,
                updated_at
            ) VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                min_players,
                organizer_name,
                organizer_notification_method,
                organizer_notification_target,
                organizer_token,
                created_at,
                created_at,
            ),
        )
        conn.commit()
        game_id = cursor.lastrowid

    queue_notification(
        game_id=game_id,
        recipient_role="organizer",
        recipient_name=organizer_name,
        notification_method=organizer_notification_method,
        notification_target=organizer_notification_target,
        event_type="game_created",
    )

    return {
        "id": game_id,
        "public_url": f"/games/{game_id}",
        "organizer_url": f"/organizer/games/{organizer_token}",
    }


def list_open_games() -> list[dict[str, Any]]:
    """Fetch all open games."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT 
                id,
                title,
                status,
                min_players,
                organizer_name,
                created_at
            FROM games 
            WHERE status = 'open' 
            ORDER BY created_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

def get_game(game_id: int) -> dict[str, Any] | None:
    """Fetch one game and its registrations."""
    with get_connection() as conn:
        game = conn.execute(
            """
            SELECT 
                id,
                title,
                status,
                min_players,
                organizer_name,
                created_at
            FROM games WHERE id = ?
            """,
            (game_id,),
        ).fetchone()
        if not game:
            return None

        registrations = conn.execute(
            """
            SELECT id, game_id, display_name, attendance_response, responded_at
            FROM registrations WHERE game_id = ?
            """,
            (game_id,),
        ).fetchall()
        return {
            "game": dict(game),
            "registrations": [dict(row) for row in registrations],
        }


def get_organizer_game(organizer_token: str) -> dict[str, Any] | None:
    """Fetch a game and its registrations using its secret organizer token."""
    with get_connection() as conn:
        game = conn.execute(
            "SELECT * FROM games WHERE organizer_token = ?",
            (organizer_token,),
        ).fetchone()
        if not game:
            return None

        registrations = conn.execute(
            "SELECT * FROM registrations WHERE game_id = ?",
            (game["id"],),
        ).fetchall()
        return {
            "game": dict(game),
            "registrations": [dict(row) for row in registrations],
        }


def count_active_players(game_id: int) -> int:
    """Count current registrations for a game."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS player_count FROM registrations WHERE game_id = ?",
            (game_id,),
        ).fetchone()
        return int(row["player_count"] or 0)


def set_game_status(game_id: int, status: str) -> None:
    """Update the state of a game."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        game = conn.execute("SELECT status FROM games WHERE id = ?", (game_id,)).fetchone()
        if not game:
            raise ValueError("Game not found")
        if game["status"] == "started":
            raise ValueError("Game has started; status changes are locked")
        if status == "started":
            raise ValueError("Use the organizer start action to start the game")
        conn.execute(
            "UPDATE games SET status = ?, updated_at = ? WHERE id = ?",
            (status, datetime.utcnow().isoformat(), game_id),
        )
        conn.commit()


def create_registration(
    game_id: int,
    display_name: str,
    notification_method: str,
    notification_target: str,
) -> dict[str, Any]:
    """Add a registration to an open game."""
    notification_target = validate_notification_target(notification_method, notification_target)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        game = conn.execute(
            "SELECT * FROM games WHERE id = ?",
            (game_id,),
        ).fetchone()
        if not game:
            raise ValueError("Game not found")
        if game["status"] != "open":
            raise ValueError("Registration is closed for this game")

        return _insert_registration(
            conn, game, display_name, notification_method, notification_target,
        )


def create_organizer_registration(
    organizer_token: str,
    display_name: str,
) -> dict[str, Any]:
    """Add an in-person player with attendance confirmed by the organizer."""
    display_name = display_name.strip()
    if not display_name:
        raise ValueError("Please provide the player's name.")
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        game = conn.execute(
            "SELECT * FROM games WHERE organizer_token = ?",
            (organizer_token,),
        ).fetchone()
        if not game:
            raise ValueError("Organizer link not found")
        if game["status"] != "open":
            raise ValueError("Registration is closed for this game")
        return _insert_registration(conn, game, display_name, "none", "", "coming")


def _insert_registration(
    conn: sqlite3.Connection,
    game: sqlite3.Row,
    display_name: str,
    notification_method: str,
    notification_target: str,
    attendance_response: str | None = None,
) -> dict[str, Any]:
    """Insert a player, confirmation, and threshold notice in one transaction."""
    attendance_token = secrets.token_urlsafe(32)
    cursor = conn.execute(
        """
        INSERT INTO registrations (
            game_id, display_name, notification_method, notification_target,
            attendance_token, attendance_response, responded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            game["id"], display_name, notification_method, notification_target,
            attendance_token, attendance_response,
            datetime.now(timezone.utc).isoformat() if attendance_response else None,
        ),
    )
    if notification_method != "none":
        queue_notification(
            game_id=game["id"],
            recipient_role="registration",
            recipient_name=display_name,
            notification_method=notification_method,
            notification_target=notification_target,
            event_type="registration_created",
            action_token=attendance_token,
            connection=conn,
        )
    current_count = conn.execute(
        "SELECT COUNT(*) FROM registrations WHERE game_id = ?", (game["id"],),
    ).fetchone()[0]
    if current_count == int(game["min_players"]):
        queue_notification(
            game_id=game["id"],
            recipient_role="organizer",
            recipient_name=game["organizer_name"],
            notification_method=game["organizer_notification_method"],
            notification_target=game["organizer_notification_target"],
            event_type="minimum_players_reached",
            connection=conn,
        )

    return {"id": cursor.lastrowid}


def remove_registration(registration_id: int, attendance_token: str) -> None:
    """Remove an open-game registration authorized by its private token."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        registration = conn.execute(
            """
            SELECT r.id, g.status
            FROM registrations r
            JOIN games g ON g.id = r.game_id
            WHERE r.id = ? AND r.attendance_token = ?
            """,
            (registration_id, attendance_token),
        ).fetchone()
        if not registration:
            raise ValueError("Registration not found")
        if registration["status"] != "open":
            raise ValueError("Registration changes are locked for this game")

        conn.execute("DELETE FROM registrations WHERE id = ?", (registration_id,))
        conn.commit()


def start_gathering(organizer_token: str) -> dict[str, Any]:
    """Transition the organizer's game into gathering and queue notifications."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        game = conn.execute(
            "SELECT * FROM games WHERE organizer_token = ?",
            (organizer_token,),
        ).fetchone()
        if not game:
            raise ValueError("Organizer link not found")
        if game["status"] != "open":
            raise ValueError("Game is not open for gathering")

        game_id = game["id"]
        registrations = conn.execute(
            "SELECT * FROM registrations WHERE game_id = ?",
            (game_id,),
        ).fetchall()
        if len(registrations) < int(game["min_players"]):
            raise ValueError("Minimum players not reached")

        conn.execute(
            "UPDATE games SET status = 'gathering', updated_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), game_id),
        )
        conn.commit()

    for registration in registrations:
        if registration["notification_method"] == "none":
            continue
        queue_notification(
            game_id=game_id,
            recipient_role="registration",
            recipient_name=registration["display_name"],
            notification_method=registration["notification_method"],
            notification_target=registration["notification_target"],
            event_type="gathering_started",
            action_token=registration["attendance_token"],
        )

    return {"status": "gathering"}


def start_game(organizer_token: str) -> dict[str, Any]:
    """Finalize gathering and atomically notify only unanswered registrations."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        game = conn.execute(
            "SELECT id, status FROM games WHERE organizer_token = ?",
            (organizer_token,),
        ).fetchone()
        if not game:
            raise ValueError("Organizer link not found")
        if game["status"] != "gathering":
            raise ValueError("Only a gathering game can be started")
        registrations = conn.execute(
            """
            SELECT * FROM registrations
            WHERE game_id = ? AND attendance_response IS NULL AND notification_method != 'none'
            """,
            (game["id"],),
        ).fetchall()
        conn.execute(
            "UPDATE games SET status = 'started', updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), game["id"]),
        )
        for registration in registrations:
            queue_notification(
                game_id=game["id"],
                recipient_role="registration",
                recipient_name=registration["display_name"],
                notification_method=registration["notification_method"],
                notification_target=registration["notification_target"],
                event_type="game_started",
                connection=conn,
            )
    return {"status": "started"}


def get_attendance_registration(attendance_token: str) -> dict[str, Any] | None:
    """Fetch the attendance page data using its private response token."""
    with get_connection() as conn:
        registration = conn.execute(
            """
            SELECT r.id, r.display_name, r.attendance_response, r.responded_at,
                   g.id AS game_id, g.title AS game_title, g.status AS game_status
            FROM registrations r
            JOIN games g ON g.id = r.game_id
            WHERE r.attendance_token = ?
            """,
            (attendance_token,),
        ).fetchone()
        return dict(registration) if registration else None


def record_attendance(attendance_token: str, response: str) -> None:
    """Store a registration's attendance response using its private token."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        registration = conn.execute(
            """
            SELECT r.id, g.status
            FROM registrations r
            JOIN games g ON g.id = r.game_id
            WHERE r.attendance_token = ?
            """,
            (attendance_token,),
        ).fetchone()
        if not registration:
            raise ValueError("Attendance link not found")
        if registration["status"] != "gathering":
            raise ValueError("Attendance is not being collected for this game")

        conn.execute(
            "UPDATE registrations SET attendance_response = ?, responded_at = ? WHERE id = ?",
            (response, datetime.utcnow().isoformat(), registration["id"]),
        )
        conn.commit()
