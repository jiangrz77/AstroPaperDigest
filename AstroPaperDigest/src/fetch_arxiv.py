"""Fetch recent papers from the arXiv API."""

import fcntl
import os
import re
import time
from contextlib import contextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Optional
from urllib.request import Request, urlopen

import arxiv


_MIN_REQUEST_INTERVAL = 3.1
_PAGE_SIZE = 300
_PROJECT_DIR = Path(__file__).resolve().parent.parent
_RATE_LIMIT_FILE = _PROJECT_DIR / "output" / ".arxiv_api_rate_limit"
_API_THREAD_LOCK = Lock()
_RECENT_LIST_URL = "https://arxiv.org/list/astro-ph/recent?skip=0&show=2000"
_ARXIV_USER_AGENT = "AstroPaperDigest/1.0.2"
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


@contextmanager
def _api_request_session():
    """Allow one connection and preserve the three-second inter-request gap."""
    with _API_THREAD_LOCK:
        _RATE_LIMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_RATE_LIMIT_FILE, "a+", encoding="utf-8") as state:
            fcntl.flock(state.fileno(), fcntl.LOCK_EX)
            try:
                state.seek(0)
                try:
                    last_request = float(state.read().strip())
                except ValueError:
                    last_request = 0.0

                wait = _MIN_REQUEST_INTERVAL - (time.time() - last_request)
                if wait > 0:
                    print(f"  Respecting arXiv API rate limit ({wait:.1f}s)...")
                    time.sleep(wait)
                yield
            finally:
                state.seek(0)
                state.truncate()
                state.write(str(time.time()))
                state.flush()
                os.fsync(state.fileno())
                fcntl.flock(state.fileno(), fcntl.LOCK_UN)


def _new_client() -> arxiv.Client:
    """Create a single-connection client with compliant page pacing."""
    return arxiv.Client(
        page_size=_PAGE_SIZE,
        delay_seconds=_MIN_REQUEST_INTERVAL,
        num_retries=0,
    )


def _recent_date_label(target_date: str) -> str:
    target = date_cls.fromisoformat(target_date)
    return (
        f"{_WEEKDAYS[target.weekday()]}, {target.day:02d} "
        f"{_MONTHS[target.month - 1]} {target.year}"
    )


def _parse_recent_listing_ids(
    content: str,
    target_date: str,
) -> Optional[list[str]]:
    """Extract one date's authoritative IDs from the astro-ph recent page."""
    label = re.escape(_recent_date_label(target_date))
    section_match = re.search(
        rf"<h3>{label} \(showing \d+ of \d+ entries \)</h3>"
        rf"(.*?)(?=</dl>)",
        content,
        re.DOTALL,
    )
    if section_match is None:
        return None
    return re.findall(
        r'href\s*=\s*["\']/abs/([0-9.]+)["\']',
        section_match.group(1),
    )


def _fetch_recent_listing_ids(target_date: str) -> Optional[list[str]]:
    """Fetch one recent astro-ph listing page under the shared limiter."""
    request = Request(
        _RECENT_LIST_URL,
        headers={"User-Agent": _ARXIV_USER_AGENT},
    )
    with _api_request_session():
        with urlopen(request, timeout=30) as response:
            content = response.read().decode("utf-8")
    return _parse_recent_listing_ids(content, target_date)


def _fetch_listed_papers(
    ids: list[str],
    categories: list[str],
    include_cross: bool,
    include_replacements: bool,
) -> list[dict]:
    """Fetch metadata for the exact IDs published in an official listing."""
    if not ids:
        return []

    client = _new_client()
    search = arxiv.Search(id_list=ids, max_results=len(ids))
    papers_by_id = {}
    with _api_request_session():
        for result in client.results(search):
            paper_id = result.entry_id.split("/")[-1]
            base_id = re.sub(r"v\d+$", "", paper_id)
            paper_categories = [str(category) for category in result.categories]
            if not set(paper_categories).intersection(categories):
                continue

            paper_type = (
                "replacement"
                if result.updated != result.published
                else _determine_paper_type(result, categories)
            )
            if paper_type == "cross" and not include_cross:
                continue
            if paper_type == "replacement" and not include_replacements:
                continue

            papers_by_id[base_id] = {
                "id": paper_id,
                "title": result.title.replace("\n", " ").strip(),
                "authors": [author.name for author in result.authors],
                "abstract": result.summary.replace("\n", " ").strip(),
                "categories": paper_categories,
                "published": result.published.isoformat(),
                "updated": result.updated.isoformat(),
                "pdf_url": result.pdf_url,
                "primary_category": result.primary_category,
                "paper_type": paper_type,
            }

    return [
        papers_by_id[paper_id]
        for paper_id in ids
        if paper_id in papers_by_id
    ]


def fetch_papers(
    categories: list[str],
    days: int = 1,
    max_results: int = 500,
    max_retries: int = 1,
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
    if target_date:
        listed_ids = _fetch_recent_listing_ids(target_date)
        if listed_ids is not None:
            print(
                f"  Official astro-ph listing: {len(listed_ids)} entries "
                f"for {_recent_date_label(target_date)}"
            )
            return _fetch_listed_papers(
                listed_ids,
                categories,
                include_cross,
                include_replacements,
            )

    # Build query: (cat:astro-ph.GA OR cat:astro-ph.SR OR ...)
    cat_query = " OR ".join(f"cat:{c}" for c in categories)

    client = _new_client()
    effective_max = min(max_results, 300) if target_date else max_results

    submitted_search = arxiv.Search(
        query=cat_query,
        max_results=effective_max,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )
    searches = [(submitted_search, "published", False)]
    if include_replacements:
        searches.append((
            arxiv.Search(
                query=cat_query,
                max_results=effective_max,
                sort_by=arxiv.SortCriterion.LastUpdatedDate,
                sort_order=arxiv.SortOrder.Descending,
            ),
            "updated",
            True,
        ))

    # Determine date range for filtering
    if target_date:
        # Use arxiv announcement schedule to determine submission window
        # Announcements at 20:00 ET; submissions received between prev_day 14:00 ET and target_day 14:00 ET
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
        papers_by_id = {}
        try:
            if attempt > 0:
                wait = 30
                print(
                    f"  Waiting {wait}s before retry {attempt + 1}/{max_retries} "
                    "after a network error..."
                )
                time.sleep(wait)

            with _api_request_session():
                for search, date_attribute, replacement_stream in searches:
                    for result in client.results(search):
                        result_date = getattr(result, date_attribute)
                        if target_date:
                            if result_date < cutoff_start:
                                break
                            if result_date >= cutoff_end:
                                continue
                        elif result_date < cutoff_end:
                            break

                        paper_id = result.entry_id.split("/")[-1]
                        base_id = re.sub(r"v\d+$", "", paper_id)
                        if base_id in papers_by_id:
                            continue

                        if replacement_stream:
                            if result.updated == result.published:
                                continue
                            paper_type = "replacement"
                        else:
                            paper_type = _determine_paper_type(
                                result,
                                categories,
                            )

                        if paper_type == "cross" and not include_cross:
                            continue

                        papers_by_id[base_id] = {
                            "id": paper_id,
                            "title": result.title.replace("\n", " ").strip(),
                            "authors": [a.name for a in result.authors],
                            "abstract": result.summary.replace("\n", " ").strip(),
                            "categories": [c for c in result.categories],
                            "published": result.published.isoformat(),
                            "updated": result.updated.isoformat(),
                            "pdf_url": result.pdf_url,
                            "primary_category": result.primary_category,
                            "paper_type": paper_type,
                        }
            break  # Success
        except arxiv.HTTPError as e:
            if e.status == 429:
                print("  arXiv API returned HTTP 429.")
                print("  Stopping without automatic retries to avoid additional load.")
                raise
            else:
                print(f"  arXiv API error (HTTP {e.status}): {e}")
                if attempt == max_retries - 1:
                    raise
        except Exception as e:
            print(f"  Error fetching papers: {e}")
            if attempt == max_retries - 1:
                raise

    return list(papers_by_id.values())


def _determine_paper_type(
    result,
    queried_categories: list[str],
) -> str:
    """Determine whether a newly published paper is primary or cross-listed.

    Args:
        result: arxiv.Result object
        queried_categories: list of categories we queried

    Returns:
        "new" or "cross"
    """
    # Check if it's cross-listed (primary category not in queried list)
    if result.primary_category not in queried_categories:
        return "cross"

    return "new"
