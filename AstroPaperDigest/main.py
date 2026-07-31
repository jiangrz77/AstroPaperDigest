#!/usr/bin/env python3
"""Astro Paper Digest - CLI entry point.

Fetches recent arxiv papers, filters by category/keyword, ranks with LLM,
and outputs BibTeX + Markdown digest.
"""

import argparse
import os
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load .env file if present (for API keys, email password)
# Use absolute path based on script location to work regardless of cwd
_PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(_PROJECT_DIR / ".env")
os.chdir(_PROJECT_DIR)

from src.profile import build_profile, build_profile_from_config
from src.fetch_arxiv import fetch_papers
from src.filter import filter_papers
from src.ranker import rank_papers
from src.output import write_bibtex, write_digest, generate_markdown_digest
from src.notifier import send_digest_email
from src.digest_parser import parse_digest, get_latest_digest_path


def load_config(config_path: str = "config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _write_empty_digest(digest_dir: str, reason: str, digest_date: str = None):
    """Write an empty digest file marking no papers available."""
    os.makedirs(digest_dir, exist_ok=True)
    d = digest_date or date.today().isoformat()
    path = os.path.join(digest_dir, f"digest_{d}.md")
    content = f"""# AstroPaperDigest - {d}

**Total papers reviewed:** 0
**Highly relevant (score >= 7):** 0
**Status:** {reason}
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  Empty digest written to {path}")


def main():
    parser = argparse.ArgumentParser(
        description="Astro Paper Digest"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)"
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=None,
        help="Minimum score threshold for BibTeX output (overrides config)"
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Skip email notification"
    )
    parser.add_argument(
        "--update-profile",
        action="store_true",
        help="Re-extract interest profile from bib file and print summary"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run filtering without LLM ranking (for testing)"
    )
    parser.add_argument(
        "--no-cross",
        action="store_true",
        help="Exclude cross-listed papers"
    )
    parser.add_argument(
        "--no-replacements",
        action="store_true",
        help="Exclude replacement (updated) papers"
    )
    parser.add_argument(
        "--target-date",
        default=None,
        help="Target local date for digest (YYYY-MM-DD, defaults to today)"
    )
    
    args = parser.parse_args()
    
    # Load config
    config = load_config(args.config)
    
    # Override config with CLI args
    filter_cfg = config.get("filter", {})
    output_cfg = config.get("output", {})
    threshold = args.threshold if args.threshold is not None else filter_cfg.get("score_threshold", 7)
    
    print("=== Astro Paper Digest ===\n")
    
    # Step 1: Build interest profile
    print("[1/5] Building interest profile...")
    bib_path = config.get("bib_file", "")
    if bib_path and os.path.exists(bib_path):
        profile = build_profile(bib_path)
        print(f"  Parsed {len(profile['all_entries'])} entries from {bib_path}")
        print(f"  Top categories: {', '.join(f'{c}({n})' for c, n in profile['categories'].most_common(5))}")
    else:
        if bib_path:
            print(f"  Bib file not found: {bib_path}")
        print("  Using keywords from config.yaml as interest profile")
        profile = build_profile_from_config(config)
        print(f"  Keywords: {len(profile['keywords'])} | Categories: {len(profile['categories'])}")
    
    if args.update_profile:
        print("\n=== Interest Profile Summary ===")
        from src.profile import profile_to_prompt_text
        print(profile_to_prompt_text(profile))
        return
    
    # Determine digest date in the user's local timezone
    arxiv_date = args.target_date or date.today().isoformat()
    print(f"  Digest date: {arxiv_date}")

    # Step 2: Fetch papers
    print(f"\n[2/5] Fetching papers...")
    categories = config.get("arxiv_categories", [
        "astro-ph.CO",
        "astro-ph.EP",
        "astro-ph.GA",
        "astro-ph.HE",
        "astro-ph.IM",
        "astro-ph.SR",
    ])
    include_cross = not args.no_cross
    include_replacements = not args.no_replacements
    papers = fetch_papers(
        categories,
        target_date=arxiv_date,
        include_cross=include_cross,
        include_replacements=include_replacements,
    )
    print(f"  Fetched {len(papers)} papers")
    
    # Count paper types
    if papers:
        new_count = sum(1 for p in papers if p.get("paper_type") == "new")
        cross_count = sum(1 for p in papers if p.get("paper_type") == "cross")
        repl_count = sum(1 for p in papers if p.get("paper_type") == "replacement")
        print(f"  New: {new_count}, Cross-listed: {cross_count}, Replacements: {repl_count}")
    
    if not papers:
        print("  No papers found. Writing empty digest.")
        digest_dir = output_cfg.get("digest_dir", "./output/digests")
        _write_empty_digest(digest_dir, "no_papers", arxiv_date)
        return
    
    # Check for duplicates against latest digest (skip when target-date is specified)
    if not args.target_date:
        latest = get_latest_digest_path()
        if latest:
            prev = parse_digest(latest)
            prev_ids = set()
            for tier in prev["tiers"]:
                for p in tier["papers"]:
                    if p.get("paper_id"):
                        prev_ids.add(p["paper_id"])
            new_ids = {p["id"] for p in papers}
            if new_ids.issubset(prev_ids):
                print("  No new papers since last digest. Writing empty digest.")
                digest_dir = output_cfg.get("digest_dir", "./output/digests")
                _write_empty_digest(digest_dir, "no_new_papers", arxiv_date)
                return
    
    # Step 3: Filter papers
    print("\n[3/5] Filtering papers...")
    keywords = config.get("keywords", [])
    max_candidates = filter_cfg.get("max_candidates_for_llm", 50)
    candidates = filter_papers(papers, categories, keywords, max_candidates)
    
    if not candidates:
        print("  No papers matched the filter criteria. Exiting.")
        return
    
    # Step 4: Rank papers with LLM
    if args.dry_run:
        print("\n[4/5] Dry run - skipping LLM ranking")
        # Assign default scores for dry run
        for i, p in enumerate(candidates):
            p["score"] = 5
            p["reason"] = "Dry run - no LLM scoring"
        ranked = candidates
    else:
        print(f"\n[4/5] Ranking {len(candidates)} papers with LLM...")
        llm_config = config.get("llm", {})
        ranked = rank_papers(candidates, profile, llm_config)
    
    if not ranked:
        print("  Warning: No papers were successfully scored. Exiting.")
        return
    
    print(f"  Top scored paper: {ranked[0]['title'][:80]}... (score: {ranked[0]['score']})")
    
    # Include unranked papers (those that didn't pass the filter) with score 0
    ranked_ids = {p["id"] for p in ranked}
    unranked = [p for p in papers if p["id"] not in ranked_ids]
    for p in unranked:
        p["score"] = 0
        p["reason"] = "Not ranked (below keyword threshold)"
    all_papers = ranked + unranked
    print(f"  Including {len(unranked)} unranked papers in digest")
    
    # Step 5: Generate outputs
    print("\n[5/5] Generating outputs...")
    
    # BibTeX output (date-stamped) - only ranked papers above threshold
    bibtex_dir = output_cfg.get("bibtex_dir", "./output/bibtex")
    bibtex_path = write_bibtex(
        ranked,
        bibtex_dir,
        threshold,
        output_date=arxiv_date,
    )
    
    # Markdown digest - ALL papers
    digest_dir = output_cfg.get("digest_dir", "./output/digests")
    digest_path = write_digest(all_papers, digest_dir, threshold, digest_date=arxiv_date)
    
    # Email notification
    if not args.no_email:
        email_config = config.get("email", {})
        if email_config.get("enabled", False):
            print("\nSending email notification...")
            digest_content = generate_markdown_digest(ranked, threshold)
            send_digest_email(digest_content, email_config)
    
    print("\n=== Done! ===")
    print(f"  BibTeX: {bibtex_path}")
    print(f"  Digest: {digest_path}")
    print(f"  Papers scored: {len(ranked)}")
    print(f"  Papers above threshold ({threshold}): {len([p for p in ranked if p.get('score', 0) >= threshold])}")


if __name__ == "__main__":
    main()
