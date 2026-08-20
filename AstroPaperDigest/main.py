#!/usr/bin/env python3
"""Astro Paper Digest - CLI entry point.

Fetches recent arxiv papers, filters by category/keyword, ranks with LLM,
and outputs BibTeX + Markdown digest.
"""

import argparse
import os
import sys
from datetime import date, datetime, time as time_cls
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

# Load .env file if present (for API keys, email password)
# Use absolute path based on script location to work regardless of cwd
_PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(_PROJECT_DIR / ".env")
os.chdir(_PROJECT_DIR)

from src.profile import build_profile, build_profile_from_config
from src.fetch_arxiv import fetch_daily_batch
from src.filter import filter_papers
from src.ranker import rank_papers
from src.output import write_bibtex, write_digest, generate_markdown_digest
from src.notifier import send_digest_email
from src.digest_parser import parse_digest, get_latest_digest_path
from src.progress import emit


def load_config(config_path: str = "config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_timezone_config(config: dict):
    """Return (digest_tz, available_after) from config with safe defaults.

    digest_tz: the timezone the digest date is expressed in (default
        Asia/Shanghai).  available_after: the digest-timezone clock time after
        which the day's arXiv batch is expected to be visible (default 10:00,
        based on observed mailing completion).
    """
    tz_cfg = config.get("timezone", {}) or {}
    tz_name = tz_cfg.get("digest", "Asia/Shanghai")
    try:
        digest_tz = ZoneInfo(tz_name)
    except Exception:
        print(f"  Warning: unknown timezone '{tz_name}', using Asia/Shanghai")
        digest_tz = ZoneInfo("Asia/Shanghai")
    available_after = time_cls(10, 0)
    raw = str(tz_cfg.get("available_after", "10:00"))
    try:
        hh, mm = raw.split(":")
        available_after = time_cls(int(hh), int(mm))
    except Exception:
        print(f"  Warning: invalid available_after '{raw}', using 10:00")
    return digest_tz, available_after


def holiday_note(config: dict, et_announcement) -> str:
    """Return a note when an arXiv 2026 US holiday falls near the announcement.

    The holiday list is informational only - fetching always follows the
    official listing, which reflects actual (possibly deferred) mailings.
    """
    if et_announcement is None:
        return ""
    holidays = set(config.get("arxiv_schedule", {}).get("holidays_2026", []) or [])
    hits = sorted(
        h for h in holidays
        if abs((date.fromisoformat(h) - et_announcement).days) <= 3
    )
    if hits:
        return f"This week includes an arXiv holiday ({', '.join(hits)}); the batch may be deferred."
    return ""


def _write_empty_digest(digest_dir: str, reason: str, digest_date: str = None, note: str = ""):
    """Write an empty digest file marking no papers available."""
    os.makedirs(digest_dir, exist_ok=True)
    d = digest_date or date.today().isoformat()
    path = os.path.join(digest_dir, f"digest_{d}.md")
    content = f"""# AstroPaperDigest - {d}

**Total papers reviewed:** 0
**Highly relevant (score >= 7):** 0
**Status:** {reason}
**Content:** empty
"""
    if note:
        content += f"\n> Note: {note}\n"
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
    emit("profile", 1, 1, "Building interest profile…")
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
    
    # Digest date is a "visible date" in the configured digest timezone
    # (default Asia/Shanghai): the day the announcement batch becomes visible
    # at 08:00 local.  See src/fetch_arxiv.py for the schedule mapping.
    digest_tz, available_after = load_timezone_config(config)
    now_local = datetime.now(digest_tz)
    arxiv_date = args.target_date or now_local.date().isoformat()
    print(f"  Digest date: {arxiv_date} ({digest_tz})")

    # Step 2: Fetch papers
    print("\n[2/5] Fetching papers...")
    emit("fetch", 0, 0, "Contacting arXiv…")
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
    try:
        result = fetch_daily_batch(
            categories,
            include_cross=include_cross,
            include_replacements=include_replacements,
            target_date=arxiv_date,
            now_local=now_local,
            available_after=available_after,
        )
    except Exception as e:
        print(f"ERROR: Failed to fetch papers from arXiv: {e}")
        print("Check your network/proxy settings and try again.")
        sys.exit(2)

    status = result["status"]
    if status == "not_yet_available":
        # Before ~10:00 local the day's batch has not been mailed yet; do not
        # write a (wrong or empty) digest for today.
        print("NOT_YET_AVAILABLE")
        print(f"  {result['message']}")
        print(f"  Today's arXiv batch is not published yet (expected after {available_after.isoformat()}); no digest generated.")
        print("  Please re-run later, or pass --target-date with another date.")
        return
    if status in ("no_announcement", "deferred_or_lagging"):
        # No announcement that day (BJT Saturday/Sunday, or a US-holiday /
        # ad hoc deferral): record an explicit empty digest.
        print("NO_ANNOUNCEMENT" if status == "no_announcement" else "DEFERRED_OR_LAGGING")
        print(f"  {result['message']}")
        note = holiday_note(config, result.get("et_announcement"))
        if note:
            print(f"  {note}")
        digest_dir = output_cfg.get("digest_dir", "./output/digests")
        _write_empty_digest(digest_dir, status, arxiv_date, note=note)
        return

    papers = result["papers"]
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
    
    # Check for duplicates against latest digest (skip when target-date is
    # specified, or when the latest digest is for the same date we are
    # regenerating - otherwise re-running today would wipe the existing digest).
    if not args.target_date:
        latest = get_latest_digest_path()
        if latest and os.path.basename(latest) != f"digest_{arxiv_date}.md":
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
        print("  No papers matched the filter criteria. Writing empty digest.")
        digest_dir = output_cfg.get("digest_dir", "./output/digests")
        _write_empty_digest(digest_dir, "no_matches", arxiv_date)
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
        emit("rank", 0, 0, "Starting…")
        llm_config = config.get("llm", {})
        ranked = rank_papers(candidates, profile, llm_config)
    
    if not ranked:
        print("  Warning: No papers were successfully scored. Writing empty digest.")
        digest_dir = output_cfg.get("digest_dir", "./output/digests")
        _write_empty_digest(digest_dir, "no_scores", arxiv_date)
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
    emit("output", 0, 0, "Generating BibTeX and digest…")
    
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
    emit("done", 1, 1, "All done")
    print(f"  BibTeX: {bibtex_path}")
    print(f"  Digest: {digest_path}")
    print(f"  Papers scored: {len(ranked)}")
    print(f"  Papers above threshold ({threshold}): {len([p for p in ranked if p.get('score', 0) >= threshold])}")


if __name__ == "__main__":
    main()
