import os
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-123")
os.environ.setdefault("DEMO_MODE", "true")

import db
from config import settings
from dashboard.server import app

class DashboardApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings.DATABASE_PATH = str(Path(self.tmp.name) / "test.db")
        await db.init_db()
        self.client = TestClient(app)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def test_get_sessions_empty(self) -> None:
        response = self.client.get("/api/sessions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_create_session_text(self) -> None:
        # Create a new session
        response = self.client.post(
            "/api/sessions/new",
            data={
                "description": "2018 BMW 320d, 95k km, manual, good service history, asking EUR 18,000",
                "user_id": 12345,
                "chat_id": 12345
            }
        )
        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertIn("session_id", json_data)
        self.assertEqual(json_data["phase"], "negotiating")
        
        # Verify the session is in the DB and matches the user_id filtering
        response_list = self.client.get("/api/sessions?user_id=12345")
        self.assertEqual(response_list.status_code, 200)
        sessions = response_list.json()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["user_id"], 12345)
        self.assertEqual(sessions[0]["chat_id"], 12345)
        self.assertIsNone(sessions[0]["thread_id"])  # thread_id should be None for user sessions

        # Other user should get empty sessions list
        response_other = self.client.get("/api/sessions?user_id=99999")
        self.assertEqual(response_other.json(), [])

    def test_create_session_resets_previous_active(self) -> None:
        # Create first session
        response1 = self.client.post(
            "/api/sessions/new",
            data={
                "description": "2018 BMW 320d, 95k km, manual, good service history, asking EUR 18,000",
                "user_id": 12345,
                "chat_id": 12345
            }
        )
        sess1_id = response1.json()["session_id"]

        # Create second session for same user
        response2 = self.client.post(
            "/api/sessions/new",
            data={
                "description": "2020 Audi A4, 50k km, automatic, asking EUR 25,000",
                "user_id": 12345,
                "chat_id": 12345
            }
        )
        sess2_id = response2.json()["session_id"]
        
        # Verify first session is now status='reset' and inactive
        response_list = self.client.get("/api/sessions?user_id=12345")
        sessions = response_list.json()
        self.assertEqual(len(sessions), 2)
        
        # Order is DESC, so second is index 0, first is index 1
        self.assertEqual(sessions[0]["id"], sess2_id)
        self.assertEqual(sessions[0]["status"], "active")
        
        self.assertEqual(sessions[1]["id"], sess1_id)
        self.assertEqual(sessions[1]["status"], "reset")
