"""Fetch recent papers from the arXiv API.

Date semantics
--------------
A "digest date" (the YYYY-MM-DD used in digest_*.md filenames) is the date in
the digest timezone (config ``timezone.digest``, default Asia/Shanghai) on
which a batch of announcements becomes visible: arXiv announces at 20:00 US
Eastern time, which is 08:00 the next calendar day in Asia/Shanghai, so the
digest date equals the announcement date + 1 day in both calendars.

Announcement schedule (current, in effect since ~Oct 2021)
----------------------------------------------------------
arXiv announces new submissions (plus replacements, cross-listings and
withdrawal notices) at 20:00 ET on Sunday..Thursday; there are no
announcements on Friday or Saturday.  Submission windows (14:00 ET cutoff),
per https://info.arxiv.org/help/availability.html:

    Mon 14:00 - Tue 14:00  -> announced Tue 20:00 ET (visible Wed 08:00 CST)
    Tue 14:00 - Wed 14:00  -> announced Wed 20:00 ET
    Wed 14:00 - Thu 14:00  -> announced Thu 20:00 ET
    Thu 14:00 - Fri 14:00  -> announced Sun 20:00 ET
    Fri 14:00 - Mon 14:00  -> announced Mon 20:00 ET

The official per-category listing (https://arxiv.org/list/astro-ph/recent)
labels each batch with the date its mailing completes, which equals the
digest (visible) date.  It is the authoritative source for "which papers
belong to which day": fetching the section labelled with the digest date
directly needs no timezone arithmetic, and it automatically reflects holiday
deferrals.  The API query path is only a fallback (listing unreachable, or a
digest date older than the five sections the recent page keeps).
"""

import fcntl
import os
import re
import time
from contextlib import contextmanager
from datetime import date as date_cls
from datetime import datetime, time as time_cls, timedelta, timezone
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
_DIGEST_TZ_DEFAULT = "Asia/Shanghai"
_ANNOUNCEMENT_TZ = "America/New_York"
_AVAILABLE_AFTER_DEFAULT = time_cls(10, 0)
# Digest dates (BJT) that map to a non-announcement ET day: BJT Sat/Sun.
_NO_ANNOUNCEMENT_WEEKDAYS = (5, 6)  # ET Fri/Sat


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


def _announcement_window_et(
    et_announce: date_cls,
) -> Optional[tuple[date_cls, date_cls]]:
    """Return the [start, end) submission-window dates (ET, 14:00 cutoffs) for
    the announcement on ``et_announce`` (an ET date), or None when arXiv makes
    no announcement that day (Friday/Saturday).

    Mirrors the current official schedule:
        Sun: [Thu 14:00, Fri 14:00)
        Mon: [Fri 14:00, Mon 14:00)
        Tue-Thu: [previous day 14:00, same day 14:00)
    """
    wd = et_announce.weekday()  # 0=Mon .. 6=Sun
    if wd == 6:  # Sunday announcement: Thursday-Friday window
        return (
            et_announce - timedelta(days=3),
            et_announce - timedelta(days=2),
        )
    if wd == 0:  # Monday announcement: Friday-Monday window
        return (
            et_announce - timedelta(days=3),
            et_announce,
        )
    if wd in (1, 2, 3):  # Tue, Wed, Thu: previous day to same day
        return (
            et_announce - timedelta(days=1),
            et_announce,
        )
    return None  # Friday/Saturday: no announcement


def _window_to_utc(
    window_dates: tuple[date_cls, date_cls],
) -> tuple[datetime, datetime]:
    """Convert a (start, end) ET-date window into UTC cutoffs at 14:00 ET."""
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo(_ANNOUNCEMENT_TZ)
    except ImportError:  # pragma: no cover - Python < 3.9 fallback
        et = timezone(timedelta(hours=-4))
    start_et = datetime(
        window_dates[0].year, window_dates[0].month, window_dates[0].day,
        14, 0, tzinfo=et,
    )
    end_et = datetime(
        window_dates[1].year, window_dates[1].month, window_dates[1].day,
        14, 0, tzinfo=et,
    )
    return (
        start_et.astimezone(timezone.utc),
        end_et.astimezone(timezone.utc),
    )


def _parse_recent_listing_sections(content: str) -> dict[date_cls, list[str]]:
    """Extract every dated section's IDs from an arXiv recent-listing page.

    Sections are labelled with the date the mailing completes, which is the
    digest (visible) date for that batch.  Returns {label_date: [ids,...]}.
    """
    sections: dict[date_cls, list[str]] = {}
    section_pattern = re.compile(
        r"<h3>(\w{3}), (\d{1,2}) (\w{3}) (\d{4}) "
        r"\(showing \d+ of \d+ entries?\s*\)</h3>(.*?)(?=</dl>)",
        re.DOTALL,
    )
    for m in section_pattern.finditer(content):
        weekday_token, day, month_token, year = (
            m.group(1), int(m.group(2)), m.group(3), int(m.group(4)),
        )
        if month_token not in _MONTHS:
            continue
        month = _MONTHS.index(month_token) + 1
        try:
            label = date_cls(year, month, day)
        except ValueError:
            continue
        # Guard against a heading that names a different weekday than the date.
        if _WEEKDAYS[label.weekday()] != weekday_token:
            continue
        ids = re.findall(r'href\s*=\s*["\']/abs/([0-9.]+)["\']', m.group(5))
        sections[label] = ids
    return sections


def _fetch_recent_listing() -> Optional[dict[date_cls, list[str]]]:
    """Fetch and parse the astro-ph recent listing page.

    Returns {label_date: [ids,...]} on success, or None when the page cannot
    be fetched (network/proxy failure) so callers can fall back to the API.
    """
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
    return _parse_recent_listing_sections(content)


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


def resolve_daily_batch(
    target_date: str,
    now_local: Optional[datetime] = None,
    available_after: Optional[time_cls] = None,
) -> dict:
    """Decide how to obtain the batch for digest date ``target_date``.

    Args:
        target_date: digest date YYYY-MM-DD (a visible date in the digest
            timezone; the section the official listing labels with this date).
        now_local: current time in the digest timezone (defaults to now UTC).
        available_after: the digest-timezone clock time after which today's
            batch is expected to have appeared (defaults to 10:00).

    Returns a dict with:
        status: "ok" | "no_announcement" | "not_yet_available"
                | "deferred_or_lagging" | "listing_unavailable"
        ids: list of paper IDs (only when status == "ok")
        et_announcement: the ET date of the 20:00 ET announcement
        window_utc: (start, end) UTC submission-window cutoffs or None
        message: human-readable explanation
    """
    now_local = now_local or datetime.now(timezone.utc)
    available_after = available_after or _AVAILABLE_AFTER_DEFAULT
    target = date_cls.fromisoformat(target_date)
    # Announcement at 20:00 ET is visible at 08:00 the next day in the digest
    # timezone; the calendar-date numbers coincide, so the ET announcement
    # date is simply the day before the digest date.
    et_announce = target - timedelta(days=1)
    window_dates = _announcement_window_et(et_announce)

    if window_dates is None:
        return {
            "status": "no_announcement",
            "ids": None,
            "et_announcement": et_announce,
            "window_utc": None,
            "message": (
                f"{target} ({_WEEKDAYS[target.weekday()]}) has no arXiv "
                "announcement: arXiv announces Sunday-Thursday only, so "
                "nothing new becomes visible on BJT Saturday/Sunday."
            ),
        }

    window_utc = _window_to_utc(window_dates)
    sections = _fetch_recent_listing()

    if sections is None:
        return {
            "status": "listing_unavailable",
            "ids": None,
            "et_announcement": et_announce,
            "window_utc": window_utc,
            "message": "Official astro-ph recent listing unavailable; using API fallback.",
        }

    if target in sections:
        return {
            "status": "ok",
            "ids": sections[target],
            "et_announcement": et_announce,
            "window_utc": window_utc,
            "message": f"Official astro-ph listing: {len(sections[target])} entries.",
        }

    if not sections or target < min(sections):
        # The recent page keeps only the most recent announcement days; an
        # older digest date must fall back to the API window query.
        return {
            "status": "listing_unavailable",
            "ids": None,
            "et_announcement": et_announce,
            "window_utc": window_utc,
            "message": (
                f"Target date {target} is outside the recent-listing window "
                f"({min(sections)}..{max(sections)}); using API fallback."
            ),
        }

    # Target is inside the page's date window but has no section: either the
    # batch has not been mailed yet today, or the announcement was deferred
    # (US holiday, ad hoc arXiv deferral).
    if target == now_local.date() and now_local.time() < available_after:
        return {
            "status": "not_yet_available",
            "ids": None,
            "et_announcement": et_announce,
            "window_utc": window_utc,
            "message": (
                f"Today's ({target}) arXiv batch is not published yet "
                f"(expected after {available_after.isoformat()}); do not write "
                "a digest now."
            ),
        }
    return {
        "status": "deferred_or_lagging",
        "ids": None,
        "et_announcement": et_announce,
        "window_utc": window_utc,
        "message": (
            f"No listing section for {target} ({_WEEKDAYS[target.weekday()]}); "
            "the announcement was likely deferred (US holiday / arXiv status "
            "page) or the listing lags."
        ),
    }


def _fetch_api_window(
    categories: list[str],
    include_cross: bool,
    include_replacements: bool,
    cutoff_start: Optional[datetime],
    cutoff_end: datetime,
    max_results: int = 500,
    max_retries: int = 1,
) -> list[dict]:
    """Fetch papers whose published/updated time falls in [cutoff_start, cutoff_end).

    Used by the fallback path when the official listing cannot supply the
    batch (page unreachable, or digest date older than the recent page).
    """
    cat_query = " OR ".join(f"cat:{c}" for c in categories)

    client = _new_client()
    proxy_retried = False
    effective_max = max_results

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
                        if cutoff_start is not None:
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


def fetch_daily_batch(
    categories: list[str],
    include_cross: bool,
    include_replacements: bool,
    target_date: str,
    now_local: Optional[datetime] = None,
    available_after: Optional[time_cls] = None,
) -> dict:
    """Fetch the exact daily batch for digest date ``target_date``.

    Prefers the official astro-ph recent listing (authoritative, handles
    holiday deferrals automatically); falls back to an API window query when
    the listing is unreachable or the date has scrolled off the recent page.

    Returns {"status": ..., "papers": [...], "et_announcement": ...,
    "window_utc": ..., "message": ...}.  Status is "ok" when papers were
    fetched (possibly via fallback); otherwise one of "no_announcement",
    "not_yet_available", "deferred_or_lagging" with an empty paper list.
    """
    resolved = resolve_daily_batch(
        target_date, now_local=now_local, available_after=available_after,
    )
    if resolved["status"] == "ok":
        try:
            papers = _fetch_listed_papers(
                resolved["ids"],
                categories,
                include_cross,
                include_replacements,
            )
            return {
                **resolved,
                "status": "ok",
                "papers": papers,
                "message": (
                    f"Official astro-ph listing: {len(resolved['ids'])} "
                    f"entries for {_recent_date_label(target_date)}."
                ),
            }
        except requests.exceptions.ProxyError:
            print("  Proxy connection failed; retrying without the system proxy...")
            papers = _fetch_listed_papers(
                resolved["ids"],
                categories,
                include_cross,
                include_replacements,
                trust_env=False,
            )
            return {**resolved, "status": "ok", "papers": papers}

    if resolved["status"] == "listing_unavailable":
        cutoff_start, cutoff_end = resolved["window_utc"]
        print(f"  Submission window: {cutoff_start.isoformat()} to {cutoff_end.isoformat()}")
        papers = _fetch_api_window(
            categories,
            include_cross,
            include_replacements,
            cutoff_start,
            cutoff_end,
            max_results=500,
        )
        return {
            **resolved,
            "status": "ok",
            "papers": papers,
            "message": f"API fallback for {target_date}: {len(papers)} papers.",
        }

    # no_announcement / not_yet_available / deferred_or_lagging
    return {**resolved, "papers": []}


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
        days: number of past days to search (used only without target_date)
        max_results: maximum number of results to return
        max_retries: number of retry attempts on network failure
        include_cross: include cross-listed papers
        include_replacements: include replacement (updated) papers
        target_date: digest date (YYYY-MM-DD) - a visible date in the digest
            timezone, i.e. the date the official listing labels the batch with

    Returns:
        list of dicts with keys: id, title, authors, abstract, categories,
        published, pdf_url, entry, paper_type
    """
    if target_date:
        result = fetch_daily_batch(
            categories,
            include_cross,
            include_replacements,
            target_date,
            now_local=datetime.now(timezone.utc),
            available_after=time_cls(0, 0),  # compat: no time-of-day guard
        )
        return result["papers"]

    # Build query: (cat:astro-ph.GA OR cat:astro-ph.SR OR ...)
    cat_query = " OR ".join(f"cat:{c}" for c in categories)

    client = _new_client()
    proxy_retried = False
    effective_max = min(max_results, 300)

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
                        if result_date < cutoff_end:
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
