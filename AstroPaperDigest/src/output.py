"""Generate BibTeX and Markdown outputs from ranked papers."""

import json
import os
import re
from datetime import date

from .scoring import normalize_threshold


def _escape_bibtex(text: str) -> str:
    """Escape special BibTeX characters outside of TeX math mode."""
    # Escape special chars but preserve existing TeX math ($...$)
    result = []
    in_math = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '$':
            in_math = not in_math
            result.append(ch)
        elif not in_math and ch in '&%#_~^':
            result.append('\\' + ch)
        elif not in_math and ch == '{':
            result.append('\\{')
        elif not in_math and ch == '}':
            result.append('\\}')
        else:
            result.append(ch)
        i += 1
    return ''.join(result)


def paper_to_bibtex(paper: dict) -> str:
    """Convert a paper dict to a BibTeX entry string."""
    entry_key = paper["id"].replace(".", "_")
    authors = " and ".join(paper["authors"])
    year = paper["published"][:4] if paper.get("published") else str(date.today().year)
    
    title = _escape_bibtex(paper.get("title", ""))
    abstract = _escape_bibtex(paper.get("abstract", ""))
    reason = _escape_bibtex(paper.get("reason", ""))
    scoring_failed = bool(paper.get("scoring_failed"))
    if scoring_failed:
        note_text = "No score (scoring failed)"
    else:
        note_text = f"Recommended: {paper.get('score', 'N/A')}/5 stars - {reason}"
    
    bibtex = f"""@misc{{{entry_key},
  title = {{{{{title}}}}},
  author = {{{authors}}},
  year = {{{year}}},
  number = {{arXiv:{paper['id']}}},
  eprint = {{{paper['id']}}},
  primaryclass = {{{paper.get('primary_category', 'astro-ph')}}},
  publisher = {{arXiv}},
  url = {{{paper.get('pdf_url', '')}}},
  abstract = {{{{{abstract}}}}},
  archiveprefix = {{arXiv}},
  note = {{{note_text}}}
}}
"""
    return bibtex


def write_bibtex(
    papers: list[dict],
    output_dir: str,
    threshold: int = 4,
    output_date: str = None,
) -> str:
    """Write BibTeX entries for papers above the score threshold.
    
    Papers are saved to a date-stamped file: bibtex/recommendations_YYYY-MM-DD.bib
    
    Args:
        papers: ranked list of paper dicts with 'score' field
        output_dir: path to the bibtex output directory
        threshold: minimum score to include
        output_date: date string (YYYY-MM-DD) for the output filename

    Returns:
        path to the written file
    """
    threshold = normalize_threshold(threshold)
    filtered = [p for p in papers if p.get("score", 0) >= threshold]
    
    if not filtered:
        print("  No papers above threshold for BibTeX output.")
        return ""
    
    os.makedirs(output_dir, exist_ok=True)
    d = output_date or date.today().isoformat()
    output_path = os.path.join(output_dir, f"recommendations_{d}.bib")
    
    # Check existing entries in the selected date's file for dedup
    existing_ids = set()
    if os.path.exists(output_path):
        with open(output_path, "r", encoding="utf-8") as f:
            existing = f.read()
        for match in re.finditer(r'@\w+\{([^,]+),', existing):
            existing_ids.add(match.group(1).strip())
    
    new_entries = []
    for p in filtered:
        entry_key = p["id"].replace(".", "_")
        if entry_key not in existing_ids:
            new_entries.append(paper_to_bibtex(p))
    
    if new_entries:
        mode = "a" if os.path.exists(output_path) else "w"
        with open(output_path, mode, encoding="utf-8") as f:
            if mode == "a":
                f.write("\n")
            f.write("\n".join(new_entries))
        print(f"  Wrote {len(new_entries)} entries to {output_path}")
    else:
        print("  All papers already exist in the selected date's BibTeX file.")
    
    return output_path


def generate_markdown_digest(
    papers: list[dict],
    threshold: int = 4,
    digest_date: str = None,
) -> str:
    """Generate a markdown digest of ranked papers.
    
    Args:
        papers: ranked list of paper dicts with 'score' and 'reason' fields
        threshold: minimum star rating for BibTeX/high-relevance reporting
        digest_date: date string (YYYY-MM-DD) for the digest header

    Returns:
        markdown string
    """
    threshold = normalize_threshold(threshold)
    d = digest_date or date.today().isoformat()
    content_flag = "partial" if any(p.get("scoring_failed") for p in papers) else "full"
    lines = [
        f"# AstroPaperDigest - {d}",
        "",
        f"**Total papers reviewed:** {len(papers)}",
        f"**Highly relevant (4–5 stars):** {len([p for p in papers if p.get('score', 0) >= 4])}",
        f"**Content:** {content_flag}",
        "",
    ]
    
    # Tier 1: the highest recommendation is intentionally distinct from 4★.
    strong = [p for p in papers if p.get("score", 0) == 5]
    if strong:
        lines.append("## Strongly Recommended")
        lines.append("")
        for p in strong:
            lines.extend(_paper_to_markdown_entry(p))
        lines.append("")

    # Tier 2: Highly Relevant (4★)
    high = [p for p in papers if p.get("score", 0) == 4]
    if high:
        lines.append("## Highly Relevant")
        lines.append("")
        for p in high:
            lines.extend(_paper_to_markdown_entry(p))
        lines.append("")
    
    # Tier 3: Possibly Relevant (2–3★)
    medium = [p for p in papers if 2 <= p.get("score", 0) <= 3]
    if medium:
        lines.append("## Possibly Relevant")
        lines.append("")
        for p in medium:
            lines.extend(_paper_to_markdown_entry(p))
        lines.append("")
    
    # Tier 4: Marginal (1★); failed scoring remains visible here as No score.
    low = [p for p in papers if p.get("score", 0) <= 1]
    if low:
        lines.append("## Marginal")
        lines.append("")
        for p in low:
            lines.extend(_paper_to_markdown_entry(p, brief=True))
        lines.append("")
    
    return "\n".join(lines)


def _paper_to_markdown_entry(paper: dict, brief: bool = False) -> list[str]:
    """Format a single paper as markdown lines."""
    score = paper.get("score", 0)
    reason = paper.get("reason", "")
    paper_type = paper.get("paper_type", "new")
    authors_str = ", ".join(paper["authors"][:5])
    if len(paper["authors"]) > 5:
        authors_str += " et al."
    
    arxiv_url = f"https://arxiv.org/abs/{paper['id']}"
    
    # Add paper type indicator
    type_indicator = ""
    if paper_type == "cross":
        type_indicator = " [Cross-listed]"
    elif paper_type == "replacement":
        type_indicator = " [Replacement]"
    
    lines = [
        f"### {paper['title']}{type_indicator}",
        "",
    ]
    if paper.get("scoring_failed"):
        lines.append(f"**Score:** No score | **Reason:** {reason}")
    else:
        lines.append(f"**Score:** {score}/5 | **Reason:** {reason}")
        adjustment = paper.get("score_adjustment", 0)
        if adjustment:
            lines.append(f"**Adjustment:** {adjustment:+.1f}")
    lines += [
        f"**Authors:** {authors_str}",
        f"**Categories:** {', '.join(paper['categories'])}",
        f"**Link:** [{paper['id']}]({arxiv_url})",
    ]
    
    if not brief:
        # Include first 300 chars of abstract
        abstract_snippet = paper.get("abstract", "")[:300] + "..."
        lines.append("")
        lines.append(f"> {abstract_snippet}")
    
    lines.append("")
    return lines


def write_digest(
    papers: list[dict],
    digest_dir: str,
    threshold: int = 4,
    digest_date: str = None,
) -> str:
    """Write markdown digest to file.
    
    Returns:
        path to the written file
    """
    os.makedirs(digest_dir, exist_ok=True)
    d = digest_date or date.today().isoformat()
    digest_path = os.path.join(digest_dir, f"digest_{d}.md")
    
    content = generate_markdown_digest(papers, threshold, digest_date=d)
    
    # Commit the digest atomically.  If generation is stopped while output is
    # being written, users keep the previous complete digest rather than a
    # truncated replacement.
    temp_digest_path = digest_path + ".tmp"
    with open(temp_digest_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(temp_digest_path, digest_path)

    # Sidecar with full abstracts so the desktop digest page can expand them
    # ("Show more" / "Show less") without bloating the emailed markdown, which
    # intentionally keeps the 300-char snippets above.
    full_abstracts = {}
    for p in papers:
        pid = p.get("id")
        abstract = (p.get("abstract") or "").strip()
        if pid and abstract:
            full_abstracts[pid] = abstract
    if full_abstracts:
        sidecar_path = os.path.join(digest_dir, f"digest_{d}.full.json")
        temp_sidecar_path = sidecar_path + ".tmp"
        with open(temp_sidecar_path, "w", encoding="utf-8") as f:
            json.dump(full_abstracts, f, ensure_ascii=False, indent=2)
        os.replace(temp_sidecar_path, sidecar_path)
        print(f"  Full abstracts written to {sidecar_path}")
    
    print(f"  Digest written to {digest_path}")
    return digest_path
