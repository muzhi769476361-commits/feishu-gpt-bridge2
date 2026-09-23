import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import app as api


class FakeCursor:
    def __init__(self, rows, fetchone_results=None):
        self.rows = rows
        self.fetchone_results = list(fetchone_results or [])
        self.sql = ""
        self.params = []
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params or []
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.fetchone_results.pop(0) if self.fetchone_results else None


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
             timestamp.isoformat(), f"{index:032x}", timestamp, timestamp,
             0, "")
            for index in (3, 2, 1)
        ]
        cursor = FakeCursor(rows, fetchone_results=[(timestamp,), None])
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
        self.assertIn("COALESCE(message_at, received_at) >=", cursor.executed[1][0])
        self.assertIn("(COALESCE(message_at, received_at), id) <", cursor.executed[1][0])
        self.assertIn("ORDER BY COALESCE(message_at, received_at) DESC, id DESC", cursor.executed[1][0])
        self.assertFalse(response.json["collector"]["is_running"])
        self.assertEqual(response.json["range"]["anchor"], "latest_group_message")
        self.assertEqual(response.json["range"]["end_time"], timestamp.isoformat())

    def test_image_metadata_is_preserved(self):
        message = api.clean_message({
            "sender": "发送者",
            "content": "[图片]",
            "timestamp": "2026-09-19T23:22:34+08:00",
            "image_count": 1,
            "image_ocr": "图片中的公告文字",
        })
        self.assertEqual(message["image_count"], 1)
        self.assertEqual(message["image_ocr"], "图片中的公告文字")

    def test_sync_anchor_returns_latest_database_time(self):
        timestamp = datetime(2026, 9, 23, 1, 30, tzinfo=timezone.utc)
        cursor = FakeCursor([], fetchone_results=[(9, timestamp, "a" * 32)])
        with (
            patch.object(api, "ensure_schema"),
            patch.object(api, "db_connect", return_value=FakeConnection(cursor)),
        ):
            response = self.client.get("/sync-anchor", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["anchor"]["message_at"], timestamp.isoformat())

    def test_upload_batch_uses_authenticated_upsert(self):
        with patch.object(api, "store_message", side_effect=[True, False]):
            response = self.client.post(
                "/upload-batch",
                headers=self.headers,
                json={"messages": [
                    {"sender": "A", "content": "新消息", "timestamp":
                     "2026-09-23T09:31:00+08:00"},
                    {"sender": "A", "content": "重复消息", "timestamp":
                     "2026-09-23T09:30:00+08:00"},
                ]},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["stored"], 1)
        self.assertEqual(response.json["duplicates"], 1)

    def test_heartbeat_requires_key_and_stores_group_state(self):
        cursor = FakeCursor([])
        with (
            patch.object(api, "ensure_schema"),
            patch.object(api, "db_connect", return_value=FakeConnection(cursor)),
        ):
            denied = self.client.post("/heartbeat", json={"dom_message_count": 4})
            accepted = self.client.post(
                "/heartbeat",
                json={"group_name": "A独角兽综合群", "dom_message_count": 4},
                headers=self.headers,
            )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        self.assertIn("INSERT INTO collector_heartbeats", cursor.sql)


if __name__ == "__main__":
    unittest.main()
