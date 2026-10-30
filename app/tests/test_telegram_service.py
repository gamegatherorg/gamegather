import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src import database
from src.services.game_service import create_game, create_registration, start_gathering
from src.services.notification_methods import validate_notification_configuration
from src.services.notification_service import process_notifications
from src.services.telegram_service import (
    TelegramService, handle_update, telegram_request, validate_chat_id,
)


class TelegramTests(unittest.TestCase):
    def test_start_echoes_private_id(self) -> None:
        with patch("src.services.telegram_service.send_message") as send:
            handle_update({"message": {"chat": {"type": "private", "id": 123}, "text": "/start"}})
            self.assertEqual(send.call_args.args[0], "123")
            self.assertIn("123", send.call_args.args[1])
            handle_update({"message": {"chat": {"type": "group", "id": -123}, "text": "/start"}})
            self.assertEqual(send.call_count, 1)

    def test_targets_are_private_ids(self) -> None:
        self.assertEqual(validate_chat_id("123456"), "123456")
        for target in ("-123", "@username", "0", "", "abc"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_chat_id(target)

    def test_api_errors_are_safe_and_classified(self) -> None:
        for code, exception in ((403, ValueError), (429, RuntimeError), (500, RuntimeError)):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({
                "ok": False, "error_code": code, "description": "secret-token",
            }).encode()
            with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "secret-token"}), patch(
                "src.services.telegram_service.urlopen", return_value=response,
            ), self.assertRaises(exception) as caught:
                telegram_request("sendMessage", {})
            self.assertNotIn("secret-token", str(caught.exception))

    def test_queue_sends_private_links(self) -> None:
        env = {
            "TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "placeholder",
            "TELEGRAM_BOT_USERNAME": "gamegather_notifications_bot", "BASE_URL": "https://games.example",
        }
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, env, clear=True), patch.object(
            database, "DB_PATH", folder + "/test.db",
        ), patch("src.services.telegram_service.send_message") as send:
            validate_notification_configuration()
            database.init_db()
            game = create_game("Game", 1, "Organizer", "telegram", "111")
            create_registration(game["id"], "Player", "telegram", "222")
            start_gathering(game["organizer_url"].split("/")[-1])
            self.assertEqual(process_notifications(), 4)
            self.assertEqual(send.call_args_list[0].args[0], "111")
            self.assertIn("/organizer/games/", send.call_args_list[0].args[1])
            for index in (1, 3):
                self.assertEqual(send.call_args_list[index].args[0], "222")
                self.assertIn("/attendance/", send.call_args_list[index].args[1])
            self.assertIn("Manage your registration", send.call_args_list[1].args[1])


class TelegramLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_offsets_and_clean_shutdown(self) -> None:
        service = TelegramService()
        calls = []

        def request(method, payload):
            calls.append((method, payload))
            if method == "getWebhookInfo":
                return {"url": ""}
            if method == "getUpdates":
                if payload["offset"] == 0:
                    return [{"update_id": 10, "message": {"chat": {"type": "private", "id": 123}, "text": "/start"}}]
                service.stop.set()
                return []
            return {}

        with patch("src.services.telegram_service.telegram_request", side_effect=request), patch(
            "src.services.telegram_service.send_message",
        ) as send:
            await service.start()
            await asyncio.wait_for(service.task, timeout=2)
            await service.close()
            send.assert_called_once()
            polls = [payload for method, payload in calls if method == "getUpdates"]
            self.assertEqual([payload["offset"] for payload in polls], [0, 11])

    async def test_existing_webhook_fails_without_removing_it(self) -> None:
        service = TelegramService()
        with patch("src.services.telegram_service.telegram_request", return_value={"url": "https://example.com"}):
            with self.assertRaisesRegex(ValueError, "webhook configured"):
                await service.start()
            self.assertIsNone(service.task)
