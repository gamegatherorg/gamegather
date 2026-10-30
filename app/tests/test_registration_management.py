import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

from fastapi.testclient import TestClient

from src import database, main
from src.services.game_service import (
    count_active_players,
    create_game,
    create_registration,
    get_organizer_game,
    remove_registration,
    start_game,
    start_gathering,
)
from src.services.notification_service import notification_content, process_notifications


class RegistrationManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for patcher in (
            patch.object(database, "DB_PATH", folder.name + "/test.db"),
            patch.dict(os.environ, {
                "EMAIL_ENABLED": "true", "DISCORD_ENABLED": "true",
                "SMS_ENABLED": "true", "TELEGRAM_ENABLED": "true",
                "BASE_URL": "https://games.example",
                "VONAGE_API_KEY": "test-key", "VONAGE_API_SECRET": "test-secret",
                "VONAGE_SMS_FROM": "GameGather",
            }, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        database.init_db()
        self.game = create_game("Game", 1, "Organizer", "email", "organizer@example.com")
        self.organizer_token = self.game["organizer_url"].split("/")[-1]
        self.client = TestClient(main.app)

    def register(self, name: str = "Player", game_id: int | None = None) -> dict:
        result = create_registration(
            game_id or self.game["id"], name, "email", "player@example.com",
        )
        with database.get_connection() as conn:
            return dict(conn.execute(
                "SELECT * FROM registrations WHERE id = ?", (result["id"],),
            ).fetchone())

    def test_confirmations_delivered_by_all_four_providers(self) -> None:
        targets = {"email": "player@example.com", "discord": "123", "sms": "+46701234567", "telegram": "456"}
        for method, target in targets.items():
            create_registration(self.game["id"], method, method, target)
        with database.get_connection() as conn:
            notices = conn.execute(
                "SELECT * FROM notifications WHERE event_type = 'registration_created'",
            ).fetchall()
            tokens = [row[0] for row in conn.execute("SELECT attendance_token FROM registrations")]
        self.assertEqual(len(notices), 4)
        self.assertEqual(len(set(tokens)), 4)
        for notice in notices:
            self.assertIn(notice["action_token"], tokens)
            self.assertGreaterEqual(len(notice["action_token"]), 40)
            subject, body = notification_content(notice)
            self.assertIn("Registration confirmed", subject)
            self.assertIn("https://games.example/attendance/" + notice["action_token"], body)
            self.assertIn("Keep this link private", body)

        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "messages": [{"status": "0"}],
        }).encode()
        with patch("src.services.notification_service._smtp_settings", return_value={}), patch(
            "src.services.notification_service._send_email",
        ) as email, patch("src.services.discord_service._service") as discord, patch(
            "src.services.sms_service.urlopen", return_value=response,
        ) as sms, patch("src.services.telegram_service.send_message") as telegram:
            self.assertEqual(process_notifications(), 6)
        email_notices = [call.args[0] for call in email.call_args_list]
        self.assertEqual(sum(item["event_type"] == "registration_created" for item in email_notices), 1)
        self.assertIn("Manage your registration", discord.send_from_worker.call_args.args[1])
        sms_body = parse_qs(sms.call_args.args[0].data.decode())["text"][0]
        self.assertIn("Registered for Game", sms_body)
        self.assertIn("/attendance/", sms_body)
        self.assertIn("Manage your registration", telegram.call_args.args[1])

    def test_confirmation_failure_rolls_back_registration_and_threshold_notice(self) -> None:
        with database.get_connection() as conn:
            conn.executescript("""
                CREATE TRIGGER reject_confirmation BEFORE INSERT ON notifications
                WHEN NEW.event_type = 'registration_created'
                BEGIN SELECT RAISE(ABORT, 'test failure'); END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.register()
        self.assertEqual(count_active_players(self.game["id"]), 0)
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)

    def test_open_management_page_and_explicit_withdrawal(self) -> None:
        player = self.register()
        url = "/attendance/" + player["attendance_token"]
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Withdraw registration", page.text)
        self.assertNotIn('value="coming"', page.text)
        self.assertEqual(self.client.get(url + "/withdraw").status_code, 405)
        self.assertEqual(count_active_players(self.game["id"]), 1)
        self.assertEqual(self.client.post(url, data={"response": "coming"}).status_code, 400)
        response = self.client.post(url + "/withdraw")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Registration withdrawn", response.text)
        self.assertEqual(count_active_players(self.game["id"]), 0)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url + "/withdraw").status_code, 404)

    def test_threshold_failure_rolls_back_registration_and_confirmation(self) -> None:
        with database.get_connection() as conn:
            conn.executescript("""
                CREATE TRIGGER reject_threshold BEFORE INSERT ON notifications
                WHEN NEW.event_type = 'minimum_players_reached'
                BEGIN SELECT RAISE(ABORT, 'test failure'); END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.register()
        self.assertEqual(count_active_players(self.game["id"]), 0)
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)

    def test_deletion_requires_token_for_the_specific_registration(self) -> None:
        player = self.register()
        other = self.register("Other")
        other_game = create_game("Other game", 1, "Organizer", "email", "other@example.com")
        cross_game = self.register("Cross game", other_game["id"])
        url = f"/registrations/{player['id']}"
        self.assertEqual(self.client.delete(url).status_code, 422)
        for token in ("", "invalid", other["attendance_token"], cross_game["attendance_token"], self.organizer_token):
            response = self.client.delete(url, headers={"X-Registration-Token": token})
            self.assertEqual(response.status_code, 404)
        self.assertEqual(count_active_players(self.game["id"]), 2)
        with self.assertRaisesRegex(ValueError, "Registration not found"):
            remove_registration(player["id"], "invalid")
        self.assertEqual(self.client.post("/attendance/invalid/withdraw").status_code, 404)
        self.assertEqual(self.client.delete(url, headers={
            "X-Registration-Token": player["attendance_token"],
        }).status_code, 200)
        self.assertEqual(count_active_players(self.game["id"]), 1)

    def test_same_link_responds_in_gathering_and_locks_after_start(self) -> None:
        player = self.register()
        url = "/attendance/" + player["attendance_token"]
        start_gathering(self.organizer_token)
        for started in (False, True):
            if started:
                start_game(self.organizer_token)
            page = self.client.get(url).text
            self.assertNotIn("Withdraw registration", page)
            self.assertEqual(self.client.post(url + "/withdraw").status_code, 400)
            self.assertEqual(self.client.delete(f"/registrations/{player['id']}", headers={
                "X-Registration-Token": player["attendance_token"],
            }).status_code, 400)
            if not started:
                for response in ("coming", "declined"):
                    result = self.client.post(url, data={"response": response}, follow_redirects=False)
                    self.assertEqual(result.status_code, 303)
            else:
                self.assertNotIn('value="coming"', page)
                self.assertEqual(self.client.post(url, data={"response": "coming"}).status_code, 400)
        self.assertEqual(get_organizer_game(self.organizer_token)["registrations"][0]["attendance_response"], "declined")

    def test_form_and_api_registration_queue_confirmation(self) -> None:
        payload = {"display_name": "Player", "notification_method": "email", "notification_target": "player@example.com"}
        self.assertEqual(self.client.post(
            self.game["public_url"] + "/register-form", data=payload, follow_redirects=False,
        ).status_code, 303)
        self.assertEqual(self.client.post(
            self.game["public_url"] + "/register", json=payload,
        ).status_code, 200)
        with database.get_connection() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE event_type = 'registration_created'",
            ).fetchone()[0], 2)
