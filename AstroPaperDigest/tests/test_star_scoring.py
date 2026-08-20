"""Regression tests for the native five-star scoring migration."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.digest_parser import parse_digest
from src.preference_learning import derive_learned_profile
from src.scoring import apply_source_adjustment, legacy_score_to_stars, normalize_threshold


class StarScoringTests(unittest.TestCase):
    def test_legacy_scores_preserve_confirmed_tiers(self):
        expected = {0: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3, 7: 4, 8: 4, 9: 5, 10: 5}
        self.assertEqual({score: legacy_score_to_stars(score) for score in expected}, expected)
        self.assertEqual(normalize_threshold(7), 4)
        self.assertEqual(normalize_threshold(4), 4)

    def test_parser_accepts_both_score_scales(self):
        content = """# AstroPaperDigest - 2026-08-21

**Total papers reviewed:** 2
**Highly relevant (4–5 stars):** 2
**Content:** full

## Strongly Recommended

### Legacy paper

**Score:** 9/10 | **Reason:** Legacy result
**Authors:** A
**Categories:** astro-ph.GA
**Link:** [legacy](https://arxiv.org/abs/legacy)

### Native paper

**Score:** 4/5 | **Reason:** Native result
**Authors:** B
**Categories:** astro-ph.SR
**Link:** [native](https://arxiv.org/abs/native)
"""
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as handle:
            handle.write(content)
            path = handle.name
        try:
            papers = parse_digest(path)["tiers"][0]["papers"]
        finally:
            os.unlink(path)
        self.assertEqual([(p["score"], p["score_scale"]) for p in papers], [(5, 10), (4, 5)])

    def test_one_star_feedback_uses_source_scale(self):
        self.assertEqual(apply_source_adjustment(7, 2, 10), 5)
        self.assertEqual(apply_source_adjustment(4, 1, 5), 5)

    def test_feedback_weights_are_stronger(self):
        now = datetime.now(timezone.utc).astimezone()
        base = {
            "paper_id": "p",
            "title": "Chemical evolution",
            "abstract_snippet": "chemical evolution",
            "categories": ["astro-ph.GA"],
            "timestamp": now.isoformat(timespec="seconds"),
        }
        plus = derive_learned_profile(
            [{**base, "action": "underrated"}],
            config_keywords=["chemical evolution"],
            now=now,
        )
        minus = derive_learned_profile(
            [{**base, "action": "overrated"}],
            config_keywords=["chemical evolution"],
            now=now,
        )
        self.assertEqual(plus["keyword_weights"]["chemical evolution"]["weight"], 1.4)
        self.assertEqual(minus["keyword_weights"]["chemical evolution"]["weight"], 0.7)


if __name__ == "__main__":
    unittest.main()
