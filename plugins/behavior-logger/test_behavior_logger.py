"""Unit tests for behavior-logger's pure/local logic (no Postgres, no LLM).
Run with: python3 -m unittest plugins.behavior_logger.test_behavior_logger -v
(or `python3 plugins/behavior-logger/test_behavior_logger.py` from repo root)
"""

import importlib.util
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
