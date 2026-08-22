"""Read a local Zotero SQLite library without opening it for writes.

Zotero may keep its live database locked while the application is running.
The reader therefore copies the database to a private temporary directory and
opens only that copy in read-only mode.  The public functions in this module
return plain dictionaries so the profile builder does not depend on Zotero's
internal file layout beyond the documented SQLite tables.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional


DEFAULT_ZOTERO_DB = Path.home() / "Zotero" / "zotero.sqlite"


class ZoteroReadError(RuntimeError):
    """A user-actionable error while locating or reading a Zotero library."""


def candidate_database_paths(configured_path: Optional[str] = None) -> list[Path]:
    """Return likely Zotero database locations in preference order."""
    candidates: list[Path] = []
    if configured_path and configured_path.strip():
        # An explicit path is a user choice.  Do not silently read a different
        # library if that path is missing or invalid.
        candidates.append(Path(os.path.expanduser(configured_path.strip())))
        return candidates

    candidates.append(DEFAULT_ZOTERO_DB)

    # This is not Zotero's usual data location, but is used by some custom
    # macOS installations.  Keep the search narrow; do not scan user files.
    app_support = Path.home() / "Library" / "Application Support" / "Zotero"
    candidates.append(app_support / "zotero.sqlite")
    profiles_dir = app_support / "Profiles"
    if profiles_dir.is_dir():
        candidates.extend(sorted(profiles_dir.glob("*/zotero.sqlite")))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _missing_database_error(attempted: list[Path]) -> ZoteroReadError:
    locations = "\n".join(f"  - {path}" for path in attempted)
    return ZoteroReadError(
        "No Zotero database was found in the usual locations.\n"
        "Please enter the path to your real zotero.sqlite file.\n"
        f"Locations checked:\n{locations}"
    )


def resolve_database_path(configured_path: Optional[str] = None) -> Path:
    """Resolve a configured path or a small set of common macOS locations."""
    candidates = candidate_database_paths(configured_path)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise _missing_database_error(candidates)


def _copy_to_private_database(source: Path) -> tuple[Path, Path]:
    temp_dir = Path(tempfile.mkdtemp(prefix="apd-zotero-"))
    copy_path = temp_dir / "zotero.sqlite"
    try:
        shutil.copy2(source, copy_path)
    except Exception as exc:  # noqa: BLE001 - convert OS errors to user text
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise ZoteroReadError(
            f"Could not copy the Zotero database for safe reading: {exc}"
        ) from exc
    return temp_dir, copy_path


def _read_from_copy(copy_path: Path) -> dict:
    connection = None
    try:
        connection = sqlite3.connect(f"{copy_path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")

        required_tables = {
            "items", "itemTypes", "itemData", "itemDataValues", "fields",
            "deletedItems", "itemTags", "tags",
        }
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing_tables = sorted(required_tables - table_names)
        if missing_tables:
            raise ZoteroReadError(
                "The selected file is not a compatible Zotero database; "
                f"missing tables: {', '.join(missing_tables)}."
            )

        item_type_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(itemTypes)")
        }
        if "typeName" in item_type_columns:
            item_type_name_column = "typeName"
        elif "type" in item_type_columns:
            item_type_name_column = "type"
        else:
            raise ZoteroReadError(
                "The selected file is not a compatible Zotero database; "
                "itemTypes has no type-name column."
            )

        rows = connection.execute(
            f"""
            SELECT
                i.itemID AS item_id,
                i.dateAdded AS date_added,
                MAX(CASE WHEN f.fieldName = 'title' THEN v.value END) AS title,
                MAX(CASE WHEN f.fieldName = 'abstractNote' THEN v.value END) AS abstract,
                MAX(CASE WHEN f.fieldName = 'date' THEN v.value END) AS published_date,
                MAX(CASE WHEN f.fieldName = 'url' THEN v.value END) AS url,
                MAX(CASE WHEN f.fieldName = 'DOI' THEN v.value END) AS doi,
                MAX(CASE WHEN f.fieldName = 'extra' THEN v.value END) AS extra,
                (
                    SELECT GROUP_CONCAT(t.name, ' || ')
                    FROM itemTags it
                    JOIN tags t ON it.tagID = t.tagID
                    WHERE it.itemID = i.itemID
                ) AS tags
            FROM items i
            JOIN itemTypes it ON it.itemTypeID = i.itemTypeID
            LEFT JOIN itemData d ON d.itemID = i.itemID
            LEFT JOIN fields f ON f.fieldID = d.fieldID
            LEFT JOIN itemDataValues v ON v.valueID = d.valueID
            WHERE it.{item_type_name_column} IN ('journalArticle', 'preprint')
              AND NOT EXISTS (
                  SELECT 1 FROM deletedItems di WHERE di.itemID = i.itemID
              )
            GROUP BY i.itemID, i.dateAdded
            ORDER BY i.itemID
            """
        ).fetchall()

        collection_rows = connection.execute(
            """
            SELECT collectionName
            FROM collections
            WHERE parentCollectionID IS NULL
            ORDER BY collectionName
            """
        ).fetchall() if "collections" in table_names else []

        deleted_count = 0
        if "deletedItems" in table_names:
            deleted_count = int(
                connection.execute("SELECT COUNT(*) FROM deletedItems").fetchone()[0]
            )

        entries: list[dict] = []
        tag_names: set[str] = set()
        for row in rows:
            tags = [tag.strip() for tag in (row["tags"] or "").split("||") if tag.strip()]
            tag_names.update(tags)
            published_date = (row["published_date"] or "").strip()
            date_added = (row["date_added"] or "").strip()
            year = ""
            for date_value in (published_date, date_added):
                if len(date_value) >= 4 and date_value[:4].isdigit():
                    year = date_value[:4]
                    break
            entries.append({
                "item_id": row["item_id"],
                "title": (row["title"] or "").strip(),
                "abstract": (row["abstract"] or "").strip(),
                "year": year,
                "date_added": date_added,
                "published_date": published_date,
                "url": (row["url"] or "").strip(),
                "doi": (row["doi"] or "").strip(),
                "extra": (row["extra"] or "").strip(),
                "tags": tags,
                # Existing profile extraction consumes BibTeX-like keywords.
                "keywords": ", ".join(tags),
            })

        return {
            "entries": entries,
            "collections": [row[0] for row in collection_rows if row[0]],
            "tags": sorted(tag_names),
            "deleted_count": deleted_count,
        }
    except ZoteroReadError:
        raise
    except sqlite3.DatabaseError as exc:
        if "not a database" in str(exc).lower():
            raise ZoteroReadError(
                "The selected file is not a valid SQLite Zotero database."
            ) from exc
        raise ZoteroReadError(
            f"Could not read the selected Zotero database: {exc}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def read_zotero_library(configured_path: Optional[str] = None) -> dict:
    """Read a Zotero library and return normalized entries plus a summary."""
    source = resolve_database_path(configured_path)
    temp_dir, copy_path = _copy_to_private_database(source)
    try:
        result = _read_from_copy(copy_path)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    result["database_path"] = str(source)
    result["item_count"] = len(result["entries"])
    result["collection_count"] = len(result["collections"])
    result["tag_count"] = len(result["tags"])
    return result
