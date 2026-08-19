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
import requests
import urllib.request


_MIN_REQUEST_INTERVAL = 3.1
_PAGE_SIZE = 300
# arXiv throttles large id_list requests; keep each API call small.
_ID_LIST_BATCH_SIZE = 50
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


def _new_client(trust_env: bool = True) -> arxiv.Client:
    """Create a single-connection client with compliant page pacing.

    Args:
        trust_env: honour the system HTTP(S)_PROXY environment. Pass False to
            force a direct connection (fallback when the proxy is broken).
    """
    client = arxiv.Client(
        page_size=_PAGE_SIZE,
        delay_seconds=_MIN_REQUEST_INTERVAL,
        num_retries=0,
    )
    client._session.trust_env = trust_env
    # The arxiv library issues requests without a timeout; a stalled proxy or
    # network can hang the pipeline for minutes. Enforce our own timeout so
    # failures surface quickly instead of blocking forever.
    original_get = client._session.get

    def get_with_timeout(url, **kwargs):
        kwargs.setdefault("timeout", 30)
        return original_get(url, **kwargs)

    client._session.get = get_with_timeout
    return client


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
    target = date_cls.fromisoformat(target_date)
    # arXiv list pages write the day of month without zero padding
    # (e.g. "Thu, 2 Jan 2025"); tolerate both padded and unpadded forms.
    section_pattern = re.compile(
        rf"<h3>{_WEEKDAYS[target.weekday()]}, (\d{{1,2}}) "
        rf"{_MONTHS[target.month - 1]} {target.year} "
        rf"\(showing \d+ of \d+ entries \)</h3>"
        rf"(.*?)(?=</dl>)",
        re.DOTALL,
    )
    for section_match in section_pattern.finditer(content):
        if int(section_match.group(1)) != target.day:
            continue
        return re.findall(
            r'href\s*=\s*["\']/abs/([0-9.]+)["\']',
            section_match.group(2),
        )
    return None


def _fetch_recent_listing_ids(target_date: str) -> Optional[list[str]]:
    """Fetch one recent astro-ph listing page under the shared limiter."""
    request = Request(
        _RECENT_LIST_URL,
        headers={"User-Agent": _ARXIV_USER_AGENT},
    )
    with _api_request_session():
        try:
            with urlopen(request, timeout=30) as response:
                content = response.read().decode("utf-8")
        except Exception:
            # The system proxy may be unreachable; retry with a direct
            # connection before giving up on the official listing.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=30) as response:
                content = response.read().decode("utf-8")
    return _parse_recent_listing_ids(content, target_date)


def _paper_from_result(
    result,
    categories: list[str],
    include_cross: bool,
    include_replacements: bool,
    replacement_stream: bool = False,
) -> Optional[dict]:
    """Convert an arxiv.Result into a paper dict (or None if filtered out).

    Returns a dict with "base_id" (version-stripped) and "paper" so callers can
    deduplicate by the version-less identifier.
    """
    paper_id = result.entry_id.split("/")[-1]
    base_id = re.sub(r"v\d+$", "", paper_id)
    paper_categories = [str(category) for category in result.categories]
    if not set(paper_categories).intersection(categories):
        return None

    if replacement_stream:
        if result.updated == result.published:
            return None
        paper_type = "replacement"
    elif result.updated != result.published:
        # A newer version in the submitted stream is a replacement; honour the
        # same preferences as the updated stream.
        if not include_replacements:
            return None
        paper_type = "replacement"
    else:
        paper_type = _determine_paper_type(result, categories)
        if paper_type == "cross" and not include_cross:
            return None

    return {
        "base_id": base_id,
        "paper": {
            "id": paper_id,
            "title": result.title.replace("\n", " ").strip(),
            "authors": [a.name for a in result.authors],
            "abstract": result.summary.replace("\n", " ").strip(),
            "categories": paper_categories,
            "published": result.published.isoformat(),
            "updated": result.updated.isoformat(),
            "pdf_url": result.pdf_url,
            "primary_category": result.primary_category,
            "paper_type": paper_type,
        },
    }


def _fetch_listed_papers(
    ids: list[str],
    categories: list[str],
    include_cross: bool,
    include_replacements: bool,
    trust_env: bool = True,
) -> list[dict]:
    """Fetch metadata for the exact IDs published in an official listing.

    The id_list is fetched in small batches: arXiv rate-limits large id_list
    requests, and the shared 3.1s limiter applies between batches.
    """
    if not ids:
        return []

    client = _new_client(trust_env=trust_env)
    papers_by_id = {}

    for start in range(0, len(ids), _ID_LIST_BATCH_SIZE):
        batch = ids[start:start + _ID_LIST_BATCH_SIZE]
        search = arxiv.Search(id_list=batch, max_results=len(batch))
        for attempt in range(2):
            try:
                with _api_request_session():
                    for result in client.results(search):
                        item = _paper_from_result(
                            result,
                            categories,
                            include_cross,
                            include_replacements,
                        )
                        if item is None:
                            continue
                        if item["base_id"] not in papers_by_id:
                            papers_by_id[item["base_id"]] = item["paper"]
                break
            except requests.exceptions.ProxyError:
                if trust_env and attempt == 0:
                    # The system proxy is unreachable; retry directly.
                    print("  Proxy connection failed; retrying without the system proxy...")
                    client = _new_client(trust_env=False)
                    continue
                raise
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt == 0:
                    print(f"  arXiv API connection error; retrying once ({e})...")
                    time.sleep(5)
                    continue
                raise
            except arxiv.HTTPError as e:
                if e.status == 429 and attempt == 0:
                    # A single polite retry after a backoff; never hammer.
                    print("  arXiv API rate limited; waiting 30s before one retry...")
                    time.sleep(30)
                    continue
                raise

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
        # The recent page is labelled with the announcement date in US Eastern
        # time, which is one day earlier than the user's BJT digest date
        # (announcements appear at 20:00 ET = 08:00 BJT the next day).
        et_target_date = (
            date_cls.fromisoformat(target_date) - timedelta(days=1)
        ).isoformat()
        try:
            listed_ids = _fetch_recent_listing_ids(et_target_date)
        except Exception as e:
            # The official listing is an optimisation; a network failure here
            # must not kill the run - fall back to the API query path.
            print(f"  Recent listing unavailable ({e}); using API query fallback.")
            listed_ids = None
        if listed_ids is not None:
            print(
                f"  Official astro-ph listing: {len(listed_ids)} entries "
                f"for {_recent_date_label(et_target_date)}"
            )
            try:
                return _fetch_listed_papers(
                    listed_ids,
                    categories,
                    include_cross,
                    include_replacements,
                )
            except requests.exceptions.ProxyError:
                print("  Proxy connection failed; retrying without the system proxy...")
                return _fetch_listed_papers(
                    listed_ids,
                    categories,
                    include_cross,
                    include_replacements,
                    trust_env=False,
                )

    # Build query: (cat:astro-ph.GA OR cat:astro-ph.SR OR ...)
    cat_query = " OR ".join(f"cat:{c}" for c in categories)

    client = _new_client()
    proxy_retried = False
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

    attempt = 0
    while attempt < max_retries:
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

                        item = _paper_from_result(
                            result,
                            categories,
                            include_cross,
                            include_replacements,
                            replacement_stream=replacement_stream,
                        )
                        if item is None:
                            continue
                        if item["base_id"] in papers_by_id:
                            continue
                        papers_by_id[item["base_id"]] = item["paper"]
            break  # Success
        except requests.exceptions.ProxyError:
            if not proxy_retried:
                # The system proxy is unreachable; retry immediately with a
                # direct connection, without consuming the retry budget.
                proxy_retried = True
                print("  Proxy connection failed; retrying without the system proxy...")
                client = _new_client(trust_env=False)
                continue
            raise
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
        attempt += 1

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
