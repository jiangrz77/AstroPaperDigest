"""Parse markdown digest files into structured paper entries."""

import glob
import os
import re

from .scoring import score_to_stars


def parse_digest(digest_path: str) -> dict:
    """Parse a markdown digest file into structured data.
    
    Returns:
        dict with keys: date, total_papers, highly_relevant_count, tiers
        where tiers is a list of {"name": str, "papers": [paper_dict, ...]}
    """
    with open(digest_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    result = {
        "date": "",
        "total_papers": 0,
        "highly_relevant_count": 0,
        "content": "",
        "tiers": [],
    }
    
    # Extract header info
    date_match = re.search(r"# (?:AstroPaperDigest|Arxiv Daily Digest) - (\d{4}-\d{2}-\d{2})", content)
    if date_match:
        result["date"] = date_match.group(1)
    
    total_match = re.search(r"\*\*Total papers reviewed:\*\* (\d+)", content)
    if total_match:
        result["total_papers"] = int(total_match.group(1))
    
    high_match = re.search(r"\*\*Highly relevant.*?:\*\* (\d+)", content)
    if high_match:
        result["highly_relevant_count"] = int(high_match.group(1))
    
    status_match = re.search(r"\*\*Status:\*\* (\w+)", content)
    if status_match:
        result["status"] = status_match.group(1)

    content_match = re.search(r"\*\*Content:\*\* (\w+)", content)
    if content_match:
        result["content"] = content_match.group(1)
    
    # Split into tiers by ## headers
    tier_sections = re.split(r"^## ", content, flags=re.MULTILINE)
    
    for section in tier_sections[1:]:  # Skip header before first ##
        lines = section.strip().split("\n")
        tier_name = lines[0].strip()
        papers = _parse_papers_in_section("\n".join(lines[1:]))
        if papers:
            result["tiers"].append({"name": tier_name, "papers": papers})
    
    return result


def _parse_papers_in_section(section_text: str) -> list:
    """Parse individual paper entries from a tier section."""
    papers = []
    
    # Split by ### headers (paper titles)
    paper_blocks = re.split(r"^### ", section_text, flags=re.MULTILINE)
    
    for block in paper_blocks[1:]:  # Skip text before first ###
        paper = _parse_single_paper(block.strip())
        if paper:
            papers.append(paper)
    
    return papers


def _parse_single_paper(block: str) -> dict:
    """Parse a single paper block into a dict."""
    lines = block.split("\n")
    if not lines:
        return None
    
    raw_title = lines[0].strip()
    
    # Extract paper type from title suffix
    paper_type = "new"
    title = raw_title
    if raw_title.endswith("[Cross-listed]"):
        paper_type = "cross"
        title = raw_title[:-len("[Cross-listed]")].strip()
    elif raw_title.endswith("[Replacement]"):
        paper_type = "replacement"
        title = raw_title[:-len("[Replacement]")].strip()
    
    paper = {
        "title": title,
        "score": 0,
        "score_source": 0,
        "score_scale": 5,
        "score_adjustment": 0.0,
        "scoring_failed": False,
        "reason": "",
        "authors": "",
        "categories": "",
        "link": "",
        "paper_id": "",
        "abstract": "",
        "paper_type": paper_type,
    }
    
    # Extract score and reason (No score for failed papers)
    if re.search(r"\*\*Score:\*\* No score", block):
        paper["score"] = 0
        paper["scoring_failed"] = True
        reason_match = re.search(r"\*\*Score:\*\* No score \| \*\*Reason:\*\* (.+)", block)
        if reason_match:
            paper["reason"] = reason_match.group(1).strip()
    else:
        score_match = re.search(r"\*\*Score:\*\* (\d+)/(5|10) \| \*\*Reason:\*\* (.+)", block)
        if score_match:
            source_score = int(score_match.group(1))
            score_scale = int(score_match.group(2))
            paper["score_source"] = source_score
            paper["score_scale"] = score_scale
            paper["score"] = score_to_stars(source_score, score_scale)
            paper["reason"] = score_match.group(3).strip()

    # Extract deterministic preference adjustment (added by ranker)
    adj_match = re.search(r"\*\*Adjustment:\*\* ([+-]?\d+(?:\.\d+)?)", block)
    if adj_match:
        adjustment = float(adj_match.group(1))
        paper["score_adjustment"] = adjustment / 2.0 if paper["score_scale"] == 10 else adjustment
    
    # Extract authors
    authors_match = re.search(r"\*\*Authors:\*\* (.+)", block)
    if authors_match:
        paper["authors"] = authors_match.group(1).strip()
    
    # Extract categories
    cats_match = re.search(r"\*\*Categories:\*\* (.+)", block)
    if cats_match:
        paper["categories"] = cats_match.group(1).strip()
    
    # Extract link and paper ID
    link_match = re.search(r"\*\*Link:\*\* \[([^\]]+)\]\(([^)]+)\)", block)
    if link_match:
        paper["paper_id"] = link_match.group(1)
        paper["link"] = link_match.group(2)
    
    # Extract abstract (lines starting with >)
    abstract_lines = []
    for line in lines:
        if line.startswith("> "):
            abstract_lines.append(line[2:])
    paper["abstract"] = " ".join(abstract_lines).strip()
    
    return paper


def get_latest_digest_path(digest_dir: str = "./output/digests") -> str:
    """Find the most recent digest file."""
    pattern = os.path.join(digest_dir, "digest_*.md")
    files = glob.glob(pattern)
    if not files:
        return ""
    return max(files)  # Lexicographic max works for YYYY-MM-DD format


def get_digest_path_for_date(date_str: str, digest_dir: str = "./output/digests") -> str:
    """Get digest file path for a specific date (YYYY-MM-DD). Returns '' if not found."""
    path = os.path.join(digest_dir, f"digest_{date_str}.md")
    return path if os.path.exists(path) else ""


def get_available_dates(digest_dir: str = "./output/digests") -> list:
    """Return sorted list of dates that have digest files (newest first)."""
    pattern = os.path.join(digest_dir, "digest_*.md")
    dates = []
    for f in glob.glob(pattern):
        m = re.search(r"digest_(\d{4}-\d{2}-\d{2})\.md", os.path.basename(f))
        if m:
            dates.append(m.group(1))
    return sorted(dates, reverse=True)
