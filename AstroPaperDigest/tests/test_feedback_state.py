"""Tests for cumulative per-digest feedback score adjustments."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.feedback_state import apply_to_digest, clear_date, record_adjustment


class FeedbackStateTests(unittest.TestCase):
    def test_adjustments_accumulate_and_rebuild_tiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "adjustments.json")
            self.assertEqual(record_adjustment("2026-08-21", "a", -1, path), -1)
            self.assertEqual(record_adjustment("2026-08-21", "a", -1, path), -2)
            self.assertEqual(record_adjustment("2026-08-21", "b", 1, path), 1)

            digest = {
                "tiers": [
                    {"name": "Highly Relevant", "papers": [
                        {"paper_id": "a", "score": 7},
                        {"paper_id": "b", "score": 6},
                    ]},
                ],
            }
            apply_to_digest(digest, "2026-08-21", path)
            self.assertEqual(
                [(p["paper_id"], p["score"]) for t in digest["tiers"] for p in t["papers"]],
                [("b", 4), ("a", 3)],
            )

            clear_date("2026-08-21", path)
            digest = {"tiers": [{"name": "Highly Relevant", "papers": [{"paper_id": "a", "score": 7}]}]}
            apply_to_digest(digest, "2026-08-21", path)
            papers = [p for tier in digest["tiers"] for p in tier["papers"]]
            self.assertEqual(papers[0]["score"], 4)


if __name__ == "__main__":
    unittest.main()
