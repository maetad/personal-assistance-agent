"""behavior-logger: silently classifies each conversation turn and writes structured habit
logs / semantic planning notes into Postgres. Registers only a lifecycle hook (post_llm_call)
- no tool, no system-prompt section, no memory-provider registration - so the main chat LLM
is never aware this exists and never sees anything injected back into its context.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("plugins.behavior-logger")

EMBED_MODEL_NAME = "all-MiniLM-L6-v2"
EMBED_DIM = 384
ADVISORY_LOCK_ID = 8_726_662_982 % 2_147_483_647  # arbitrary but stable int for pg_advisory_lock

_QUEUE_MAX = 256
_turn_queue: "queue.Queue[dict]" = queue.Queue(maxsize=_QUEUE_MAX)
_worker_lock = threading.Lock()
_worker: Optional[threading.Thread] = None
_schema_ready = threading.Event()

_embed_model = None  # lazily loaded SentenceTransformer

STRUCTURED_LOGS_DDL = """
CREATE TABLE IF NOT EXISTS structured_logs (
    id           BIGSERIAL PRIMARY KEY,
    profile_name TEXT NOT NULL,
    log_type     TEXT NOT NULL,
    data         JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
STRUCTURED_LOGS_IDX1 = (
    "CREATE INDEX IF NOT EXISTS idx_structured_logs_profile_type "
    "ON structured_logs (profile_name, log_type, occurred_at DESC)"
)
STRUCTURED_LOGS_IDX2 = (
    "CREATE INDEX IF NOT EXISTS idx_structured_logs_data_gin "
    "ON structured_logs USING GIN (data)"
)
SEMANTIC_MEMORIES_DDL = f"""
CREATE TABLE IF NOT EXISTS semantic_memories (
    id           BIGSERIAL PRIMARY KEY,
    profile_name TEXT NOT NULL,
    category     TEXT NOT NULL DEFAULT 'general',
    content      TEXT NOT NULL,
    embedding    VECTOR({EMBED_DIM}) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
SEMANTIC_MEMORIES_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_semantic_memories_profile "
    "ON semantic_memories (profile_name, created_at DESC)"
)

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["structured", "semantic"]},
                    "log_type": {"type": "string"},
                    "data": {"type": "object"},
                    "category": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["kind"],
            },
        }
    },
    "required": ["items"],
}

CLASSIFY_INSTRUCTIONS = (
    "Extract personal-tracking data points from this one chat turn. Return zero or more "
    "items. Use kind=\"structured\" for a concrete logged activity/metric (workouts, water "
    "intake, sleep, mood, habits, etc.) with log_type (short snake_case category) and data "
    "(flat JSON object of the relevant fields). Use kind=\"semantic\" for free-form "
    "planning/aspirational content worth semantic search later (trip planning, project "
    "ideas, goals) with category (short snake_case label) and content (the relevant text, "
    "lightly normalized, in the user's own words). Return {\"items\": []} if neither applies "
    "- most turns are ordinary chat and should return no items. Never invent facts that "
    "are not present in the message."
)


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("content") or "")
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and "text" in part:
                parts.append(str(part["text"]))
        return "\n".join(parts)
    return str(value)


def _db_connect():
    import psycopg
    from agent.secret_scope import get_secret

    dsn = get_secret("DATABASE_URL", os.environ.get("DATABASE_URL", ""))
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set - behavior-logger cannot connect to Postgres")
    return psycopg.connect(dsn)


def _ensure_schema() -> None:
    if _schema_ready.is_set():
        return
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_ID,))
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute(STRUCTURED_LOGS_DDL)
                cur.execute(STRUCTURED_LOGS_IDX1)
                cur.execute(STRUCTURED_LOGS_IDX2)
                cur.execute(SEMANTIC_MEMORIES_DDL)
                cur.execute(SEMANTIC_MEMORIES_IDX)
                conn.commit()
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))
                conn.commit()
    _schema_ready.set()


def _embed(text: str) -> list:
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer

        _embed_model = SentenceTransformer(EMBED_MODEL_NAME)
    vec = _embed_model.encode(text, normalize_embeddings=True)
    return vec.tolist()


def _vec_literal(vec) -> str:
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"


def _on_post_llm_call(user_message=None, assistant_response=None, session_id="", **_kwargs) -> None:
    """Hook callback - must return near-instantly (this hook blocks the live turn up to
    plugins.hook_callback_timeout while waiting for a result). Only enqueues; a background
    worker thread does the real classification + DB work."""
    user_text = _coerce_text(user_message)
    if not user_text.strip():
        return
    try:
        _turn_queue.put_nowait(
            {
                "session_id": session_id or "",
                "user_text": user_text,
                "assistant_text": _coerce_text(assistant_response),
                "sent_at": datetime.now(timezone.utc),
            }
        )
    except queue.Full:
        logger.warning("behavior-logger: queue full, dropping turn for session %s", session_id)


def _process_turn(ctx, turn: dict) -> None:
    result = ctx.llm.complete_structured(
        instructions=CLASSIFY_INSTRUCTIONS,
        input=[
            {
                "type": "text",
                "text": f"User: {turn['user_text']}\n\nAssistant: {turn['assistant_text']}",
            }
        ],
        json_schema=CLASSIFY_SCHEMA,
    )
    items = (result.parsed or {}).get("items") or []
    logger.debug("behavior-logger: classified %d item(s): %r", len(items), items)
    if not items:
        return

    from hermes_cli.profiles import get_active_profile_name

    profile_name = get_active_profile_name()

    with _db_connect() as conn:
        with conn.cursor() as cur:
            for item in items:
                kind = item.get("kind")
                if kind == "structured" and item.get("log_type"):
                    cur.execute(
                        "INSERT INTO structured_logs (profile_name, log_type, data, occurred_at) "
                        "VALUES (%s, %s, %s, %s)",
                        (
                            profile_name,
                            item["log_type"],
                            json.dumps(item.get("data") or {}),
                            turn["sent_at"],
                        ),
                    )
                elif kind == "semantic" and item.get("content"):
                    vec = _embed(item["content"])
                    cur.execute(
                        "INSERT INTO semantic_memories (profile_name, category, content, embedding) "
                        "VALUES (%s, %s, %s, %s::vector)",
                        (
                            profile_name,
                            item.get("category") or "general",
                            item["content"],
                            _vec_literal(vec),
                        ),
                    )
        conn.commit()


def _worker_loop(ctx) -> None:
    try:
        _ensure_schema()
    except Exception:
        logger.exception("behavior-logger: schema bootstrap failed")

    while True:
        turn = _turn_queue.get()
        try:
            _process_turn(ctx, turn)
        except Exception:
            logger.exception("behavior-logger: turn processing failed")
        finally:
            _turn_queue.task_done()


def _ensure_worker(ctx) -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(
                target=_worker_loop, args=(ctx,), name="behavior-logger", daemon=True
            )
            _worker.start()


def register(ctx) -> None:
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    _ensure_worker(ctx)
