"""Tests for clear handling of missing and rejected LLM API keys."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ranker import APIKeyError, is_api_key_error, rank_papers


class _UnauthorizedError(Exception):
    status_code = 401


class APIKeyErrorTests(unittest.TestCase):
    def test_missing_key_is_explicit_and_actionable(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(APIKeyError) as caught:
                rank_papers([{"id": "p1"}], {"keywords": {}}, {"api_key_env": "TEST_APD_KEY"})
        message = str(caught.exception)
        self.assertIn("LLM API key is unavailable", message)
        self.assertIn("Update & About", message)
        self.assertIn("LLM & API", message)

    def test_authentication_status_is_classified(self):
        self.assertTrue(is_api_key_error(_UnauthorizedError("invalid credentials")))

    def test_non_authentication_error_is_not_classified_as_key_failure(self):
        error = RuntimeError("temporary network connection failed")
        self.assertFalse(is_api_key_error(error))


if __name__ == "__main__":
    unittest.main()
