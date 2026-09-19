"""Unit tests for behavior-logger's pure/local logic (no Postgres, no LLM).
Run with: python3 -m unittest plugins.behavior_logger.test_behavior_logger -v
(or `python3 plugins/behavior-logger/test_behavior_logger.py` from repo root)
"""

import importlib.util
import json
import pathlib
import sys
import unittest
from datetime import datetime, timezone

_MODULE_PATH = pathlib.Path(__file__).parent / "__init__.py"
_spec = importlib.util.spec_from_file_location("behavior_logger", _MODULE_PATH)
bl = importlib.util.module_from_spec(_spec)
sys.modules["behavior_logger"] = bl
_spec.loader.exec_module(bl)


def _drain(q):
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except Exception:
            break
    return items


class CoerceTextTests(unittest.TestCase):
    def test_none(self):
        self.assertEqual(bl._coerce_text(None), "")

    def test_str_passthrough(self):
        self.assertEqual(bl._coerce_text("hi"), "hi")

    def test_dict_prefers_text_key(self):
        self.assertEqual(bl._coerce_text({"text": "a", "content": "b"}), "a")

    def test_dict_falls_back_to_content(self):
        self.assertEqual(bl._coerce_text({"content": "b"}), "b")

    def test_list_of_text_parts(self):
        value = ["a", {"text": "b"}, {"other": "ignored"}]
        self.assertEqual(bl._coerce_text(value), "a\nb")


class VecLiteralTests(unittest.TestCase):
    def test_formats_as_pgvector_literal(self):
        self.assertEqual(bl._vec_literal([1.0, -0.5]), "[1.00000000,-0.50000000]")


class BuildClassifySchemaTests(unittest.TestCase):
    def test_no_taxonomy_omits_fact_key_property(self):
        schema = bl._build_classify_schema([])
        item_props = schema["properties"]["items"]["items"]["properties"]
        self.assertNotIn("fact_key", item_props)

    def test_taxonomy_constrains_fact_key_to_closed_set(self):
        schema = bl._build_classify_schema(["weight", "blood_pressure"])
        fact_key_prop = schema["properties"]["items"]["items"]["properties"]["fact_key"]
        self.assertEqual(fact_key_prop["enum"], ["weight", "blood_pressure"])


class ResolveFactKeyTests(unittest.TestCase):
    def test_matches_taxonomy(self):
        item = {
            "kind": "structured",
            "log_type": "weight",
            "fact_key": "weight",
            "data": {"value": 70},
        }
        self.assertEqual(bl._resolve_fact_key(item, ["weight", "sleep"]), "weight")

    def test_missing_fact_key_returns_none(self):
        item = {"kind": "structured", "log_type": "workout"}
        self.assertIsNone(bl._resolve_fact_key(item, ["weight"]))

    def test_fact_key_not_in_taxonomy_returns_none(self):
        item = {"kind": "structured", "log_type": "weight", "fact_key": "made_up"}
        self.assertIsNone(bl._resolve_fact_key(item, ["weight"]))

    def test_empty_taxonomy_returns_none(self):
        item = {"kind": "structured", "log_type": "weight", "fact_key": "weight"}
        self.assertIsNone(bl._resolve_fact_key(item, []))

    def test_missing_data_returns_none(self):
        item = {"kind": "structured", "log_type": "weight", "fact_key": "weight"}
        self.assertIsNone(bl._resolve_fact_key(item, ["weight"]))

    def test_empty_data_returns_none(self):
        item = {"kind": "structured", "log_type": "weight", "fact_key": "weight", "data": {}}
        self.assertIsNone(bl._resolve_fact_key(item, ["weight"]))


class NormalizeFactKeyTests(unittest.TestCase):
    def test_valid_snake_case(self):
        self.assertEqual(bl._normalize_fact_key("weight"), "weight")

    def test_strips_and_lowercases(self):
        self.assertEqual(bl._normalize_fact_key("  Blood_Pressure  "), "blood_pressure")

    def test_digits_allowed_after_first_char(self):
        self.assertEqual(bl._normalize_fact_key("sleep_hours2"), "sleep_hours2")

    def test_rejects_leading_digit(self):
        self.assertIsNone(bl._normalize_fact_key("2fast"))

    def test_rejects_spaces(self):
        self.assertIsNone(bl._normalize_fact_key("blood pressure"))

    def test_rejects_empty_string(self):
        self.assertIsNone(bl._normalize_fact_key("   "))

    def test_rejects_non_string(self):
        self.assertIsNone(bl._normalize_fact_key(None))
        self.assertIsNone(bl._normalize_fact_key(123))

    def test_rejects_special_characters(self):
        self.assertIsNone(bl._normalize_fact_key("weight!"))


class _FakeCursor:
    def __init__(self, rows=None):
        self.executed = []
        self._rows = rows or []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, rows=None):
        self.cursor_obj = _FakeCursor(rows)
        self.committed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class HandleAddFactKeyTests(unittest.TestCase):
    def setUp(self):
        self._orig_connect = bl._db_connect
        self._orig_ensure_schema = bl._ensure_schema
        self._orig_profile = bl._get_active_profile_name
        self.conn = _FakeConn()
        bl._db_connect = lambda: self.conn
        bl._ensure_schema = lambda: None
        bl._get_active_profile_name = lambda: "pan"

    def tearDown(self):
        bl._db_connect = self._orig_connect
        bl._ensure_schema = self._orig_ensure_schema
        bl._get_active_profile_name = self._orig_profile

    def test_adds_valid_fact_key_and_commits_immediately(self):
        result = json.loads(bl._handle_add_fact_key({"fact_key": "weight"}))
        self.assertTrue(result["success"])
        self.assertEqual(result["fact_key"], "weight")
        self.assertTrue(self.conn.committed)
        sql, params = self.conn.cursor_obj.executed[0]
        self.assertIn("INSERT INTO fact_taxonomy", sql)
        self.assertEqual(params, ("pan", "weight"))

    def test_rejects_invalid_fact_key_without_touching_db(self):
        result = json.loads(bl._handle_add_fact_key({"fact_key": "not valid!"}))
        self.assertFalse(result["success"])
        self.assertIn("error", result)
        self.assertEqual(self.conn.cursor_obj.executed, [])
        self.assertFalse(self.conn.committed)

    def test_rejects_missing_fact_key(self):
        result = json.loads(bl._handle_add_fact_key({}))
        self.assertFalse(result["success"])


class HandleListFactKeysTests(unittest.TestCase):
    def setUp(self):
        self._orig_connect = bl._db_connect
        self._orig_ensure_schema = bl._ensure_schema
        self._orig_profile = bl._get_active_profile_name
        bl._ensure_schema = lambda: None
        bl._get_active_profile_name = lambda: "pan"

    def tearDown(self):
        bl._db_connect = self._orig_connect
        bl._ensure_schema = self._orig_ensure_schema
        bl._get_active_profile_name = self._orig_profile

    def test_lists_current_fact_keys(self):
        conn = _FakeConn(rows=[("weight",), ("sleep_hours",)])
        bl._db_connect = lambda: conn
        result = json.loads(bl._handle_list_fact_keys({}))
        self.assertTrue(result["success"])
        self.assertEqual(result["fact_keys"], ["weight", "sleep_hours"])

    def test_empty_taxonomy_returns_empty_list(self):
        conn = _FakeConn(rows=[])
        bl._db_connect = lambda: conn
        result = json.loads(bl._handle_list_fact_keys({}))
        self.assertEqual(result["fact_keys"], [])


class OnPostLlmCallTests(unittest.TestCase):
    def setUp(self):
        _drain(bl._turn_queue)

    def tearDown(self):
        _drain(bl._turn_queue)

    def test_blank_user_message_is_dropped(self):
        bl._on_post_llm_call(user_message="   ", assistant_response="reply", session_id="s1")
        self.assertEqual(_drain(bl._turn_queue), [])

    def test_enqueues_turn_with_utc_sent_at(self):
        before = datetime.now(timezone.utc)
        bl._on_post_llm_call(user_message="hi", assistant_response="hello", session_id="s1")
        after = datetime.now(timezone.utc)

        (turn,) = _drain(bl._turn_queue)
        self.assertEqual(turn["session_id"], "s1")
        self.assertEqual(turn["user_text"], "hi")
        self.assertEqual(turn["assistant_text"], "hello")
        self.assertIsInstance(turn["sent_at"], datetime)
        self.assertEqual(turn["sent_at"].tzinfo, timezone.utc)
        self.assertTrue(before <= turn["sent_at"] <= after)


if __name__ == "__main__":
    unittest.main()
