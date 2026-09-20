"""Stateless Flask API for Render, backed by Supabase PostgreSQL."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from flask import Flask, jsonify, request


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
# X_API_KEY is the portable environment-variable spelling.  The hyphenated
# spelling is also accepted for platforms that allow it.
API_KEY = (os.getenv("X_API_KEY") or os.getenv("X-API-KEY") or "").strip()
DEFAULT_GROUP = os.getenv("DEFAULT_GROUP", "A独角兽综合群").strip()
LOCAL_TIMEZONE = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "Asia/Shanghai"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("feishu-api")

_schema_ready = False
_schema_lock = threading.Lock()


def db_connect() -> psycopg.Connection[Any]:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    # Supabase requires encrypted connections.  Use the Session Pooler URL
    # (port 5432) from the project's Connect dialog when Render needs IPv4.
    return psycopg.connect(
        DATABASE_URL,
        connect_timeout=15,
        autocommit=True,
        sslmode="require",
    )


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return

    with _schema_lock:
        if _schema_ready:
            return
        with db_connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS feishu_messages (
                    id BIGSERIAL PRIMARY KEY,
                    group_name TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    content TEXT NOT NULL,
                    "timestamp" TEXT NOT NULL,
                    msg_hash CHAR(32) NOT NULL,
                    message_at TIMESTAMPTZ,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cursor.execute(
                """
                ALTER TABLE feishu_messages
                    ADD COLUMN IF NOT EXISTS message_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ
                        NOT NULL DEFAULT NOW()
                """
            )
            cursor.execute(
                """
                UPDATE feishu_messages
                SET message_at = "timestamp"::TIMESTAMPTZ
                WHERE message_at IS NULL
                  AND "timestamp" ~
                      '^\\d{4}-\\d{2}-\\d{2}[T ][0-2]\\d:[0-5]\\d:[0-5]\\d'
                """
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    ux_feishu_messages_msg_hash
                ON feishu_messages (msg_hash)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    ix_feishu_messages_group_id
                ON feishu_messages (group_name, id DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    ix_feishu_messages_group_message_at
                ON feishu_messages
                    (group_name, message_at DESC, received_at DESC, id DESC)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS collector_heartbeats (
                    group_name TEXT PRIMARY KEY,
                    last_seen_at TIMESTAMPTZ NOT NULL,
                    dom_message_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        _schema_ready = True


def authorized() -> bool:
    if not API_KEY:
        LOGGER.error("X_API_KEY / X-API-KEY is not configured")
        return False
    supplied = request.headers.get("X-API-KEY", "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, API_KEY)


def calculate_msg_hash(sender: str, content: str, timestamp: str) -> str:
    raw = f"{sender}{content}{timestamp}".encode("utf-8", errors="replace")
    return hashlib.md5(raw, usedforsecurity=False).hexdigest()


def parse_datetime(value: str, field_name: str) -> datetime:
    """Parse an ISO-8601 value and normalize it to UTC."""
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be ISO-8601, for example "
            "2026-09-17T09:00:00+08:00"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(timezone.utc)


def optional_message_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return parse_datetime(value, "timestamp")
    except ValueError:
        return None


def clean_message(data: dict[str, Any]) -> dict[str, str]:
    group_name = str(data.get("group_name") or DEFAULT_GROUP).strip()[:200]
    sender = str(data.get("sender") or "未知发送者").strip()[:500]
    content = str(data.get("content") or "").strip()
    timestamp = str(data.get("timestamp") or data.get("time") or "").strip()[:200]
    calculated = calculate_msg_hash(sender, content, timestamp)
    supplied = str(data.get("msg_hash") or "").strip().lower()

    msg_hash = supplied if hmac.compare_digest(supplied, calculated) else calculated
    return {
        "group_name": group_name,
        "sender": sender,
        "content": content,
        "timestamp": timestamp,
        "msg_hash": msg_hash,
    }


def store_message(message: dict[str, str]) -> bool:
    """把清洗后的消息写入数据库，返回 True 表示新插入，False 表示重复。"""
    ensure_schema()
    with db_connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO feishu_messages
                (group_name, sender, content, "timestamp", msg_hash, message_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (msg_hash) DO NOTHING
            RETURNING id
            """,
            (
                message["group_name"],
                message["sender"],
                message["content"],
                message["timestamp"],
                message["msg_hash"],
                optional_message_datetime(message["timestamp"]),
            ),
        )
        inserted = cursor.fetchone()
    return bool(inserted)


@app.get("/")
@app.get("/health")
def health() -> Any:
    try:
        ensure_schema()
        with db_connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return jsonify({"status": "ok", "database": "supabase"}), 200
    except Exception as exc:
        LOGGER.exception("Database health check failed")
        return jsonify({"status": "error", "detail": str(exc)}), 503


@app.post("/upload")
def upload() -> Any:
    if not authorized():
        return jsonify({"status": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "invalid_json"}), 400

    message = clean_message(data)
    if not message["content"]:
        return jsonify({"status": "ignored", "reason": "empty_content"}), 400
    if len(message["content"]) > 200_000:
        return jsonify({"status": "ignored", "reason": "content_too_large"}), 413

    inserted = store_message(message)
    return jsonify(
        {
            "status": "stored" if inserted else "duplicate",
            "msg_hash": message["msg_hash"],
        }
    ), 201 if inserted else 200


@app.post("/heartbeat")
def heartbeat() -> Any:
    if not authorized():
        return jsonify({"status": "unauthorized"}), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "invalid_json"}), 400
    group_name = str(data.get("group_name") or DEFAULT_GROUP).strip()[:200]
    try:
        node_count = int(data.get("dom_message_count", 0))
    except (TypeError, ValueError):
        return jsonify({"status": "invalid_dom_message_count"}), 400
    if not group_name or not 0 <= node_count <= 100_000:
        return jsonify({"status": "invalid_heartbeat"}), 400
    ensure_schema()
    with db_connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO collector_heartbeats
                (group_name, last_seen_at, dom_message_count)
            VALUES (%s, NOW(), %s)
            ON CONFLICT (group_name) DO UPDATE SET
                last_seen_at = EXCLUDED.last_seen_at,
                dom_message_count = EXCLUDED.dom_message_count
            """,
            (group_name, node_count),
        )
    return jsonify({"status": "ok"}), 200


@app.get("/get-messages")
def get_messages() -> Any:
    if not authorized():
        return jsonify({"status": "unauthorized"}), 401

    requested_limit = request.args.get("limit", default=100, type=int) or 100
    limit = min(max(requested_limit, 1), 500)
    group_name = (
        request.args.get("group_name")
        or request.args.get("group")
        or DEFAULT_GROUP
    ).strip()[:200]
    before_id = request.args.get("before_id", type=int)

    try:
        start_time = (
            parse_datetime(request.args["start_time"], "start_time")
            if request.args.get("start_time")
            else None
        )
        end_time = (
            parse_datetime(request.args["end_time"], "end_time")
            if request.args.get("end_time")
            else None
        )
        hours_raw = request.args.get("hours", "").strip()
        hours = float(hours_raw) if hours_raw else None
        if hours is not None and not 0 < hours <= 24 * 366:
            raise ValueError("hours must be greater than 0 and at most 8784")
        if hours is not None and start_time is None:
            range_end = end_time or datetime.now(timezone.utc)
            start_time = range_end - timedelta(hours=hours)
        if start_time and end_time and start_time > end_time:
            raise ValueError("start_time must not be later than end_time")
    except ValueError as exc:
        return jsonify({"status": "invalid_time_range", "detail": str(exc)}), 400

    ensure_schema()
    sql = """
        SELECT id, group_name, sender, content, "timestamp", msg_hash,
               COALESCE(message_at, received_at) AS event_time,
               received_at
        FROM feishu_messages
        WHERE group_name = %s
    """
    params: list[Any] = [group_name]
    if start_time is not None:
        sql += " AND COALESCE(message_at, received_at) >= %s"
        params.append(start_time)
    if end_time is not None:
        sql += " AND COALESCE(message_at, received_at) <= %s"
        params.append(end_time)
    if before_id is not None:
        sql += " AND id < %s"
        params.append(before_id)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(limit + 1)

    with db_connect() as connection, connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT last_seen_at, dom_message_count
            FROM collector_heartbeats WHERE group_name = %s
            """,
            (group_name,),
        )
        heartbeat_row = cursor.fetchone()

    observed_at = datetime.now(timezone.utc)
    heartbeat_age = (
        max(0, int((observed_at - heartbeat_row[0]).total_seconds()))
        if heartbeat_row else None
    )

    has_more = len(rows) > limit
    rows = rows[:limit]
    messages = [
        {
            "id": row[0],
            "group_name": row[1],
            "sender": row[2],
            "content": row[3],
            "timestamp": row[4],
            "msg_hash": row[5].strip(),
            "event_time": row[6].isoformat(),
            "received_at": row[7].isoformat(),
        }
        for row in reversed(rows)
    ]
    return jsonify(
        {
            "group": group_name,
            "count": len(messages),
            "latest_messages": messages,
            "range": {
                "start_time": start_time.isoformat() if start_time else None,
                "end_time": end_time.isoformat() if end_time else None,
                "timezone": str(LOCAL_TIMEZONE),
            },
            "has_more": has_more,
            "next_before_id": messages[0]["id"] if has_more and messages else None,
            "collector": {
                "is_running": heartbeat_age is not None and heartbeat_age <= 180,
                "last_seen_at": heartbeat_row[0].isoformat() if heartbeat_row else None,
                "seconds_since_heartbeat": heartbeat_age,
                "dom_message_count": heartbeat_row[1] if heartbeat_row else None,
            },
            "observed_at": observed_at.isoformat(),
        }
    ), 200


@app.errorhandler(413)
def request_too_large(_error: Exception) -> Any:
    return jsonify({"status": "ignored", "reason": "request_too_large"}), 413


# ============================================================
# 飞书 Webhook 专用接口（已修复握手校验）
# ============================================================
@app.route("/feishu/webhook", methods=["POST"])
def feishu_webhook() -> Any:
    data = request.get_json(silent=True) or {}

    # 1) URL 校验：飞书首次保存请求地址时发来的 challenge，必须直接原样返回
    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]}), 200

    # 2) 事件回调：接收飞书群消息
    header = data.get("header") or {}
    event_type = header.get("event_type")

    if event_type != "im.message.receive_v1":
        return jsonify({"status": "ignored", "reason": "unsupported_event"}), 200

    try:
        event = data.get("event") or {}
        message = event.get("message") or {}
        sender_info = event.get("sender") or {}

        msg_type = message.get("message_type")
        if msg_type != "text":
            return jsonify({"status": "ignored", "reason": "non_text_message"}), 200

        content_raw = message.get("content") or "{}"
        try:
            content_json = (
                content_raw
                if isinstance(content_raw, dict)
                else json.loads(content_raw)
            )
        except Exception:
            content_json = {}
        text = str(content_json.get("text") or "").strip()

        if not text:
            return jsonify({"status": "ignored", "reason": "empty_content"}), 200

        sender_id = sender_info.get("sender_id") or {}
        sender = (
            sender_id.get("open_id")
            or sender_id.get("user_id")
            or sender_id.get("union_id")
            or "未知发送者"
        )

        chat_id = str(message.get("chat_id") or "").strip()
        group_name = chat_id or DEFAULT_GROUP

        create_time = message.get("create_time")
        if create_time:
            try:
                ts = datetime.fromtimestamp(
                    int(create_time) / 1000, tz=timezone.utc
                )
                timestamp = ts.astimezone(LOCAL_TIMEZONE).isoformat()
            except Exception:
                timestamp = datetime.now(LOCAL_TIMEZONE).isoformat()
        else:
            timestamp = datetime.now(LOCAL_TIMEZONE).isoformat()

        msg = clean_message(
            {
                "group_name": group_name,
                "sender": sender,
                "content": text,
                "timestamp": timestamp,
            }
        )
        inserted = store_message(msg)
        LOGGER.info(
            "Feishu webhook stored=%s group=%s sender=%s",
            inserted,
            group_name,
            sender,
        )
        return jsonify(
            {
                "status": "stored" if inserted else "duplicate",
                "msg_hash": msg["msg_hash"],
            }
        ), 200

    except Exception:
        LOGGER.exception("Failed to handle Feishu webhook event")
        return jsonify({"status": "error"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
