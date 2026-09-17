# feishu-gpt-bridge2

Render Flask API backed by Supabase PostgreSQL.

## Message queries

The existing latest-message request remains compatible:

```text
GET /get-messages?group_name=A独角兽综合群&limit=30
```

Recent hours:

```text
GET /get-messages?group_name=A独角兽综合群&hours=3&limit=100
```

Explicit Beijing-time range:

```text
GET /get-messages?start_time=2026-09-17T09:00:00+08:00&end_time=2026-09-17T12:00:00+08:00&limit=500
```

Requests authenticate with the `X-API-KEY` header. If `has_more` is true,
request the next page with the returned `next_before_id` as `before_id`.
feishu-gpt-bridge
