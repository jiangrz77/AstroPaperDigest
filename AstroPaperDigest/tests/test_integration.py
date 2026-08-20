#!/usr/bin/env python3
"""Integration test for the full pipeline with mocked LLM response."""

import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arxiv
import requests

from src.profile import build_profile, profile_to_prompt_text
from src.fetch_arxiv import fetch_papers
from src.filter import filter_papers
from src.ranker import rank_papers
from src.output import write_bibtex, write_digest, generate_markdown_digest, _escape_bibtex


def _fetch_or_skip(categories: list, max_results: int):
    """Fetch papers, or report 'skip' when arXiv rate-limits us.

    The live-API tests depend on the network and on arXiv's per-IP limits;
    a 429 is environmental, not a code failure, so it is reported as skipped.
    Returns 'skip' on rate limiting, otherwise the fetched paper list.
    """
    try:
        return fetch_papers(categories, days=7, max_results=max_results)
    except arxiv.HTTPError as e:
        if e.status == 429:
            print("  SKIPPED (arXiv API rate limit; test needs the live API)")
            return "skip"
        raise
    except requests.exceptions.Timeout:
        print("  SKIPPED (arXiv API timed out; test needs the live API)")
        return "skip"
    except requests.exceptions.ConnectionError:
        print("  SKIPPED (arXiv API unreachable; test needs the live API)")
        return "skip"


def test_escape_bibtex():
    """Test BibTeX special character escaping."""
    print("=== Test: BibTeX Escaping ===")
    
    # Plain text unchanged
    assert _escape_bibtex("hello world") == "hello world"
    
    # Special chars escaped
    assert _escape_bibtex("100% complete") == r"100\% complete"
    assert _escape_bibtex("A & B") == r"A \& B"
    assert _escape_bibtex("50# item") == r"50\# item"
    assert _escape_bibtex("a_b") == r"a\_b"
    
    # Math mode preserved
    assert _escape_bibtex("$T_{\rm eff}$") == "$T_{\rm eff}$"
    assert _escape_bibtex("$z>6$") == "$z>6$"
    
    # Mixed: math preserved, outside escaped
    result = _escape_bibtex("Temperature $T_{\rm eff} \geq 5000$ K & 100%")
    assert r"\&" in result
    assert r"\%" in result
    assert "$T_{\rm eff} \\geq 5000$" in result
    
    print("  PASSED")


def test_profile_extraction():
    """Test interest profile extraction from bib file."""
    print("=== Test: Profile Extraction ===")
    
    bib_path = os.path.join(os.path.dirname(__file__), "ArxivDailyCollection.bib")
    profile = build_profile(bib_path)
    
    assert len(profile["all_entries"]) == 10, f"Expected 10 fixture entries, got {len(profile['all_entries'])}"
    assert len(profile["recent_titles"]) > 0, "Should have recent titles"
    assert len(profile["topic_phrases"]) > 0, "Should have topic phrases"
    
    # Check top categories
    top_cats = profile["categories"].most_common(3)
    assert top_cats[0][0] == "astro-ph", f"Top category should be astro-ph, got {top_cats[0][0]}"
    
    # Test prompt text generation
    prompt_text = profile_to_prompt_text(profile)
    assert "Research Interest Profile" in prompt_text
    assert "astro-ph" in prompt_text
    
    print(f"  PASSED ({len(profile['all_entries'])} entries, {len(profile['recent_titles'])} recent titles)")


def test_filter():
    """Test category + keyword filtering."""
    print("=== Test: Filter ===")
    
    papers = [
        {"id": "1", "title": "Chemical enrichment in dwarf galaxies", "abstract": "metal-poor stars in globular clusters", "categories": ["astro-ph.GA"], "primary_category": "astro-ph.GA"},
        {"id": "2", "title": "Deep learning for NLP", "abstract": "transformer models", "categories": ["cs.CL"], "primary_category": "cs.CL"},
        {"id": "3", "title": "Supernova remnants in the Milky Way", "abstract": "core-collapse supernova and nucleosynthesis", "categories": ["astro-ph.SR", "astro-ph.HE"], "primary_category": "astro-ph.SR"},
        {"id": "4", "title": "Exoplanet detection with transit", "abstract": "hot Jupiter atmospheres", "categories": ["astro-ph.EP"], "primary_category": "astro-ph.EP"},
    ]
    
    categories = ["astro-ph.GA", "astro-ph.SR", "astro-ph.HE"]
    keywords = ["chemical enrichment", "metal-poor", "supernova", "Milky Way", "globular clusters"]
    
    result = filter_papers(papers, categories, keywords, max_candidates=10)
    
    # Should keep only astro-ph papers with keyword matches
    assert len(result) >= 1, f"Should have at least 1 match, got {len(result)}"
    assert all(p["id"] != "2" for p in result), "Should filter out cs.CL paper"
    
    # Paper 1 should rank highest (most keyword matches)
    assert result[0]["id"] == "1", f"Paper 1 should rank first, got {result[0]['id']}"
    
    print(f"  PASSED ({len(papers)} -> {len(result)} papers)")


def test_ranker_with_mock():
    """Test LLM ranker with mocked API response."""
    print("=== Test: Ranker (Mocked LLM) ===")
    
    papers = [
        {"id": "2607.001", "title": "Chemical enrichment in metal-poor galaxies", "authors": ["Smith J."], "categories": ["astro-ph.GA"], "abstract": "We study chemical enrichment in metal-poor dwarf galaxies at high redshift.", "primary_category": "astro-ph.GA"},
        {"id": "2607.002", "title": "Quantum computing algorithms", "authors": ["Jones A."], "categories": ["quant-ph"], "abstract": "Novel quantum error correction codes.", "primary_category": "quant-ph"},
        {"id": "2607.003", "title": "Supernova progenitors in the Milky Way", "authors": ["Lee B.", "Park C."], "categories": ["astro-ph.SR"], "abstract": "Red supergiant stars as supernova progenitors in the Milky Way.", "primary_category": "astro-ph.SR"},
    ]
    
    profile = build_profile(os.path.join(os.path.dirname(__file__), "ArxivDailyCollection.bib"))
    
    # Mock LLM response
    mock_scores = [
        {"index": 0, "score": 9, "reason": "Directly studies chemical enrichment in metal-poor galaxies"},
        {"index": 1, "score": 2, "reason": "Unrelated quantum computing topic"},
        {"index": 2, "score": 8, "reason": "Studies supernova progenitors in the Milky Way"},
    ]
    mock_response_text = json.dumps(mock_scores)
    
    # Create mock objects
    mock_choice = MagicMock()
    mock_choice.message.content = mock_response_text
    
    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_completion
    
    llm_config = {
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-v4-flash",
    }
    
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}):
        with patch("src.ranker.get_client", return_value=mock_client):
            ranked = rank_papers(papers, profile, llm_config)
    
    assert len(ranked) == 3, f"Expected 3 ranked papers, got {len(ranked)}"
    
    # Should be sorted by score descending
    assert ranked[0]["score"] >= ranked[1]["score"], "Should be sorted by score"
    assert ranked[0]["id"] == "2607.001", f"Highest score should be paper 1, got {ranked[0]['id']}"
    assert ranked[0]["score"] == 9
    assert "chemical enrichment" in ranked[0]["reason"].lower()
    
    # Check that reason field is attached
    for p in ranked:
        assert "reason" in p, "Each paper should have a reason"
        assert "score" in p, "Each paper should have a score"
    
    print(f"  PASSED (ranked {len(ranked)} papers, top score: {ranked[0]['score']})")


def test_ranker_none_response():
    """Test ranker handles None LLM response gracefully."""
    print("=== Test: Ranker None Response ===")
    
    papers = [
        {"id": "2607.001", "title": "Test paper", "authors": ["A"], "categories": ["astro-ph.GA"], "abstract": "Test abstract.", "primary_category": "astro-ph.GA"},
    ]
    
    profile = build_profile(os.path.join(os.path.dirname(__file__), "ArxivDailyCollection.bib"))
    
    # Mock None response
    mock_choice = MagicMock()
    mock_choice.message.content = None
    
    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_completion
    
    llm_config = {
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-v4-flash",
    }
    
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}):
        with patch("src.ranker.get_client", return_value=mock_client):
            ranked = rank_papers(papers, profile, llm_config)
    
    assert len(ranked) == 1
    assert ranked[0]["scoring_failed"] is True, "Paper should be marked as scoring-failed"
    assert ranked[0]["score"] == 0, f"Failed paper should score 0, got {ranked[0]['score']}"
    assert "no score" in ranked[0]["reason"].lower()
    
    print("  PASSED (handled None response gracefully)")


def test_ranker_markdown_wrapped_json():
    """Test ranker handles markdown-wrapped JSON response."""
    print("=== Test: Ranker Markdown-Wrapped JSON ===")
    
    papers = [
        {"id": "2607.001", "title": "Test", "authors": ["A"], "categories": ["astro-ph.GA"], "abstract": "Test.", "primary_category": "astro-ph.GA"},
    ]
    
    profile = build_profile(os.path.join(os.path.dirname(__file__), "ArxivDailyCollection.bib"))
    
    # Mock markdown-wrapped JSON response
    wrapped_response = '```json\n[{"index": 0, "score": 7, "reason": "Good match"}]\n```'
    
    mock_choice = MagicMock()
    mock_choice.message.content = wrapped_response
    
    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_completion
    
    llm_config = {"base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY", "model": "deepseek-v4-flash"}
    
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}):
        with patch("src.ranker.get_client", return_value=mock_client):
            ranked = rank_papers(papers, profile, llm_config)
    
    assert len(ranked) == 1
    assert ranked[0]["score"] == 7
    
    print("  PASSED (parsed markdown-wrapped JSON)")


def test_ranker_string_score():
    """Test ranker handles string scores from LLM (e.g., '8' instead of 8)."""
    print("=== Test: Ranker String Score ===")
    
    papers = [
        {"id": "2607.001", "title": "Test", "authors": ["A"], "categories": ["astro-ph.GA"], "abstract": "Test.", "primary_category": "astro-ph.GA"},
    ]
    
    profile = build_profile(os.path.join(os.path.dirname(__file__), "ArxivDailyCollection.bib"))
    
    # Mock response with string score
    string_score_response = '[{"index": 0, "score": "8", "reason": "Good"}]'
    
    mock_choice = MagicMock()
    mock_choice.message.content = string_score_response
    
    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_completion
    
    llm_config = {"base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY", "model": "deepseek-v4-flash"}
    
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}):
        with patch("src.ranker.get_client", return_value=mock_client):
            ranked = rank_papers(papers, profile, llm_config)
    
    assert len(ranked) == 1
    assert ranked[0]["score"] == 8, f"String '8' should be coerced to int 8, got {ranked[0]['score']}"
    assert isinstance(ranked[0]["score"], int), "Score should be int"
    
    print("  PASSED (string score coerced to int)")


def test_output_generation():
    """Test BibTeX and Markdown output generation."""
    print("=== Test: Output Generation ===")
    
    papers = [
        {"id": "2607.111", "title": "Chemical Enrichment in $z>6$ Galaxies", "authors": ["Smith J.", "Doe J."], "published": "2026-07-15T00:00:00", "primary_category": "astro-ph.GA", "categories": ["astro-ph.GA"], "pdf_url": "https://arxiv.org/pdf/2607.111", "abstract": "We study 100% of galaxies with M < 10^10 M_\\odot & find enrichment.", "score": 9, "reason": "Chemical enrichment & metallicity match"},
        {"id": "2607.222", "title": "Stellar populations in dwarf galaxies", "authors": ["Lee B."], "published": "2026-07-16T00:00:00", "primary_category": "astro-ph.SR", "categories": ["astro-ph.SR"], "pdf_url": "https://arxiv.org/pdf/2607.222", "abstract": "A study of stellar populations.", "score": 6, "reason": "Related to stellar populations"},
        {"id": "2607.333", "title": "Quantum gravity review", "authors": ["Chen X."], "published": "2026-07-17T00:00:00", "primary_category": "gr-qc", "categories": ["gr-qc"], "pdf_url": "https://arxiv.org/pdf/2607.333", "abstract": "A review.", "score": 2, "reason": "Not relevant"},
    ]
    
    # Test BibTeX output
    with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as bib_dir:
        bib_path = write_bibtex(
            papers,
            bib_dir,
            threshold=7,
            output_date="2026-07-15",
        )
        
        assert bib_path, "Should return a path"
        assert bib_path.endswith("recommendations_2026-07-15.bib")
        with open(bib_path) as f:
            content = f.read()
        
        assert "@misc{2607_111" in content, "Should contain high-score paper"
        assert "@misc{2607_222" not in content, "Should NOT contain medium-score paper (below threshold)"
        assert "@misc{2607_333" not in content, "Should NOT contain low-score paper"
        assert r"\&" in content, "Should escape & in BibTeX"
        assert r"\%" in content, "Should escape % in BibTeX"
        
        # Test dedup
        write_bibtex(
            papers,
            bib_dir,
            threshold=7,
            output_date="2026-07-15",
        )
        with open(bib_path) as f:
            content2 = f.read()
        assert content2.count("@misc{2607_111") == 1, "Should not duplicate entry"
        
        print("  BibTeX: PASSED")
    
    # Test Markdown digest
    digest = generate_markdown_digest(papers, threshold=7)
    
    assert "## Highly Relevant" in digest, "Should have Highly Relevant section"
    assert "## Possibly Relevant" in digest, "Should have Possibly Relevant section"
    assert "## Marginal" in digest, "Should have Marginal section"
    assert "Chemical Enrichment" in digest, "Should contain paper title"
    assert "9/10" in digest, "Should show score"
    
    # Test file writing
    with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as tmpdir:
        digest_path = write_digest(papers, tmpdir, threshold=7)
        assert os.path.exists(digest_path), "Digest file should exist"
        with open(digest_path) as f:
            content = f.read()
        assert len(content) > 100, "Digest should have substantial content"
    
    print("  Markdown: PASSED")


def test_full_pipeline_dry_run():
    """Test the full pipeline end-to-end (without LLM)."""
    print("=== Test: Full Pipeline (Dry Run) ===")
    
    bib_path = os.path.join(os.path.dirname(__file__), "ArxivDailyCollection.bib")
    
    # Step 1: Profile
    profile = build_profile(bib_path)
    assert len(profile["all_entries"]) > 0
    
    # Step 2: Fetch
    papers = _fetch_or_skip(["astro-ph.GA", "astro-ph.SR"], 30)
    if papers == "skip":
        return "skip"
    assert len(papers) > 0, "Should fetch papers"
    
    # Step 3: Filter
    keywords = ["chemical enrichment", "metal-poor", "supernova", "Milky Way", "globular clusters", "star formation"]
    candidates = filter_papers(papers, ["astro-ph.GA", "astro-ph.SR"], keywords, max_candidates=20)
    assert len(candidates) > 0, "Should have candidates after filtering"
    assert len(candidates) <= 20, "Should respect max_candidates"
    
    # Step 4: Mock ranking
    for p in candidates:
        p["score"] = 7
        p["reason"] = "Mock score"
    
    # Step 5: Output
    with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as bib_dir:
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as digest_dir:
            bib_path = write_bibtex(candidates, bib_dir, threshold=7)
            digest_path = write_digest(candidates, digest_dir, threshold=7)
            
            assert bib_path, "Should return a BibTeX path"
            assert os.path.exists(bib_path), "BibTeX file should exist"
            assert os.path.exists(digest_path), "Digest file should exist"
            
            with open(bib_path) as f:
                bib_content = f.read()
            assert bib_content.count("@misc") == len(candidates), f"Should have {len(candidates)} entries"
    
    print(f"  PASSED ({len(papers)} fetched -> {len(candidates)} candidates -> outputs generated)")


def test_announcement_window_et():
    """Test the arXiv submission-window mapping (current Sun-Thu schedule)."""
    from datetime import date

    from src.fetch_arxiv import _announcement_window_et

    # 2026-08-16 is a Sunday (ET announcement): window Thu 08-13 .. Fri 08-14
    assert _announcement_window_et(date(2026, 8, 16)) == (date(2026, 8, 13), date(2026, 8, 14))
    # Monday announcement: Fri .. Mon
    assert _announcement_window_et(date(2026, 8, 17)) == (date(2026, 8, 14), date(2026, 8, 17))
    # Tue/Wed/Thu: previous day .. same day
    assert _announcement_window_et(date(2026, 8, 18)) == (date(2026, 8, 17), date(2026, 8, 18))
    assert _announcement_window_et(date(2026, 8, 19)) == (date(2026, 8, 18), date(2026, 8, 19))
    assert _announcement_window_et(date(2026, 8, 20)) == (date(2026, 8, 19), date(2026, 8, 20))
    # Friday/Saturday: no announcement
    assert _announcement_window_et(date(2026, 8, 21)) is None
    assert _announcement_window_et(date(2026, 8, 22)) is None
    print("  PASSED (announcement windows for all weekdays)")


def test_resolve_daily_batch():
    """Test daily-batch resolution statuses without touching the network."""
    from datetime import date, datetime, time as time_cls
    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    from src.fetch_arxiv import resolve_daily_batch

    sections = {
        date(2026, 8, 17): ["2608.14547"],
        date(2026, 8, 18): ["2608.16841"],
        date(2026, 8, 19): ["2608.18051"],
    }
    tz = ZoneInfo("Asia/Shanghai")
    noon = datetime(2026, 8, 19, 11, 0, tzinfo=tz)
    early = datetime(2026, 8, 19, 9, 0, tzinfo=tz)
    avail = time_cls(10, 0)

    with patch("src.fetch_arxiv._fetch_recent_listing", return_value=sections):
        # Exact section match -> ok
        r = resolve_daily_batch("2026-08-19", noon, avail)
        assert r["status"] == "ok" and r["ids"] == ["2608.18051"], r
        # BJT Saturday/Sunday -> no announcement
        r = resolve_daily_batch("2026-08-22", noon, avail)
        assert r["status"] == "no_announcement", r
        r = resolve_daily_batch("2026-08-23", noon, avail)
        assert r["status"] == "no_announcement", r
        # Older than the recent-page window -> API fallback
        r = resolve_daily_batch("2026-08-10", noon, avail)
        assert r["status"] == "listing_unavailable", r
    # Today's section missing before the availability time -> not yet available
    with patch(
        "src.fetch_arxiv._fetch_recent_listing",
        return_value={d: v for d, v in sections.items() if d != date(2026, 8, 19)},
    ):
        r = resolve_daily_batch("2026-08-19", early, avail)
        assert r["status"] == "not_yet_available", r
        # ... but after the availability time with a missing section: deferred/lagging
        r = resolve_daily_batch("2026-08-19", noon, avail)
        assert r["status"] == "deferred_or_lagging", r
    # Inside the window but missing -> deferred/lagging
    with patch(
        "src.fetch_arxiv._fetch_recent_listing",
        return_value={d: v for d, v in sections.items() if d != date(2026, 8, 18)},
    ):
        r = resolve_daily_batch("2026-08-18", noon, avail)
        assert r["status"] == "deferred_or_lagging", r
    # Listing page unreachable -> API fallback with a window
    with patch("src.fetch_arxiv._fetch_recent_listing", return_value=None):
        r = resolve_daily_batch("2026-08-19", noon, avail)
        assert r["status"] == "listing_unavailable", r
        assert r["window_utc"] is not None, r
    print("  PASSED (daily-batch resolution statuses)")


if __name__ == "__main__":
    print("=" * 60)
    print("Arxiv Daily Recommender - Integration Tests")
    print("=" * 60)
    print()
    
    tests = [
        test_escape_bibtex,
        test_profile_extraction,
        test_filter,
        test_announcement_window_et,
        test_resolve_daily_batch,
        test_ranker_with_mock,
        test_ranker_none_response,
        test_ranker_markdown_wrapped_json,
        test_ranker_string_score,
        test_output_generation,
        test_full_pipeline_dry_run,
    ]
    
    passed = 0
    skipped = 0
    failed = 0
    
    for test in tests:
        try:
            result = test()
            if result == "skip":
                skipped += 1
            else:
                passed += 1
        except Exception as e:
            print(f"  FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
        print()
    
    print("=" * 60)
    print(f"Results: {passed} passed, {skipped} skipped, {failed} failed out of {len(tests)} tests")
    print("=" * 60)
    
    sys.exit(0 if failed == 0 else 1)
