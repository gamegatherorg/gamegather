import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import database, main
from src.services.game_service import (
    create_game, create_registration, get_organizer_game, record_attendance,
    remove_registration, set_game_status, start_game, start_gathering,
)
from src.services.notification_service import notification_content
from src.services.sms_service import sms_content


class StartGameTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for patcher in (
            patch.object(database, "DB_PATH", folder.name + "/test.db"),
            patch.dict(os.environ, {
                "EMAIL_ENABLED": "true", "DISCORD_ENABLED": "true",
                "SMS_ENABLED": "true", "TELEGRAM_ENABLED": "true",
            }, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        database.init_db()
        self.game = create_game("Game", 1, "Organizer", "email", "organizer@example.com")
        self.token = self.game["organizer_url"].split("/")[-1]

    def register(self, name: str, method: str = "email", target: str = "player@example.com") -> dict:
        result = create_registration(self.game["id"], name, method, target)
        with database.get_connection() as conn:
            return dict(conn.execute("SELECT * FROM registrations WHERE id = ?", (result["id"],)).fetchone())

    def test_start_with_zero_confirmations_and_all_channels(self) -> None:
        for method, target in (("email", "player@example.com"), ("discord", "123"),
                               ("sms", "+46701234567"), ("telegram", "456")):
            self.register(method, method, target)
        start_gathering(self.token)
        self.assertEqual(start_game(self.token), {"status": "started"})
        with database.get_connection() as conn:
            notifications = conn.execute("SELECT * FROM notifications WHERE event_type = 'game_started'").fetchall()
        self.assertEqual({row["notification_method"] for row in notifications}, {"email", "discord", "sms", "telegram"})
        for row in notifications:
            body = sms_content(row) if row["notification_method"] == "sms" else notification_content(row)[1]
            self.assertIn("has started", body)
            self.assertIn("responses are now closed", body)
            self.assertNotIn("/attendance/", body)

    def test_only_unanswered_notified_and_changes_locked(self) -> None:
        coming, declined, waiting = [self.register(name) for name in ("Coming", "Declined", "Waiting")]
        start_gathering(self.token)
        record_attendance(coming["attendance_token"], "coming")
        record_attendance(declined["attendance_token"], "declined")
        start_game(self.token)
        with database.get_connection() as conn:
            rows = conn.execute("SELECT recipient_name FROM notifications WHERE event_type = 'game_started'").fetchall()
        self.assertEqual([row[0] for row in rows], ["Waiting"])
        for action in (
            lambda: start_game(self.token), lambda: start_gathering(self.token),
            lambda: set_game_status(self.game["id"], "open"),
            lambda: record_attendance(waiting["attendance_token"], "coming"),
            lambda: record_attendance(coming["attendance_token"], "declined"),
            lambda: remove_registration(waiting["id"], waiting["attendance_token"]), lambda: self.register("Late"),
        ):
            with self.assertRaises(ValueError):
                action()
        responses = [row["attendance_response"] for row in get_organizer_game(self.token)["registrations"]]
        self.assertEqual(responses, ["coming", "declined", None])
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications WHERE event_type = 'game_started'").fetchone()[0], 1)

    def test_invalid_token_and_open_game_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Organizer link not found"):
            start_game("invalid")
        with self.assertRaisesRegex(ValueError, "gathering"):
            start_game(self.token)

    def test_queue_failure_rolls_back_transition_and_all_messages(self) -> None:
        self.register("One")
        self.register("Two")
        start_gathering(self.token)
        with database.get_connection() as conn:
            conn.executescript("""
                CREATE TRIGGER reject_second_notice BEFORE INSERT ON notifications
                WHEN NEW.event_type = 'game_started' AND NEW.recipient_name = 'Two'
                BEGIN SELECT RAISE(ABORT, 'test failure'); END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            start_game(self.token)
        self.assertEqual(get_organizer_game(self.token)["game"]["status"], "gathering")
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications WHERE event_type = 'game_started'").fetchone()[0], 0)

    def test_routes_and_started_pages(self) -> None:
        player = self.register("Player")
        start_gathering(self.token)
        client = TestClient(main.app)
        self.assertIn("Start game", client.get(self.game["organizer_url"]).text)
        self.assertEqual(client.post("/organizer/games/invalid/start").status_code, 404)
        self.assertEqual(client.post(self.game["organizer_url"] + "/start-form", follow_redirects=False).status_code, 303)
        self.assertEqual(client.post(self.game["organizer_url"] + "/start").status_code, 400)
        organizer_page = client.get(self.game["organizer_url"]).text
        self.assertIn("This game has started", organizer_page)
        self.assertNotIn(">Start game<", organizer_page)
        page = client.get(self.game["public_url"], headers={"accept": "text/html"}).text
        self.assertIn("This game has started", page)
        attendance = client.get("/attendance/" + player["attendance_token"]).text
        self.assertIn("This game has started", attendance)
        self.assertNotIn('value="coming"', attendance)
        self.assertEqual(client.post("/attendance/" + player["attendance_token"], data={"response": "coming"}).status_code, 400)
