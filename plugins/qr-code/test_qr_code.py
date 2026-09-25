"""Unit tests for qr-code's pure logic (no gateway, no Telegram).
Run inside the hermes-agent container (qrcode/Pillow live there, not on host):
  docker cp plugins/qr-code hermes-agent:/tmp/qr-code
  docker exec hermes-agent /opt/hermes/.venv/bin/python3 -m unittest -v discover -s /tmp/qr-code -p "test_*.py"
"""

import importlib.util
import os
import pathlib
import sys
import unittest

_MODULE_PATH = pathlib.Path(__file__).parent / "__init__.py"
_spec = importlib.util.spec_from_file_location("qr_code", _MODULE_PATH)
qc = importlib.util.module_from_spec(_spec)
sys.modules["qr_code"] = qc
_spec.loader.exec_module(qc)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class HandleSlashTests(unittest.TestCase):
    def setUp(self):
        self._generated_paths = []
        self._orig_generate = qc._generate_qr_png
        def _tracking_generate(text):
            path = self._orig_generate(text)
            self._generated_paths.append(path)
            return path
        qc._generate_qr_png = _tracking_generate

    def tearDown(self):
        qc._generate_qr_png = self._orig_generate
        for path in self._generated_paths:
            if os.path.exists(path):
                os.remove(path)

    def test_empty_args_returns_usage_and_writes_no_file(self):
        result = qc._handle_slash("   ")
        self.assertIn("Usage", result)
        self.assertFalse(self._generated_paths)

    def test_normal_text_returns_media_tag_to_a_real_png(self):
        result = qc._handle_slash("hello world")
        self.assertTrue(result.startswith("MEDIA:"))
        path = result[len("MEDIA:"):]
        self.assertTrue(path.endswith(".png"))
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(8), _PNG_MAGIC)

    def test_oversized_text_returns_friendly_error_without_crashing(self):
        result = qc._handle_slash("x" * 5000)
        self.assertNotIn("MEDIA:", result)
        self.assertIn("too long", result.lower())
        self.assertFalse(self._generated_paths)


if __name__ == "__main__":
    unittest.main()
