import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import app as api


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.params = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params or []

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


class MessageQueryTests(unittest.TestCase):
    def setUp(self):
        api.API_KEY = "test-key"
        self.client = api.app.test_client()
        self.headers = {"X-API-KEY": "test-key"}

    def test_rejects_reversed_range(self):
        response = self.client.get(
            "/get-messages",
            query_string={
                "start_time": "2026-09-17T12:00:00+08:00",
                "end_time": "2026-09-17T09:00:00+08:00",
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["status"], "invalid_time_range")

    def test_hours_filter_and_pagination(self):
        timestamp = datetime(2026, 9, 17, 1, 18, tzinfo=timezone.utc)
        rows = [
            (index, "A独角兽综合群", "发送者", f"消息{index}",
             timestamp.isoformat(), f"{index:032x}", timestamp, timestamp)
            for index in (3, 2, 1)
        ]
        cursor = FakeCursor(rows)
        with (
            patch.object(api, "ensure_schema"),
            patch.object(api, "db_connect", return_value=FakeConnection(cursor)),
        ):
            response = self.client.get(
                "/get-messages",
                query_string={"hours": "3", "limit": "2", "before_id": "10"},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["count"], 2)
        self.assertTrue(response.json["has_more"])
        self.assertEqual(response.json["next_before_id"], 2)
        self.assertIn("COALESCE(message_at, received_at) >=", cursor.sql)
        self.assertIn("id <", cursor.sql)


if __name__ == "__main__":
    unittest.main()
