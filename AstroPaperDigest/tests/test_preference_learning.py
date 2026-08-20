#!/usr/bin/env python3
"""Unit tests for src/preference_learning.py."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import preference_learning as pl


def _ts(days_ago=0):
    return (datetime.now(timezone.utc).astimezone() - timedelta(days=days_ago)).isoformat(timespec="seconds")


def test_matches_term():
    assert pl.matches_term("Supernova nucleosynthesis", "supernova") is True
    assert pl.matches_term("supernovae", "supernova") is False
    assert pl.matches_term("Chemical evolution in dwarf galaxies", "chemical evolution") is True
    assert pl.matches_term("nothing here", "neutron star") is False
    print("  PASSED (matches_term)")


def test_extract_keyword_matches():
    text = "We study chemical evolution and supernova nucleosynthesis in metal-poor stars."
    out = pl.extract_keyword_matches(text, ["chemical evolution", "supernova", "dark matter", "metal-poor stars"])
    assert set(out) == {"chemical evolution", "supernova", "metal-poor stars"}
    print("  PASSED (extract_keyword_matches)")


def test_derive_underrated_boosts():
    now = datetime.now(timezone.utc).astimezone()
    fb = [
        {"paper_id": "a", "title": "Chemical evolution in dwarf galaxies",
         "abstract_snippet": "chemical evolution models", "action": "underrated",
         "categories": ["astro-ph.GA"], "timestamp": _ts(0)},
    ]
    profile = pl.derive_learned_profile(fb, config_keywords=["chemical evolution"], now=now)
    assert profile["keyword_weights"]["chemical evolution"]["weight"] > 1.0
    assert profile["category_weights"]["astro-ph.GA"]["weight"] > 1.0
    assert profile["global_calibration"] > 0
    print("  PASSED (underrated boosts keyword + category + calibration)")


def test_derive_overrated_penalizes():
    now = datetime.now(timezone.utc).astimezone()
    fb = [
        {"paper_id": "b", "title": "Dark matter review", "abstract_snippet": "dark matter",
         "action": "overrated", "categories": ["astro-ph.CO"], "timestamp": _ts(0)},
    ]
    profile = pl.derive_learned_profile(fb, config_keywords=["dark matter"], now=now)
    assert profile["keyword_weights"]["dark matter"]["weight"] < 1.0
    assert profile["global_calibration"] < 0
    print("  PASSED (overrated penalizes + negative calibration)")


def test_conflict_latest_wins():
    now = datetime.now(timezone.utc).astimezone()
    fb = [
        {"paper_id": "a", "title": "Chemical evolution", "abstract_snippet": "chemical evolution",
         "action": "underrated", "categories": [], "timestamp": _ts(2)},
        {"paper_id": "c", "title": "Chemical evolution", "abstract_snippet": "chemical evolution",
         "action": "overrated", "categories": [], "timestamp": _ts(0)},
    ]
    profile = pl.derive_learned_profile(fb, config_keywords=["chemical evolution"], now=now)
    w = profile["keyword_weights"]["chemical evolution"]["weight"]
    assert abs(w - pl.PENALTY) < 1e-6, w
    print("  PASSED (conflict: latest feedback wins)")


def test_same_direction_chains_and_clamps():
    now = datetime.now(timezone.utc).astimezone()
    fb = []
    for i in range(10):
        fb.append({"paper_id": f"p{i}", "title": "chemical evolution",
                   "abstract_snippet": "chemical evolution", "action": "underrated",
                   "categories": [], "timestamp": _ts(0)})
    profile = pl.derive_learned_profile(fb, config_keywords=["chemical evolution"], now=now)
    w = profile["keyword_weights"]["chemical evolution"]["weight"]
    assert w <= pl.WEIGHT_MAX + 1e-9, w
    assert w > 1.0
    print("  PASSED (same-direction chaining clamps at max)")


def test_decay_moves_toward_neutral():
    now = datetime.now(timezone.utc).astimezone()
    old_fb = [{"paper_id": "a", "title": "chemical evolution", "abstract_snippet": "chemical evolution",
               "action": "underrated", "categories": [], "timestamp": _ts(200)}]
    fresh_fb = [{"paper_id": "b", "title": "chemical evolution", "abstract_snippet": "chemical evolution",
                 "action": "underrated", "categories": [], "timestamp": _ts(0)}]
    p_old = pl.derive_learned_profile(old_fb, config_keywords=["chemical evolution"], now=now)
    p_new = pl.derive_learned_profile(fresh_fb, config_keywords=["chemical evolution"], now=now)
    assert p_old["keyword_weights"]["chemical evolution"]["weight"] < p_new["keyword_weights"]["chemical evolution"]["weight"]
    print("  PASSED (time decay toward neutral)")


def test_manual_override_and_suppress():
    now = datetime.now(timezone.utc).astimezone()
    fb = [{"paper_id": "a", "title": "chemical evolution", "abstract_snippet": "chemical evolution",
           "action": "underrated", "categories": [], "timestamp": _ts(0)}]
    manual = {"keyword_weights": {"chemical evolution": 1.9, "dark matter": None},
              "category_weights": {}}
    profile = pl.derive_learned_profile(fb, config_keywords=["chemical evolution", "dark matter"],
                                        manual=manual, now=now)
    assert profile["keyword_weights"]["chemical evolution"]["weight"] == 1.9
    assert profile["keyword_weights"]["chemical evolution"]["origin"] == "manual"
    assert "dark matter" not in profile["keyword_weights"]
    print("  PASSED (manual override + suppress)")


def test_compute_adjustment_and_clamp():
    profile = {
        "keyword_weights": {"chemical evolution": {"weight": 2.0}},
        "category_weights": {"astro-ph.GA": {"weight": 1.5}},
        "global_calibration": 0.3,
    }
    paper = {"title": "Chemical evolution in galaxies", "abstract": "chemical evolution",
             "categories": ["astro-ph.GA"]}
    adj = pl.compute_adjustment(paper, profile)
    assert abs(adj - 1.3) < 1e-9, adj
    assert pl.apply_adjustment(7, adj) == 8
    assert pl.apply_adjustment(0, -10) == 0
    assert pl.apply_adjustment(10, 10) == 10

    many = {"keyword_weights": {f"t{i}": {"weight": 2.0} for i in range(20)},
            "category_weights": {}, "global_calibration": 0.5}
    paper2 = {"title": " ".join(f"t{i}" for i in range(20)), "abstract": "", "categories": []}
    adj2 = pl.compute_adjustment(paper2, many)
    assert abs(abs(adj2) - pl.ADJUSTMENT_MAX) < 1e-9, adj2
    print("  PASSED (compute_adjustment + clamping)")


def test_format_learned_weights_block():
    profile = {
        "keyword_weights": {"chemical evolution": {"weight": 1.5}, "dark matter": {"weight": 0.7}},
        "category_weights": {"astro-ph.GA": {"weight": 1.2}},
        "global_calibration": 0.3,
    }
    text = pl.format_learned_weights_block(profile)
    assert "Learned Preference Weights" in text
    assert "chemical evolution" in text
    assert "dark matter" in text
    assert "astro-ph.GA" in text
    assert "+0.3" in text
    print("  PASSED (format_learned_weights_block)")


def test_discover_terms_requires_two_papers():
    fb = [
        {"paper_id": "a", "title": "Neutron star mergers", "abstract_snippet": "neutron star mergers",
         "action": "underrated", "categories": []},
        {"paper_id": "b", "title": "Neutron star mergers", "abstract_snippet": "neutron star mergers",
         "action": "underrated", "categories": []},
        {"paper_id": "c", "title": "Quasar outflows", "abstract_snippet": "quasar outflows",
         "action": "underrated", "categories": []},
    ]
    terms = pl._discover_terms(fb)
    assert "neutron star" in terms
    assert "quasar outflows" not in terms
    print("  PASSED (discover terms requires two papers)")


if __name__ == "__main__":
    tests = [test_matches_term, test_extract_keyword_matches, test_derive_underrated_boosts,
             test_derive_overrated_penalizes, test_conflict_latest_wins,
             test_same_direction_chains_and_clamps, test_decay_moves_toward_neutral,
             test_manual_override_and_suppress, test_compute_adjustment_and_clamp,
             test_format_learned_weights_block, test_discover_terms_requires_two_papers]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(0 if failed == 0 else 1)