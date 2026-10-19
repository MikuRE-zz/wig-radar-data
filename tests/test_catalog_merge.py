"""Satellite-day rules. The first import must not mint hundreds of new leaks."""

from __future__ import annotations

import unittest
from datetime import date

from scripts.catalog_lib import merge_characters, status_from_release


def _row(key: str, source: str = "nanoka-gi") -> dict:
    return {
        "key": key,
        "name": key,
        "aliases": [],
        "work": "原神",
        "game": "genshin",
        "version": "7.1",
        "kind": "GAME",
        "status": "EVERGREEN",
        "releaseDate": "2021-07-21",
        "artUrl": f"https://example.test/{key}.webp",
        "hairSampleUrl": None,
        "source": source,
    }


class CatalogMergeTest(unittest.TestCase):
    def test_first_import_is_baseline(self) -> None:
        rows = merge_characters(None, {"nanoka-gi": [_row(f"gi:{i}") for i in range(300)]}, "2026-10-19")
        self.assertEqual(300, len(rows))
        self.assertTrue(all(row["baseline"] is True for row in rows))
        self.assertTrue(all(row["firstSeenAt"] == "2026-10-19" for row in rows))
        self.assertEqual(0, sum(1 for row in rows if row["baseline"] is False))

    def test_new_key_after_baseline_is_not_baseline(self) -> None:
        first = {
            "characters": merge_characters(None, {"nanoka-gi": [_row("gi:1")]}, "2026-10-19"),
        }
        second = merge_characters(
            first,
            {"nanoka-gi": [_row("gi:1"), _row("gi:2")]},
            "2026-10-20",
        )
        by_key = {row["key"]: row for row in second}
        self.assertTrue(by_key["gi:1"]["baseline"])
        self.assertEqual("2026-10-19", by_key["gi:1"]["firstSeenAt"])
        self.assertFalse(by_key["gi:2"]["baseline"])
        self.assertEqual("2026-10-20", by_key["gi:2"]["firstSeenAt"])

    def test_failed_source_keeps_previous_characters(self) -> None:
        previous = {
            "characters": merge_characters(
                None,
                {"nanoka-gi": [_row("gi:1")], "arknights": [_row("ak:amiya", "arknights")]},
                "2026-10-19",
            ),
        }
        merged = merge_characters(
            previous,
            {"nanoka-gi": [_row("gi:1"), _row("gi:9")], "arknights": None},
            "2026-10-20",
        )
        keys = {row["key"] for row in merged}
        self.assertEqual({"gi:1", "gi:9", "ak:amiya"}, keys)
        amiya = next(row for row in merged if row["key"] == "ak:amiya")
        self.assertEqual("2026-10-19", amiya["firstSeenAt"])
        self.assertTrue(amiya["baseline"])

    def test_release_window(self) -> None:
        today = date(2026, 10, 19)
        self.assertEqual("LEAKED", status_from_release("GAME", None, today, leaked=True))
        self.assertEqual("EVERGREEN", status_from_release("GAME", None, today))
        self.assertEqual("LEAKED", status_from_release("ANIME", date(2027, 1, 8), today))
        self.assertEqual("LIVE", status_from_release("ANIME", date(2026, 10, 5), today))
        self.assertEqual("EVERGREEN", status_from_release("ANIME", date(2026, 9, 1), today))
        self.assertEqual("LIVE", status_from_release("GAME", date(2026, 9, 20), today))
        self.assertEqual("EVERGREEN", status_from_release("GAME", date(2026, 8, 1), today))


if __name__ == "__main__":
    unittest.main()
