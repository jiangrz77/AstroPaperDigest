"""Category + keyword filtering to reduce candidate pool before LLM ranking."""

import re
from collections import Counter


def category_filter(papers: list[dict], categories: list[str]) -> list[dict]:
    """Keep papers that belong to at least one of the specified categories."""
    cat_set = set(categories)
    filtered = []
    for p in papers:
        paper_cats = set(p.get("categories", []))
        # Also check primary_category
        paper_cats.add(p.get("primary_category", ""))
        if paper_cats & cat_set:
            filtered.append(p)
    return filtered


def keyword_score(paper: dict, keywords: list[str]) -> int:
    """Score a paper's title+abstract against the keyword list.
    
    Returns the number of keyword matches (case-insensitive).
    """
    text = (paper.get("title", "") + " " + paper.get("abstract", "")).lower()
    score = 0
    for kw in keywords:
        # Use word boundary matching for multi-word keywords
        pattern = r"\b" + re.escape(kw.lower()) + r"\b"
        if re.search(pattern, text):
            score += 1
    return score


def keyword_filter(
    papers: list[dict],
    keywords: list[str],
    max_candidates: int = 50,
) -> list[dict]:
    """Score papers by keyword overlap and return top-N candidates.
    
    Papers with keyword matches are ranked first, followed by remaining
    papers (if capacity allows).
    
    Args:
        papers: list of paper dicts
        keywords: list of keyword strings to match
        max_candidates: maximum number of papers to return

    Returns:
        list of paper dicts sorted by keyword score (descending), up to max_candidates
    """
    scored = []
    unscored = []
    for p in papers:
        score = keyword_score(p, keywords)
        if score > 0:
            scored.append((score, p))
        else:
            unscored.append(p)
    
    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)
    
    # Return top-N, filling remaining capacity with unscored papers
    result = [p for _, p in scored[:max_candidates]]
    remaining = max_candidates - len(result)
    if remaining > 0:
        result.extend(unscored[:remaining])
    return result


def filter_papers(
    papers: list[dict],
    categories: list[str],
    keywords: list[str],
    max_candidates: int = 50,
) -> list[dict]:
    """Two-stage filter: category filter, then keyword ranking.
    
    Args:
        papers: all fetched papers
        categories: arxiv categories to keep
        keywords: keywords for scoring
        max_candidates: max papers to pass to LLM

    Returns:
        filtered and ranked list of paper dicts
    """
    # Stage 1: category filter
    cat_filtered = category_filter(papers, categories)
    print(f"  Category filter: {len(papers)} -> {len(cat_filtered)} papers")
    
    # Stage 2: keyword scoring and filtering
    if keywords:
        kw_filtered = keyword_filter(cat_filtered, keywords, max_candidates)
        print(f"  Keyword filter: {len(cat_filtered)} -> {len(kw_filtered)} papers")
        return kw_filtered
    else:
        # If no keywords configured, just take top-N from category filter
        return cat_filtered[:max_candidates]
