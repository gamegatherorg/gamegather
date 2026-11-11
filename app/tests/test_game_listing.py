import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import database, main
from src.services.game_service import create_game, create_organizer_registration


class GameListingTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for patcher in (
            patch.object(database, "DB_PATH", folder.name + "/test.db"),
            patch.dict(os.environ, {"EMAIL_ENABLED": "true"}, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        database.init_db()
        game = create_game("Listed game", 3, "Organizer", "email", "organizer@example.com")
        self.organizer_token = game["organizer_url"].split("/")[-1]
        self.client = TestClient(main.app)

    def test_homepage_shows_remaining_players_clamped_at_zero(self) -> None:
        self.assertIn("3 of 3 players needed", self.client.get("/").text)

        create_organizer_registration(self.organizer_token, "Player one")
        create_organizer_registration(self.organizer_token, "Player two")
        self.assertIn("1 of 3 players needed", self.client.get("/").text)

        create_organizer_registration(self.organizer_token, "Player three")
        self.assertIn("0 of 3 players needed", self.client.get("/").text)
        create_organizer_registration(self.organizer_token, "Player four")
        homepage = self.client.get("/").text
        self.assertIn("0 of 3 players needed", homepage)
        self.assertNotIn("-1 of 3 players needed", homepage)
