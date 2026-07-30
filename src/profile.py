"""Parse the user's BibTeX collection to build an interest profile."""

import re
from collections import Counter
from pathlib import Path

import bibtexparser


def parse_bib(bib_path: str) -> list[dict]:
    """Parse a BibTeX file and return a list of entry dicts."""
    with open(bib_path, "r", encoding="utf-8") as f:
        bib_db = bibtexparser.loads(f.read())
    return bib_db.entries


def extract_categories(entries: list[dict]) -> Counter:
    """Count arxiv primary categories across all entries."""
    cats = Counter()
    for e in entries:
        pc = e.get("primaryclass", "")
        if pc:
            cats[pc] += 1
        # Also parse the keywords field for category info
        kw = e.get("keywords", "")
        for part in kw.split(","):
            part = part.strip()
            if "Astrophysics -" in part:
                cats[part] += 1
    return cats


def extract_keywords(entries: list[dict]) -> Counter:
    """Extract and count keywords from all entries."""
    kws = Counter()
    for e in entries:
        kw = e.get("keywords", "")
        for part in kw.split(","):
            part = part.strip()
            # Skip arxiv category labels
            if part.startswith("Astrophysics -") or part.startswith("Tag:") or part.startswith("FOS:"):
                continue
            if part and len(part) > 2:
                kws[part] += 1
    return kws


def extract_recent_titles(entries: list[dict], year_threshold: int = 2025) -> list[str]:
    """Extract titles from recent papers (used as examples for the LLM)."""
    recent = []
    for e in entries:
        year_str = e.get("year", "0")
        try:
            year = int(year_str)
        except ValueError:
            year = 0
        if year >= year_threshold:
            title = e.get("title", "").replace("{", "").replace("}", "").replace("\n", " ")
            recent.append(title)
    return recent


def extract_topic_phrases(entries: list[dict], year_threshold: int = 2025) -> Counter:
    """Extract common topic phrases from recent paper titles and abstracts."""
    # Common astrophysics topic phrases to look for
    topic_patterns = [
        r"chemical enrichment", r"metal.poor", r"globular cluster",
        r"star formation", r"supernov?a", r"[Pp]opulation III",
        r"Milky Way", r"stellar abundan", r"initial mass function",
        r"Hubble tension", r"pair.instability", r"core.collapse",
        r"red giant", r"stellar evolution", r"circumgalactic",
        r"galaxy formation", r"high.redshift", r"gravitational lens",
        r"JWST", r"Type Ia", r"nucleosynthesis", r"metallicity",
        r"stellar population", r"dwarf galaxy", r"galactic halo",
        r"white dwarf", r"neutron star", r"black hole",
        r"dark matter", r"cosmic ray", r"magnetic field",
        r"AGB", r"planetary nebula", r"supernova remnant",
        # Spectroscopy & stellar atmospheres
        r"APOGEE", r"LAMOST", r"Gaia", r"spectroscop",
        r"spectral line", r"line parameter", r"atomic data",
        r"molecular data", r"stellar atmospher", r"M dwarf",
        r"abundance analysis", r"photometr", r"survey",
        # Stellar physics
        r"massive star", r"red supergiant", r"binary",
        r"r.process", r"s.process", r"carbon.enhanced",
        r"extremely metal", r"reionization",
    ]
    
    phrases = Counter()
    for e in entries:
        year_str = e.get("year", "0")
        try:
            year = int(year_str)
        except ValueError:
            year = 0
        
        weight = 2 if year >= year_threshold else 1
        text = (e.get("title", "") + " " + e.get("abstract", "")).lower()
        
        for pattern in topic_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                phrases[pattern] += weight
    
    return phrases


def build_profile(bib_path: str) -> dict:
    """Build a complete interest profile from the BibTeX collection.
    
    Returns a dict with:
      - categories: Counter of arxiv categories
      - keywords: Counter of keywords
      - topic_phrases: Counter of topic phrases from recent papers
      - recent_titles: list of recent paper titles (as examples for LLM)
      - all_entries: all parsed entries
    """
    entries = parse_bib(bib_path)
    
    return {
        "categories": extract_categories(entries),
        "keywords": extract_keywords(entries),
        "topic_phrases": extract_topic_phrases(entries),
        "recent_titles": extract_recent_titles(entries),
        "all_entries": entries,
    }


def profile_to_prompt_text(profile: dict, max_recent: int = 10) -> str:
    """Format the interest profile into a text block suitable for an LLM prompt."""
    lines = []
    
    # Top categories
    lines.append("=== Research Interest Profile ===")
    lines.append("\nTop arxiv categories of interest:")
    for cat, count in profile["categories"].most_common(10):
        lines.append(f"  - {cat} ({count} papers)")
    
    # Top keywords
    lines.append("\nTop keywords:")
    for kw, count in profile["keywords"].most_common(20):
        lines.append(f"  - {kw} ({count})")
    
    # Top topic phrases
    lines.append("\nKey research topics (from recent papers):")
    for phrase, score in profile["topic_phrases"].most_common(15):
        lines.append(f"  - {phrase}")
    
    # Recent paper examples
    lines.append(f"\nExamples of recent papers of interest (up to {max_recent}):")
    for title in profile["recent_titles"][:max_recent]:
        lines.append(f"  - {title}")
    
    return "\n".join(lines)
