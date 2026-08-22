"""Offline tests for the read-only Zotero profile adapter."""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.profile import build_profile_from_zotero
from src.zotero import ZoteroReadError, read_zotero_library


def _make_zotero_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INTEGER, dateAdded TEXT);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
        CREATE TABLE deletedItems (itemID INTEGER);
        CREATE TABLE collections (collectionID INTEGER PRIMARY KEY, collectionName TEXT, parentCollectionID INTEGER);
        INSERT INTO itemTypes VALUES (1, 'journalArticle');
        INSERT INTO itemTypes VALUES (2, 'book');
        INSERT INTO fields VALUES (1, 'title');
        INSERT INTO fields VALUES (2, 'abstractNote');
        INSERT INTO fields VALUES (3, 'date');
        INSERT INTO items VALUES (10, 1, '2026-08-01 10:00:00');
        INSERT INTO items VALUES (11, 2, '2026-08-02 10:00:00');
        INSERT INTO items VALUES (12, 1, '2025-01-01 10:00:00');
        INSERT INTO itemData VALUES (10, 1, 100);
        INSERT INTO itemData VALUES (10, 2, 101);
        INSERT INTO itemData VALUES (10, 3, 102);
        INSERT INTO itemData VALUES (12, 1, 103);
        INSERT INTO itemDataValues VALUES (100, 'Chemical enrichment in metal-poor stars');
        INSERT INTO itemDataValues VALUES (101, 'We study stellar abundances.');
        INSERT INTO itemDataValues VALUES (102, '2026-07-01');
        INSERT INTO itemDataValues VALUES (103, 'Deleted paper');
        INSERT INTO tags VALUES (20, 'Population III');
        INSERT INTO tags VALUES (21, 'stellar abundances');
        INSERT INTO itemTags VALUES (10, 20);
        INSERT INTO itemTags VALUES (10, 21);
        INSERT INTO collections VALUES (30, 'First Stars', NULL);
        INSERT INTO collections VALUES (31, 'Nested', 30);
        INSERT INTO deletedItems VALUES (12);
        """
    )
    connection.commit()
    connection.close()


class ZoteroProfileTests(unittest.TestCase):
    def test_reads_supported_items_and_builds_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "zotero.sqlite"
            _make_zotero_database(database)

            library = read_zotero_library(str(database))
            self.assertEqual(library["item_count"], 1)
            self.assertEqual(library["deleted_count"], 1)
            self.assertEqual(library["collection_count"], 1)
            self.assertEqual(library["tag_count"], 2)
            self.assertEqual(library["entries"][0]["title"], "Chemical enrichment in metal-poor stars")

            profile = build_profile_from_zotero(str(database))
            self.assertEqual(profile["source"], "zotero")
            self.assertEqual(profile["zotero_summary"]["item_count"], 1)
            self.assertIn("Population III", profile["keywords"])
            self.assertIn("First Stars", profile["keywords"])
            self.assertIn("Chemical enrichment in metal-poor stars", profile["recent_titles"])

    def test_reports_missing_or_invalid_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.sqlite"
            with self.assertRaises(ZoteroReadError) as missing_error:
                read_zotero_library(str(missing))
            self.assertIn("No Zotero database was found", str(missing_error.exception))

            invalid = Path(temp_dir) / "invalid.sqlite"
            invalid.write_text("not a sqlite database", encoding="utf-8")
            with self.assertRaises(ZoteroReadError) as invalid_error:
                read_zotero_library(str(invalid))
            self.assertIn("not a valid SQLite Zotero database", str(invalid_error.exception))


if __name__ == "__main__":
    unittest.main()
