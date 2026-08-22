#!/usr/bin/env python3
"""Backfill full abstracts for existing digest files.

The digest markdown intentionally keeps 300-char abstract snippets (for the
emailed digest).  The desktop digest page expands abstracts to their full text
via sidecar files (output/digests/digest_<date>.full.json), which are only
written when a digest is generated with the current output.py.  This script
backfills those sidecar files for existing digests by fetching full abstracts
from arXiv's export API.

Only the standard library is used, so it runs with any python3.

Usage:
    ./.venv/bin/python3 backfill_abstracts.py                 # all dates
    ./.venv/bin/python3 backfill_abstracts.py 2026-08-20      # one date
    ./.venv/bin/python3 backfill_abstracts.py --force         # overwrite
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from src.digest_parser import parse_digest, get_digest_path_for_date, get_available_dates  # noqa: E402

DIGEST_DIR = os.path.join(ROOT, "output", "digests")
ATOM = "{http://www.w3.org/2005/Atom}"


def _norm(paper_id):
    """Strip a trailing version suffix (e.g. 2608.18342v1 -> 2608.18342)."""
    base, sep, ver = paper_id.rpartition("v")
    if sep and ver.isdigit():
        return base
    return paper_id


def _fetch_full_abstracts(paper_ids):
    """Fetch full abstracts for a list of arXiv ids (batched, retried)."""
    fetched = {}  # entry_id (as returned by the API) -> full summary
    for i in range(0, len(paper_ids), 100):
        batch = paper_ids[i:i + 100]
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({
            "id_list": ",".join(batch),
            "max_results": str(len(batch)),
        })
        xml_bytes = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    xml_bytes = resp.read()
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2)
        root = ET.fromstring(xml_bytes)
        for entry in root.findall(ATOM + "entry"):
            entry_id = (entry.findtext(ATOM + "id") or "").rstrip("/").split("/")[-1]
            summary = (entry.findtext(ATOM + "summary") or "").strip()
            if entry_id and summary:
                fetched[entry_id] = summary
        time.sleep(1)
    return fetched


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dates", nargs="*", help="YYYY-MM-DD dates; default: all")
    ap.add_argument("--force", action="store_true", help="overwrite existing sidecar files")
    args = ap.parse_args()

    dates = args.dates or get_available_dates()
    for d in dates:
        sidecar = os.path.join(DIGEST_DIR, f"digest_{d}.full.json")
        if os.path.exists(sidecar) and not args.force:
            print(f"{d}: sidecar exists, skipping (use --force to overwrite)")
            continue
        digest_path = get_digest_path_for_date(d, digest_dir=DIGEST_DIR)
        if not digest_path:
            print(f"{d}: no digest file")
            continue
        parsed = parse_digest(digest_path)
        paper_ids = []
        for tier in parsed.get("tiers", []):
            for p in tier.get("papers", []):
                pid = p.get("paper_id")
                if pid:
                    paper_ids.append(pid)
        paper_ids = list(dict.fromkeys(paper_ids))
        if not paper_ids:
            print(f"{d}: no papers found in digest")
            continue

        print(f"{d}: fetching {len(paper_ids)} abstracts from arXiv ...", flush=True)
        fetched = _fetch_full_abstracts(paper_ids)

        # The API may return ids with or without the version suffix; match both.
        by_norm = {}
        for full_id in fetched:
            by_norm.setdefault(_norm(full_id), full_id)
        result = {}
        for pid in paper_ids:
            key = by_norm.get(pid) or by_norm.get(_norm(pid))
            if key and fetched.get(key):
                result[pid] = fetched[key]

        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"{d}: wrote {len(result)}/{len(paper_ids)} full abstracts -> {os.path.basename(sidecar)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
