"""Fetch recent papers from the arxiv API."""

import time
from datetime import datetime, timedelta, timezone

import arxiv


def fetch_papers(
    categories: list[str],
    days: int = 1,
    max_results: int = 500,
    max_retries: int = 5,
    include_cross: bool = True,
    include_replacements: bool = True,
    target_date: str = None,
) -> list[dict]:
    """Fetch recent papers from arxiv for the given categories.

    Args:
        categories: list of arxiv category strings, e.g. ["astro-ph.GA", "astro-ph.SR"]
        days: number of past days to search
        max_results: maximum number of results to return
        max_retries: number of retry attempts on network failure
        include_cross: include cross-listed papers
        include_replacements: include replacement (updated) papers
        target_date: specific date (YYYY-MM-DD) to fetch papers for

    Returns:
        list of dicts with keys: id, title, authors, abstract, categories,
        published, pdf_url, entry, paper_type
    """
    # Build query: (cat:astro-ph.GA OR cat:astro-ph.SR OR ...)
    cat_query = " OR ".join(f"cat:{c}" for c in categories)

    # Use conservative settings to avoid rate limiting
    if target_date:
        client = arxiv.Client(page_size=100, delay_seconds=3.0, num_retries=3)
        effective_max = min(max_results, 300)
    else:
        client = arxiv.Client(page_size=50, delay_seconds=5.0, num_retries=3)
        effective_max = max_results

    search = arxiv.Search(
        query=cat_query,
        max_results=effective_max,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )

    # Determine date range for filtering
    if target_date:
        # Use arxiv announcement schedule to determine submission window
        # Announcements at 20:00 ET; submissions received between prev_day 14:00 ET and target_day 14:00 ET
        from datetime import date as date_cls
        try:
            from zoneinfo import ZoneInfo
            et = ZoneInfo("America/New_York")
        except ImportError:
            # Fallback: approximate ET as UTC-4 (EDT)
            et = timezone(timedelta(hours=-4))
        
        td = date_cls.fromisoformat(target_date)
        # Shift back 1 day: user selects BJT date (when papers appear at 08:00 BJT),
        # but the ET announcement date is 1 day earlier (20:00 ET previous day)
        td = td - timedelta(days=1)
        weekday = td.weekday()  # 0=Mon, 1=Tue, ..., 6=Sun
        
        # Determine how many days back the submission window starts
        if weekday == 0:  # Monday announcement: Fri 14:00 - Mon 14:00 ET (3 days)
            days_back = 3
        elif weekday >= 5:  # Sat/Sun: no announcements, use Friday's window
            days_back = 1  # Treat as Friday
        else:  # Tue-Fri: previous day 14:00 ET
            days_back = 1
        
        # cutoff_end = target_date 14:00 ET (in UTC)
        end_et = datetime(td.year, td.month, td.day, 14, 0, 0, tzinfo=et)
        cutoff_end = end_et.astimezone(timezone.utc)
        
        # cutoff_start = (target_date - days_back) 14:00 ET (in UTC)
        start_date = td - timedelta(days=days_back)
        start_et = datetime(start_date.year, start_date.month, start_date.day, 14, 0, 0, tzinfo=et)
        cutoff_start = start_et.astimezone(timezone.utc)
        
        print(f"  Submission window: {cutoff_start.isoformat()} to {cutoff_end.isoformat()}")
    else:
        cutoff_start = None
        cutoff_end = datetime.now(timezone.utc) - timedelta(days=days)

    for attempt in range(max_retries):
        papers = []  # Reset on each attempt to avoid duplicates
        try:
            # Add initial delay before first attempt to avoid rate limiting
            if attempt > 0:
                wait = min(30 * attempt, 120)  # 30s, 60s, 90s, 120s, 120s
                print(f"  Retrying (attempt {attempt + 1}) after {wait}s wait...")
                time.sleep(wait)
            
            for result in client.results(search):
                # Date filtering
                if target_date:
                    # Skip papers outside the target date range
                    if result.published < cutoff_start:
                        break  # Papers are sorted desc, so we can stop
                    if result.published >= cutoff_end:
                        continue  # Skip papers from after the target date
                else:
                    # Stop if we've gone past the cutoff date
                    if result.published < cutoff_end:
                        break

                # Determine paper type
                paper_type = _determine_paper_type(result, categories)
                
                # Filter based on user preferences
                if paper_type == "cross" and not include_cross:
                    continue
                if paper_type == "replacement" and not include_replacements:
                    continue

                papers.append({
                    "id": result.entry_id.split("/")[-1],  # e.g. "2607.12345"
                    "title": result.title.replace("\n", " ").strip(),
                    "authors": [a.name for a in result.authors],
                    "abstract": result.summary.replace("\n", " ").strip(),
                    "categories": [c for c in result.categories],
                    "published": result.published.isoformat(),
                    "pdf_url": result.pdf_url,
                    "primary_category": result.primary_category,
                    "paper_type": paper_type,
                })
            break  # Success
        except arxiv.HTTPError as e:
            if e.status_code == 429:
                print(f"  arxiv API rate limited (HTTP 429). Will retry...")
                if attempt == max_retries - 1:
                    print(f"  Failed to fetch papers after {max_retries} attempts due to rate limiting.")
                    print(f"  Try again in a few minutes, or reduce the number of categories.")
                    raise
            else:
                print(f"  arxiv API error (HTTP {e.status_code}): {e}")
                if attempt == max_retries - 1:
                    raise
        except Exception as e:
            print(f"  Error fetching papers: {e}")
            if attempt == max_retries - 1:
                raise

    return papers


def _determine_paper_type(result, queried_categories: list[str]) -> str:
    """Determine if a paper is new, cross-listed, or a replacement.
    
    Args:
        result: arxiv.Result object
        queried_categories: list of categories we queried
    
    Returns:
        "new", "cross", or "replacement"
    """
    # Check if it's a replacement (version > 1)
    entry_id = result.entry_id
    version = 1
    if "v" in entry_id:
        try:
            version = int(entry_id.split("v")[-1])
        except (ValueError, IndexError):
            version = 1
    
    if version > 1:
        return "replacement"
    
    # Check if it's cross-listed (primary category not in queried list)
    if result.primary_category not in queried_categories:
        return "cross"
    
    return "new"


def fetch_papers_by_ids(ids: list[str]) -> list[dict]:
    """Fetch specific papers by their arxiv IDs."""
    id_query = " OR ".join(f"id:{i}" for i in ids)
    client = arxiv.Client()
    search = arxiv.Search(
        query=id_query,
        max_results=len(ids),
    )

    papers = []
    for result in client.results(search):
        papers.append({
            "id": result.entry_id.split("/")[-1],
            "title": result.title.replace("\n", " ").strip(),
            "authors": [a.name for a in result.authors],
            "abstract": result.summary.replace("\n", " ").strip(),
            "categories": [c for c in result.categories],
            "published": result.published.isoformat(),
            "pdf_url": result.pdf_url,
            "primary_category": result.primary_category,
        })
    return papers
