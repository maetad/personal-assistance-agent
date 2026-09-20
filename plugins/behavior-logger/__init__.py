"""behavior-logger: classifies each conversation turn and writes structured habit logs /
semantic planning notes into Postgres via a lifecycle hook (post_llm_call) - classification
itself has no chat-visible surface. Separately, a pre_llm_call hook injects a pending Fact
Key Proposal (a Fact-shaped item the classifier saw that didn't match the taxonomy) into a
*later* turn's context, so the main agent can raise it with the user - it never grows the
taxonomy silently. It also registers chat-facing tools - add_fact_key, list_fact_keys, and
decline_fact_key_proposal - so a profile can manage its Fact Key taxonomy directly.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
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
FACT_TAXONOMY_DDL = """
CREATE TABLE IF NOT EXISTS fact_taxonomy (
    profile_name TEXT NOT NULL,
    fact_key     TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_name, fact_key)
)
"""
FACTS_DDL = """
CREATE TABLE IF NOT EXISTS facts (
    profile_name   TEXT NOT NULL,
    fact_key       TEXT NOT NULL,
    value          JSONB NOT NULL,
    observation_id BIGINT NOT NULL REFERENCES structured_logs (id),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_name, fact_key)
)
"""
FACT_KEY_PROPOSALS_DDL = """
CREATE TABLE IF NOT EXISTS fact_key_proposals (
    profile_name    TEXT NOT NULL,
    fact_key        TEXT NOT NULL,
    example_value   JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    last_surfaced_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_name, fact_key)
)
"""

CLASSIFY_ITEM_PROPERTIES = {
    "kind": {"type": "string", "enum": ["structured", "semantic"]},
    "log_type": {"type": "string"},
    "data": {"type": "object"},
    "category": {"type": "string"},
    "content": {"type": "string"},
    "proposed_fact_key": {
        "type": "string",
        "description": (
            "Only for kind=\"structured\" items that look like a Fact (a single "
            "current-value thing worth tracking, not just a one-off event) but don't match "
            "any known Fact Key: a short snake_case identifier to suggest as a new one. "
            "Never set together with fact_key."
        ),
    },
}


def _build_classify_schema(fact_keys: list) -> dict:
    """Build the classify JSON schema, constraining fact_key to the profile's current
    taxonomy (closed-set) when one exists, instead of letting the model invent a key."""
    item_properties = dict(CLASSIFY_ITEM_PROPERTIES)
    if fact_keys:
        item_properties["fact_key"] = {"type": "string", "enum": list(fact_keys)}
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": item_properties,
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
    "(flat JSON object of the relevant fields). If the item matches one of the known Fact "
    "Keys for this profile, also set fact_key to that exact value; otherwise, if it looks "
    "like a Fact (a single current-value thing worth tracking) but doesn't match any known "
    "Fact Key, set proposed_fact_key to a short snake_case suggestion instead (never set "
    "both fact_key and proposed_fact_key; never invent a fact_key that isn't a known Fact "
    "Key). Use kind=\"semantic\" for free-form planning/aspirational content worth semantic "
    "search later (trip planning, project ideas, goals) with category (short snake_case "
    "label) and content (the relevant text, lightly normalized, in the user's own words). "
    "Return {\"items\": []} if neither applies - most turns are ordinary chat and should "
    "return no items. Never invent facts that are not present in the message."
)


def _resolve_fact_key(item: dict, taxonomy_fact_keys: list) -> Optional[str]:
    """Return item's fact_key only if it's a real member of the profile's current taxonomy
    (closed-set match) - guards against a hallucinated or stale key reaching the Facts
    table, even if the schema constraint above is somehow bypassed. Also requires non-empty
    data, so a fact-shaped item with nothing to store can't clobber the existing Fact value."""
    fact_key = item.get("fact_key")
    if fact_key and fact_key in taxonomy_fact_keys and item.get("data"):
        return fact_key
    return None


def _resolve_proposal_fact_key(item: dict, taxonomy_fact_keys: list) -> Optional[str]:
    """Return a normalized proposed_fact_key when the classifier flagged a Fact-shaped
    structured item that didn't match the closed-set taxonomy - queued as a Fact Key
    Proposal for user confirmation rather than silently dropped or auto-added."""
    if item.get("kind") != "structured" or not item.get("data"):
        return None
    if _resolve_fact_key(item, taxonomy_fact_keys):
        return None
    proposed = _normalize_fact_key(item.get("proposed_fact_key"))
    if proposed and proposed not in taxonomy_fact_keys:
        return proposed
    return None


def _format_proposal_context(fact_key: str, example_value: Any) -> str:
    return (
        "Pending Fact Key Proposal from background classification (the user did not ask "
        f"for this): '{fact_key}', seen with example value {json.dumps(example_value)}. "
        "Raise it with the user now and ask if they want it tracked as a Fact Key going "
        f"forward. Call add_fact_key(fact_key=\"{fact_key}\") only if they confirm; call "
        f"decline_fact_key_proposal(fact_key=\"{fact_key}\") if they decline. Don't ask "
        "again this same turn if you already have."
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


def _db_connect(connect_timeout: Optional[int] = None):
    import psycopg
    from agent.secret_scope import get_secret

    dsn = get_secret("DATABASE_URL", os.environ.get("DATABASE_URL", ""))
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set - behavior-logger cannot connect to Postgres")
    if connect_timeout:
        return psycopg.connect(dsn, connect_timeout=connect_timeout)
    return psycopg.connect(dsn)


def _get_active_profile_name() -> str:
    from hermes_cli.profiles import get_active_profile_name

    return get_active_profile_name()


FACT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

ADD_FACT_KEY_SCHEMA = {
    "name": "add_fact_key",
    "description": (
        "Add a new Fact Key to this profile's taxonomy, so the background classifier can "
        "track it as a single current value (e.g. weight, sleep_hours) on future turns "
        "instead of only ever logging observations. Commits immediately - no confirmation step."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fact_key": {
                "type": "string",
                "description": "Short snake_case identifier for the Fact Key, e.g. 'weight' or 'sleep_hours'.",
            }
        },
        "required": ["fact_key"],
        "additionalProperties": False,
    },
}

LIST_FACT_KEYS_SCHEMA = {
    "name": "list_fact_keys",
    "description": "List this profile's current Fact Keys - the closed-set taxonomy the classifier matches against.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

DECLINE_FACT_KEY_PROPOSAL_SCHEMA = {
    "name": "decline_fact_key_proposal",
    "description": (
        "Decline a pending Fact Key Proposal the background classifier raised, so it isn't "
        "surfaced again and the taxonomy stays unchanged. Call after the user says no to "
        "tracking a proposed Fact Key."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fact_key": {
                "type": "string",
                "description": "The proposed Fact Key being declined, exactly as raised.",
            }
        },
        "required": ["fact_key"],
        "additionalProperties": False,
    },
}


def _normalize_fact_key(raw: Any) -> Optional[str]:
    """Lowercase/strip a fact_key candidate; None if it isn't a snake_case identifier."""
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().lower()
    if not FACT_KEY_RE.match(candidate):
        return None
    return candidate


def _handle_add_fact_key(args: dict, **_kw) -> str:
    from tools.registry import tool_result

    fact_key = _normalize_fact_key(args.get("fact_key"))
    if not fact_key:
        return tool_result(
            {"success": False, "error": "fact_key must be a snake_case identifier, e.g. 'weight'"}
        )

    _ensure_schema()
    profile_name = _get_active_profile_name()
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fact_taxonomy (profile_name, fact_key) VALUES (%s, %s) "
                "ON CONFLICT (profile_name, fact_key) DO NOTHING",
                (profile_name, fact_key),
            )
            cur.execute(
                "UPDATE fact_key_proposals SET status = 'confirmed', updated_at = now() "
                "WHERE profile_name = %s AND fact_key = %s AND status = 'pending'",
                (profile_name, fact_key),
            )
        conn.commit()
    return tool_result({"success": True, "fact_key": fact_key})


def _handle_decline_fact_key_proposal(args: dict, **_kw) -> str:
    from tools.registry import tool_result

    fact_key = _normalize_fact_key(args.get("fact_key"))
    if not fact_key:
        return tool_result(
            {"success": False, "error": "fact_key must be a snake_case identifier, e.g. 'weight'"}
        )

    _ensure_schema()
    profile_name = _get_active_profile_name()
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fact_key_proposals SET status = 'declined', updated_at = now() "
                "WHERE profile_name = %s AND fact_key = %s AND status = 'pending'",
                (profile_name, fact_key),
            )
        conn.commit()
    return tool_result({"success": True, "fact_key": fact_key})


def _handle_list_fact_keys(args: dict, **_kw) -> str:
    from tools.registry import tool_result

    _ensure_schema()
    profile_name = _get_active_profile_name()
    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fact_key FROM fact_taxonomy WHERE profile_name = %s ORDER BY fact_key",
                (profile_name,),
            )
            fact_keys = [row[0] for row in cur.fetchall()]
    return tool_result({"success": True, "fact_keys": fact_keys})


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
                cur.execute(FACT_TAXONOMY_DDL)
                cur.execute(FACTS_DDL)
                cur.execute(FACT_KEY_PROPOSALS_DDL)
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


_PROPOSAL_RESURFACE_COOLDOWN_SQL = "last_surfaced_at IS NULL OR last_surfaced_at < now() - interval '1 day'"


def _on_pre_llm_call(session_id=None, **_kwargs) -> Optional[dict]:
    """Fires before the main agent's turn. Surfaces the oldest pending Fact Key Proposal
    (if any, and not re-asked within the last day) as injected user-message context, so
    the agent raises it with the user on this turn - always a later turn than the one that
    raised it, since the proposal is only written by the background classifier after that
    earlier turn's response was already sent. Fails open (returns None) on any error or a
    slow/unreachable DB, via a short connect_timeout, so this never blocks the live turn.
    ponytail: no per-query statement_timeout yet - add one if a slow query is ever seen."""
    try:
        _ensure_schema()
        profile_name = _get_active_profile_name()
        with _db_connect(connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT fact_key, example_value FROM fact_key_proposals "
                    f"WHERE profile_name = %s AND status = 'pending' AND ({_PROPOSAL_RESURFACE_COOLDOWN_SQL}) "
                    "ORDER BY created_at ASC LIMIT 1",
                    (profile_name,),
                )
                row = cur.fetchone()
            if not row:
                return None
            fact_key, example_value = row
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE fact_key_proposals SET last_surfaced_at = now() "
                    "WHERE profile_name = %s AND fact_key = %s",
                    (profile_name, fact_key),
                )
            conn.commit()
        return {"context": _format_proposal_context(fact_key, example_value)}
    except Exception:
        logger.exception("behavior-logger: pre_llm_call proposal lookup failed")
        return None


def _process_turn(ctx, turn: dict) -> None:
    profile_name = _get_active_profile_name()

    with _db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fact_key FROM fact_taxonomy WHERE profile_name = %s",
                (profile_name,),
            )
            taxonomy_fact_keys = [row[0] for row in cur.fetchall()]

    result = ctx.llm.complete_structured(
        instructions=CLASSIFY_INSTRUCTIONS,
        input=[
            {
                "type": "text",
                "text": f"User: {turn['user_text']}\n\nAssistant: {turn['assistant_text']}",
            }
        ],
        json_schema=_build_classify_schema(taxonomy_fact_keys),
    )
    items = (result.parsed or {}).get("items") or []
    logger.debug("behavior-logger: classified %d item(s): %r", len(items), items)
    if not items:
        return

    with _db_connect() as conn:
        with conn.cursor() as cur:
            for item in items:
                kind = item.get("kind")
                if kind == "structured" and item.get("log_type"):
                    data_json = json.dumps(item.get("data") or {})
                    cur.execute(
                        "INSERT INTO structured_logs (profile_name, log_type, data, occurred_at) "
                        "VALUES (%s, %s, %s, %s) RETURNING id",
                        (profile_name, item["log_type"], data_json, turn["sent_at"]),
                    )
                    observation_id = cur.fetchone()[0]
                    fact_key = _resolve_fact_key(item, taxonomy_fact_keys)
                    if fact_key:
                        cur.execute(
                            "INSERT INTO facts (profile_name, fact_key, value, observation_id, updated_at) "
                            "VALUES (%s, %s, %s, %s, now()) "
                            "ON CONFLICT (profile_name, fact_key) DO UPDATE SET "
                            "value = EXCLUDED.value, observation_id = EXCLUDED.observation_id, "
                            "updated_at = EXCLUDED.updated_at",
                            (profile_name, fact_key, data_json, observation_id),
                        )
                    else:
                        proposed_key = _resolve_proposal_fact_key(item, taxonomy_fact_keys)
                        if proposed_key:
                            cur.execute(
                                "INSERT INTO fact_key_proposals "
                                "(profile_name, fact_key, example_value, updated_at) "
                                "VALUES (%s, %s, %s, now()) "
                                "ON CONFLICT (profile_name, fact_key) DO UPDATE SET "
                                "example_value = EXCLUDED.example_value, updated_at = now() "
                                "WHERE fact_key_proposals.status = 'pending'",
                                (profile_name, proposed_key, data_json),
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
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_tool(
        name="add_fact_key", toolset="behavior-logger", schema=ADD_FACT_KEY_SCHEMA,
        handler=_handle_add_fact_key, description=ADD_FACT_KEY_SCHEMA["description"], emoji="🏷️",
    )
    ctx.register_tool(
        name="list_fact_keys", toolset="behavior-logger", schema=LIST_FACT_KEYS_SCHEMA,
        handler=_handle_list_fact_keys, description=LIST_FACT_KEYS_SCHEMA["description"], emoji="📋",
    )
    ctx.register_tool(
        name="decline_fact_key_proposal", toolset="behavior-logger",
        schema=DECLINE_FACT_KEY_PROPOSAL_SCHEMA, handler=_handle_decline_fact_key_proposal,
        description=DECLINE_FACT_KEY_PROPOSAL_SCHEMA["description"], emoji="🚫",
    )
    _ensure_worker(ctx)
