"""Regression tests for malformed and truncated LLM score responses."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ranker import _parse_score_response


class RankerResponseTests(unittest.TestCase):
    def test_recovers_complete_objects_before_unterminated_string(self):
        content = (
            '[{"index": 0, "score": 8, "reason": "Strong match"}, '
            '{"index": 1, "score": 3, "reason": "Unterminated reason'
        )
        items, partial = _parse_score_response(content)
        self.assertTrue(partial)
        self.assertEqual(items, [{"index": 0, "score": 8, "reason": "Strong match"}])

    def test_parses_markdown_wrapped_json_and_clamps_scores(self):
        content = '```json\n[{"index": 2, "score": 15, "reason": "Relevant"}]\n```'
        items, partial = _parse_score_response(content)
        self.assertFalse(partial)
        self.assertEqual(items[0]["score"], 10)


if __name__ == "__main__":
    unittest.main()
