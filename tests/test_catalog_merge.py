"""Satellite-day rules. The first import must not mint hundreds of new leaks."""

from __future__ import annotations

import unittest
from datetime import date

from scripts.catalog_lib import merge_characters, pinyin_keys, search_keys, status_from_release


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


class SearchKeyTest(unittest.TestCase):
    def test_heteronyms_cover_the_real_reading(self) -> None:
        self.assertIn("luokexi", pinyin_keys("洛克茜"))
        self.assertIn("lkx", pinyin_keys("洛克茜"))
        self.assertIn("changli", pinyin_keys("长离"))
        self.assertIn("cl", pinyin_keys("长离"))
        self.assertIn("chongyun", pinyin_keys("重云"))

    def test_non_han_names_have_no_keys_and_mixed_names_keep_letters(self) -> None:
        self.assertEqual([], pinyin_keys("Lucy"))
        self.assertEqual([], pinyin_keys("ニケ"))
        self.assertIn("xingshenaxi", pinyin_keys("星神★阿希"))
        self.assertIn("d2hao", pinyin_keys("D2号"))
        self.assertIn("d2h", pinyin_keys("D2号"))

    def test_combinations_are_capped(self) -> None:
        keys = pinyin_keys("长长长长长长")
        self.assertLessEqual(len(keys), 32)

    def test_search_keys_include_chinese_aliases(self) -> None:
        keys = search_keys("心", ["心月狐", "Xin"])
        self.assertIn("xin", keys)
        self.assertIn("xyh", keys)


if __name__ == "__main__":
    unittest.main()
