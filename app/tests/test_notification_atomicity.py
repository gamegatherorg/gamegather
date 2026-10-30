import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src import database
from src.services.game_service import (
    create_game,
    create_organizer_registration,
    create_registration,
    get_organizer_game,
    start_gathering,
)


class NotificationAtomicityTests(unittest.TestCase):
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

    def create_game(self) -> dict:
        return create_game("Game", 2, "Organizer", "email", "organizer@example.com")

    def test_creation_failure_rolls_back_game_and_confirmation_then_retries(self) -> None:
        with database.get_connection() as conn:
            conn.executescript("""
                CREATE TRIGGER reject_creation_notice BEFORE INSERT ON notifications
                WHEN NEW.event_type = 'game_created'
                BEGIN SELECT RAISE(ABORT, 'test failure'); END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.create_game()
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM games").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)
            conn.execute("DROP TRIGGER reject_creation_notice")

        game = self.create_game()
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM games").fetchone()[0], 1)
            notices = conn.execute("SELECT * FROM notifications").fetchall()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["game_id"], game["id"])
        self.assertEqual(notices[0]["event_type"], "game_created")
        self.assertEqual(notices[0]["status"], "queued")

    def test_gathering_failure_rolls_back_state_and_all_notices_then_retries(self) -> None:
        for failed_player in ("One", "Two"):
            with self.subTest(failed_player=failed_player):
                game = self.create_game()
                token = game["organizer_url"].split("/")[-1]
                for name in ("One", "Two"):
                    create_registration(game["id"], name, "email", "player@example.com")
                create_organizer_registration(token, "In person")
                before = get_organizer_game(token)["game"]
                with database.get_connection() as conn:
                    before_notices = [dict(row) for row in conn.execute(
                        "SELECT * FROM notifications WHERE game_id = ? ORDER BY id",
                        (game["id"],),
                    )]
                    # Restrict failure to gathering notices so existing confirmations
                    # and the threshold notice must survive unchanged.
                    conn.executescript(f"""
                        DROP TRIGGER IF EXISTS reject_gathering_notice;
                        CREATE TRIGGER reject_gathering_notice BEFORE INSERT ON notifications
                        WHEN NEW.event_type = 'gathering_started'
                            AND NEW.recipient_name = '{failed_player}'
                        BEGIN SELECT RAISE(ABORT, 'test failure'); END;
                    """)
                with self.assertRaises(sqlite3.IntegrityError):
                    start_gathering(token)
                self.assertEqual(get_organizer_game(token)["game"], before)
                with database.get_connection() as conn:
                    after_notices = [dict(row) for row in conn.execute(
                        "SELECT * FROM notifications WHERE game_id = ? ORDER BY id",
                        (game["id"],),
                    )]
                    self.assertEqual(after_notices, before_notices)
                    conn.execute("DROP TRIGGER reject_gathering_notice")

                self.assertEqual(start_gathering(token), {"status": "gathering"})
                self.assertEqual(get_organizer_game(token)["game"]["status"], "gathering")
                with database.get_connection() as conn:
                    notices = conn.execute(
                        "SELECT * FROM notifications WHERE game_id = ? "
                        "AND event_type = 'gathering_started' ORDER BY id",
                        (game["id"],),
                    ).fetchall()
                self.assertEqual([row["recipient_name"] for row in notices], ["One", "Two"])
                self.assertTrue(all(row["status"] == "queued" for row in notices))
                self.assertTrue(all(row["action_token"] for row in notices))
