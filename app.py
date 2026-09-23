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
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

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
                    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    image_count INTEGER NOT NULL DEFAULT 0,
                    image_ocr TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cursor.execute(
                """
                ALTER TABLE feishu_messages
                    ADD COLUMN IF NOT EXISTS message_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ
                        NOT NULL DEFAULT NOW(),
                    ADD COLUMN IF NOT EXISTS image_count INTEGER
                        NOT NULL DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS image_ocr TEXT
                        NOT NULL DEFAULT ''
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
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS collector_sync_status (
                    group_name TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at TIMESTAMPTZ,
                    last_success_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ,
                    anchor_message_at TIMESTAMPTZ,
                    uploaded_count INTEGER NOT NULL DEFAULT 0,
                    detail TEXT NOT NULL DEFAULT ''
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


def clean_message(data: dict[str, Any]) -> dict[str, Any]:
    group_name = str(data.get("group_name") or DEFAULT_GROUP).strip()[:200]
    sender = str(data.get("sender") or "未知发送者").strip()[:500]
    content = str(data.get("content") or "").strip()
    timestamp = str(data.get("timestamp") or data.get("time") or "").strip()[:200]
    calculated = calculate_msg_hash(sender, content, timestamp)
    supplied = str(data.get("msg_hash") or "").strip().lower()
    try:
        image_count = int(data.get("image_count") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("image_count must be an integer") from exc
    if not 0 <= image_count <= 20:
        raise ValueError("image_count must be between 0 and 20")
    image_ocr = str(data.get("image_ocr") or "").strip()[:100_000]

    msg_hash = supplied if hmac.compare_digest(supplied, calculated) else calculated
    return {
        "group_name": group_name,
        "sender": sender,
        "content": content,
        "timestamp": timestamp,
        "msg_hash": msg_hash,
        "image_count": image_count,
        "image_ocr": image_ocr,
    }


def store_message(message: dict[str, Any]) -> bool:
    """把清洗后的消息写入数据库，返回 True 表示新插入，False 表示重复。"""
    ensure_schema()
    with db_connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO feishu_messages
                (group_name, sender, content, "timestamp", msg_hash, message_at,
                 image_count, image_ocr)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (msg_hash) DO UPDATE SET
                image_count = GREATEST(
                    feishu_messages.image_count, EXCLUDED.image_count
                ),
                image_ocr = CASE
                    WHEN EXCLUDED.image_ocr <> '' THEN EXCLUDED.image_ocr
                    ELSE feishu_messages.image_ocr
                END
            WHERE EXCLUDED.image_count > feishu_messages.image_count
               OR (EXCLUDED.image_ocr <> ''
                   AND EXCLUDED.image_ocr <> feishu_messages.image_ocr)
            RETURNING id
            """,
            (
                message["group_name"],
                message["sender"],
                message["content"],
                message["timestamp"],
                message["msg_hash"],
                optional_message_datetime(message["timestamp"]),
                message["image_count"],
                message["image_ocr"],
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

    try:
        message = clean_message(data)
    except ValueError as exc:
        return jsonify({"status": "invalid_message", "detail": str(exc)}), 400
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


@app.get("/sync-anchor")
def sync_anchor() -> Any:
    """Return the newest stored event time used by a short-lived scraper."""
    if not authorized():
        return jsonify({"status": "unauthorized"}), 401
    group_name = (
        request.args.get("group_name")
        or request.args.get("group")
        or DEFAULT_GROUP
    ).strip()[:200]
    ensure_schema()
    with db_connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, COALESCE(message_at, received_at), msg_hash
            FROM feishu_messages
            WHERE group_name = %s
            ORDER BY COALESCE(message_at, received_at) DESC, id DESC
            LIMIT 1
            """,
            (group_name,),
        )
        row = cursor.fetchone()
    return jsonify({
        "group": group_name,
        "anchor": None if row is None else {
            "id": row[0],
            "message_at": row[1].isoformat(),
            "msg_hash": row[2].strip(),
        },
    }), 200


@app.post("/upload-batch")
def upload_batch() -> Any:
    """Validate and upsert one bounded increment in a single HTTP request."""
    if not authorized():
        return jsonify({"status": "unauthorized"}), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return jsonify({"status": "invalid_json"}), 400
    raw_messages = data["messages"]
    if len(raw_messages) > 100:
        return jsonify({"status": "too_many_messages", "maximum": 100}), 413

    cleaned: list[dict[str, Any]] = []
    try:
        for item in raw_messages:
            if not isinstance(item, dict):
                raise ValueError("each message must be an object")
            message = clean_message(item)
            if not message["content"]:
                raise ValueError("content must not be empty")
            if len(message["content"]) > 200_000:
                raise ValueError("content is too large")
            cleaned.append(message)
    except ValueError as exc:
        return jsonify({"status": "invalid_message", "detail": str(exc)}), 400

    stored = 0
    duplicates = 0
    for message in cleaned:
        if store_message(message):
            stored += 1
        else:
            duplicates += 1
    return jsonify({
        "status": "ok",
        "received": len(cleaned),
        "stored": stored,
        "duplicates": duplicates,
    }), 200


@app.post("/sync-status")
def sync_status() -> Any:
    if not authorized():
        return jsonify({"status": "unauthorized"}), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "invalid_json"}), 400
    group_name = str(data.get("group_name") or DEFAULT_GROUP).strip()[:200]
    status = str(data.get("status") or "").strip().lower()
    if status not in {"started", "success", "error"}:
        return jsonify({"status": "invalid_status"}), 400
    try:
        uploaded_count = max(0, min(int(data.get("uploaded_count") or 0), 100_000))
        anchor = optional_message_datetime(str(data.get("anchor_message_at") or ""))
    except (TypeError, ValueError):
        return jsonify({"status": "invalid_sync_status"}), 400
    detail = str(data.get("detail") or "").strip()[:2_000]
    ensure_schema()
    with db_connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO collector_sync_status
                (group_name, status, started_at, last_success_at, finished_at,
                 anchor_message_at, uploaded_count, detail)
            VALUES (
                %s, %s,
                CASE WHEN %s = 'started' THEN NOW() ELSE NULL END,
                CASE WHEN %s = 'success' THEN NOW() ELSE NULL END,
                CASE WHEN %s IN ('success', 'error') THEN NOW() ELSE NULL END,
                %s, %s, %s
            )
            ON CONFLICT (group_name) DO UPDATE SET
                status = EXCLUDED.status,
                started_at = CASE WHEN EXCLUDED.status = 'started'
                    THEN NOW() ELSE collector_sync_status.started_at END,
                last_success_at = CASE WHEN EXCLUDED.status = 'success'
                    THEN NOW() ELSE collector_sync_status.last_success_at END,
                finished_at = CASE WHEN EXCLUDED.status IN ('success', 'error')
                    THEN NOW() ELSE collector_sync_status.finished_at END,
                anchor_message_at = EXCLUDED.anchor_message_at,
                uploaded_count = EXCLUDED.uploaded_count,
                detail = EXCLUDED.detail
            """,
            (group_name, status, status, status, status, anchor,
             uploaded_count, detail),
        )
    return jsonify({"status": "ok"}), 200


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
        if start_time and end_time and start_time > end_time:
            raise ValueError("start_time must not be later than end_time")
    except ValueError as exc:
        return jsonify({"status": "invalid_time_range", "detail": str(exc)}), 400

    ensure_schema()
    anchor_message_at = None
    if hours is not None and start_time is None:
        if end_time is None:
            with db_connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT MAX(COALESCE(message_at, received_at))
                    FROM feishu_messages WHERE group_name = %s
                    """,
                    (group_name,),
                )
                anchor_row = cursor.fetchone()
            anchor_message_at = anchor_row[0] if anchor_row else None
            end_time = anchor_message_at
        if end_time is not None:
            start_time = end_time - timedelta(hours=hours)
    sql = """
        SELECT id, group_name, sender, content, "timestamp", msg_hash,
               COALESCE(message_at, received_at) AS event_time,
               received_at, image_count, image_ocr
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
        # The cursor is the last item from the previous time-sorted page.
        # An id-only comparison would skip/duplicate late backfills.
        sql += """
            AND (COALESCE(message_at, received_at), id) < (
                SELECT COALESCE(message_at, received_at), id
                FROM feishu_messages
                WHERE id = %s AND group_name = %s
            )
        """
        params.extend((before_id, group_name))
    sql += " ORDER BY COALESCE(message_at, received_at) DESC, id DESC LIMIT %s"
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
        cursor.execute(
            """
            SELECT status, started_at, last_success_at, finished_at,
                   uploaded_count, detail
            FROM collector_sync_status WHERE group_name = %s
            """,
            (group_name,),
        )
        sync_row = cursor.fetchone()

    observed_at = datetime.now(timezone.utc)
    heartbeat_age = (
        max(0, int((observed_at - heartbeat_row[0]).total_seconds()))
        if heartbeat_row else None
    )
    sync_success_age = (
        max(0, int((observed_at - sync_row[2]).total_seconds()))
        if sync_row and sync_row[2] else None
    )
    scheduled_healthy = (
        bool(sync_row)
        and (sync_row[0] == "started" or (
            sync_success_age is not None and sync_success_age <= 15 * 60
        ))
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
            "image_count": row[8],
            "image_ocr": row[9],
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
                "anchor": "latest_group_message" if anchor_message_at else None,
                "anchor_message_at": (
                    anchor_message_at.isoformat() if anchor_message_at else None
                ),
                "timezone": str(LOCAL_TIMEZONE),
            },
            "has_more": has_more,
            "next_before_id": messages[0]["id"] if has_more and messages else None,
            "collector": {
                "mode": "scheduled_incremental" if sync_row else "continuous_legacy",
                "is_running": scheduled_healthy or (
                    not sync_row and heartbeat_age is not None and heartbeat_age <= 180
                ),
                "last_seen_at": heartbeat_row[0].isoformat() if heartbeat_row else None,
                "seconds_since_heartbeat": heartbeat_age,
                "dom_message_count": heartbeat_row[1] if heartbeat_row else None,
                "sync_status": sync_row[0] if sync_row else None,
                "last_sync_started_at": (
                    sync_row[1].isoformat() if sync_row and sync_row[1] else None
                ),
                "last_sync_success_at": (
                    sync_row[2].isoformat() if sync_row and sync_row[2] else None
                ),
                "seconds_since_sync_success": sync_success_age,
                "last_sync_finished_at": (
                    sync_row[3].isoformat() if sync_row and sync_row[3] else None
                ),
                "last_uploaded_count": sync_row[4] if sync_row else None,
                "last_sync_detail": sync_row[5] if sync_row else None,
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
