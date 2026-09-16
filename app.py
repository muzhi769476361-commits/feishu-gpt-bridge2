"""Stateless Flask API for Render, backed by Supabase PostgreSQL."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
from typing import Any

import psycopg
from flask import Flask, jsonify, request


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
# X_API_KEY is the portable environment-variable spelling.  The hyphenated
# spelling is also accepted for platforms that allow it.
API_KEY = (os.getenv("X_API_KEY") or os.getenv("X-API-KEY") or "").strip()
DEFAULT_GROUP = os.getenv("DEFAULT_GROUP", "A独角兽综合群").strip()

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
                    msg_hash CHAR(32) NOT NULL
                )
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


def clean_message(data: dict[str, Any]) -> dict[str, str]:
    group_name = str(data.get("group_name") or DEFAULT_GROUP).strip()[:200]
    sender = str(data.get("sender") or "未知发送者").strip()[:500]
    content = str(data.get("content") or "").strip()
    timestamp = str(data.get("timestamp") or data.get("time") or "").strip()[:200]
    calculated = calculate_msg_hash(sender, content, timestamp)
    supplied = str(data.get("msg_hash") or "").strip().lower()

    # Do not trust an arbitrary client hash.  Accept it only when it matches
    # the same formula used by the VPS.
    msg_hash = supplied if hmac.compare_digest(supplied, calculated) else calculated
    return {
        "group_name": group_name,
        "sender": sender,
        "content": content,
        "timestamp": timestamp,
        "msg_hash": msg_hash,
    }


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

    ensure_schema()
    with db_connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO feishu_messages
                (group_name, sender, content, "timestamp", msg_hash)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (msg_hash) DO NOTHING
            RETURNING id
            """,
            (
                message["group_name"],
                message["sender"],
                message["content"],
                message["timestamp"],
                message["msg_hash"],
            ),
        )
        inserted = cursor.fetchone()

    return jsonify(
        {
            "status": "stored" if inserted else "duplicate",
            "msg_hash": message["msg_hash"],
        }
    ), 201 if inserted else 200


@app.get("/get-messages")
def get_messages() -> Any:
    if not authorized():
        return jsonify({"status": "unauthorized"}), 401

    requested_limit = request.args.get("limit", default=20, type=int) or 20
    limit = min(max(requested_limit, 15), 30)
    group_name = (request.args.get("group") or DEFAULT_GROUP).strip()[:200]

    ensure_schema()
    with db_connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, group_name, sender, content, "timestamp", msg_hash
            FROM feishu_messages
            WHERE group_name = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (group_name, limit),
        )
        rows = cursor.fetchall()

    # Return the selected newest messages in chronological order for GPT.
    messages = [
        {
            "id": row[0],
            "group_name": row[1],
            "sender": row[2],
            "content": row[3],
            "timestamp": row[4],
            "msg_hash": row[5].strip(),
        }
        for row in reversed(rows)
    ]
    return jsonify(
        {
            "group": group_name,
            "count": len(messages),
            "latest_messages": messages,
        }
    ), 200


@app.errorhandler(413)
def request_too_large(_error: Exception) -> Any:
    return jsonify({"status": "ignored", "reason": "request_too_large"}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
