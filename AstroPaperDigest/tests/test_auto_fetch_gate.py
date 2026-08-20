"""Tests for the one-attempt-per-day automatic fetch guard."""

import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import gui


class AutoFetchGateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.preferences_path = os.path.join(self.temp_dir.name, "preferences.json")
        self.preferences_patch = patch.object(gui, "PREFERENCES_FILE", self.preferences_path)
        self.preferences_patch.start()

    def tearDown(self):
        self.preferences_patch.stop()
        self.temp_dir.cleanup()

    def test_before_ten_gmt_plus_eight_is_blocked(self):
        now = datetime(2026, 8, 21, 9, 59)
        with patch.object(gui, "_auto_fetch_now", return_value=now):
            allowed, message = gui._automatic_fetch_gate("2026-08-21")
        self.assertFalse(allowed)
        self.assertIn("10:00 (GMT+8)", message)

    def test_attempt_is_persisted_and_blocks_second_automatic_fetch(self):
        now = datetime(2026, 8, 21, 10, 0)
        with patch.object(gui, "_auto_fetch_now", return_value=now):
            self.assertEqual(gui._automatic_fetch_gate("2026-08-21"), (True, ""))
            gui._record_automatic_fetch_attempt("2026-08-21")
            allowed, message = gui._automatic_fetch_gate("2026-08-21")

        self.assertFalse(allowed)
        self.assertIn("already been checked", message)
        with open(self.preferences_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertIn("2026-08-21", saved["auto_fetch_attempts"])


if __name__ == "__main__":
    unittest.main()
