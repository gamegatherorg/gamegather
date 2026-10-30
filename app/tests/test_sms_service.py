import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

from src import database
from src.services.game_service import create_game, create_registration, start_gathering
from src.services.notification_methods import validate_notification_configuration
from src.services.notification_service import process_notifications
from src.services.sms_service import validate_swedish_mobile


class SwedishNumberTests(unittest.TestCase):
    def test_valid_swedish_mobile(self) -> None:
        self.assertEqual(validate_swedish_mobile("+46701234567"), "+46701234567")

    def test_foreign_landline_and_local_formats_rejected(self) -> None:
        for target in ("+4791234567", "+46812345678", "0701234567", "+460701234567", "+46", "+4670 1234567"):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "Swedish mobile"):
                validate_swedish_mobile(target)


class SmsDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.environment = patch.dict(os.environ, {
            "SMS_ENABLED": "true", "VONAGE_API_KEY": "test-key",
            "VONAGE_API_SECRET": "test-secret", "VONAGE_SMS_FROM": "GameGather",
            "BASE_URL": "https://games.example",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        db_patch = patch.object(database, "DB_PATH", folder.name + "/test.db")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        database.init_db()
        self.game = create_game("BOTC", 1, "Organizer", "sms", "+46701234567")

    def response(self, status: str) -> MagicMock:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "messages": [{"status": status, "error-text": "test-secret"}],
        }).encode()
        return response

    def test_sms_only_startup(self) -> None:
        validate_notification_configuration()
        with patch.dict(os.environ, {}, clear=True):
            os.environ["SMS_ENABLED"] = "true"
            with self.assertRaisesRegex(ValueError, "VONAGE_API_KEY"):
                validate_notification_configuration()

    def test_accepted_message_payload_and_sent_state(self) -> None:
        with patch("src.services.sms_service.urlopen", return_value=self.response("0")) as send:
            self.assertEqual(process_notifications(), 1)
        payload = parse_qs(send.call_args.args[0].data.decode())
        self.assertEqual(payload["to"], ["46701234567"])
        self.assertEqual(payload["from"], ["GameGather"])
        self.assertIn("/organizer/games/", payload["text"][0])
        self.assertEqual(payload["type"], ["text"])
        with database.get_connection() as conn:
            row = conn.execute("SELECT * FROM notifications").fetchone()
            self.assertEqual(row["status"], "sent")
            self.assertIsNotNone(row["sent_at"])

    def test_throttling_retries_without_logging_provider_text(self) -> None:
        with patch("src.services.sms_service.urlopen", return_value=self.response("1")):
            self.assertEqual(process_notifications(), 0)
        with database.get_connection() as conn:
            row = conn.execute("SELECT * FROM notifications").fetchone()
            self.assertEqual(row["status"], "queued")
            self.assertIsNotNone(row["next_attempt_at"])
            self.assertNotIn("test-secret", row["last_error"])

    def test_permanent_api_rejection_is_failed(self) -> None:
        with patch("src.services.sms_service.urlopen", return_value=self.response("4")):
            self.assertEqual(process_notifications(), 0)
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT status FROM notifications").fetchone()[0], "failed")

    def test_gathering_sms_contains_private_attendance_link(self) -> None:
        create_registration(self.game["id"], "Player", "sms", "+46701234567")
        start_gathering(self.game["organizer_url"].split("/")[-1])
        with patch("src.services.sms_service.urlopen", return_value=self.response("0")) as send:
            self.assertEqual(process_notifications(), 4)
        payload = parse_qs(send.call_args.args[0].data.decode())
        self.assertIn("https://games.example/attendance/", payload["text"][0])

    def test_invalid_targets_create_no_rows(self) -> None:
        with self.assertRaises(ValueError):
            create_registration(self.game["id"], "Player", "sms", "+4791234567")
        with self.assertRaises(ValueError):
            create_game("Bad", 1, "Organizer", "sms", "0701234567")
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM registrations").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM games").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
