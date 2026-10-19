"""Bangumi show selection, cast filter, and shrink-guard version bypass."""

from __future__ import annotations

import unittest
from datetime import date

from scripts.fetch_catalog import (
    BANGUMI_SELECTION,
    build_bangumi_character,
    collection_total,
    numeric_only_name,
    pick_cast,
    select_shows,
    shrink_guard,
    work_heats,
)


def _show(sid: int, season: str) -> dict:
    return {
        "id": sid,
        "name": f"show-{sid}",
        "season": season,
        "release": date(2026, 10, 4),
    }


def _ids(chosen: list[dict], season: str) -> list[int]:
    return [row["id"] for row in chosen if row.get("season") == season]


class AnimeSelectionTest(unittest.TestCase):
    def test_keeps_top_20_current_and_top_10_next(self) -> None:
        current = [_show(sid, "current") for sid in range(1, 31)]
        upcoming = [_show(sid, "next") for sid in range(101, 121)]
        heats = {sid: sid for sid in list(range(1, 31)) + list(range(101, 121))}
        chosen = select_shows(current, upcoming, heats)
        self.assertEqual(30, len(chosen))
        self.assertEqual(list(range(30, 10, -1)), _ids(chosen, "current"))
        self.assertEqual(list(range(120, 110, -1)), _ids(chosen, "next"))
        for row in chosen:
            self.assertEqual(heats[row["id"]], row["workHeat"])

    def test_short_current_season_is_filled_from_next(self) -> None:
        current = [_show(sid, "current") for sid in range(1, 9)]
        upcoming = [_show(sid, "next") for sid in range(201, 231)]
        heats = {sid: 1000 for sid in range(1, 9)}
        heats.update({sid: sid for sid in range(201, 231)})
        chosen = select_shows(current, upcoming, heats)
        self.assertEqual(30, len(chosen))
        self.assertEqual(list(range(1, 9)), _ids(chosen, "current"))
        self.assertEqual(list(range(230, 208, -1)), _ids(chosen, "next"))

    def test_short_next_season_is_filled_from_current(self) -> None:
        current = [_show(sid, "current") for sid in range(1, 41)]
        upcoming = [_show(sid, "next") for sid in range(301, 304)]
        heats = {sid: sid for sid in range(1, 41)}
        heats.update({301: 1000, 302: 1000, 303: 1000})
        chosen = select_shows(current, upcoming, heats)
        self.assertEqual(30, len(chosen))
        self.assertEqual([301, 302, 303], _ids(chosen, "next"))
        self.assertEqual(list(range(40, 13, -1)), _ids(chosen, "current"))

    def test_both_sides_short_keeps_everything(self) -> None:
        current = [_show(sid, "current") for sid in range(1, 5)]
        upcoming = [_show(sid, "next") for sid in range(5, 8)]
        chosen = select_shows(current, upcoming, {sid: 1 for sid in range(1, 8)})
        self.assertEqual({1, 2, 3, 4, 5, 6, 7}, {row["id"] for row in chosen})

    def test_failed_subject_heat_is_zero_and_does_not_abort(self) -> None:
        def fetch_subject(sid: int) -> dict:
            if sid == 2:
                raise RuntimeError("boom")
            if sid == 3:
                return {"name": "missing collection"}
            return {"collection": {"wish": 1, "collect": 2, "doing": 3, "on_hold": 4, "dropped": 5}}

        self.assertEqual(0, collection_total(None))
        self.assertEqual(0, collection_total({"collection": None}))
        self.assertEqual({1: 15, 2: 0, 3: 0}, work_heats([1, 2, 3], fetch_subject))

    def test_pick_cast_keeps_four_leads_in_api_order(self) -> None:
        listed = [
            {"relation": "配角", "id": 9},
            {"relation": "主角", "id": 1},
            {"relation": "主角", "id": 2},
            {"relation": "客串", "id": 8},
            {"relation": "主角", "id": 3},
            {"relation": "主角", "id": 4},
            {"relation": "主角", "id": 5},
        ]
        self.assertEqual([1, 2, 3, 4], [row["id"] for row in pick_cast(listed)])
        self.assertEqual([], pick_cast([{"relation": "配角", "id": 1}]))

    def test_mecha_leads_do_not_take_cast_slots(self) -> None:
        listed = [
            {"relation": "主角", "id": 1, "type": 1, "name": "シン・アスカ"},
            {"relation": "主角", "id": 2, "type": 2, "name": "ストライクフリーダムガンダム"},
            {"relation": "主角", "id": 3, "type": 3, "name": "ミレニアム"},
            {"relation": "主角", "id": 4, "type": 1, "name": "ルナマリア・ホーク"},
            {"relation": "主角", "id": 5, "type": 4, "name": "コンパス"},
        ]
        self.assertEqual([1, 4], [row["id"] for row in pick_cast(listed)])
        show = {"id": 9, "name": "作品", "release": date(2026, 10, 4), "workHeat": 10}
        mecha = build_bangumi_character(
            {"id": 2, "name": "ストライクフリーダムガンダム", "images": {}},
            show,
            date(2026, 10, 19),
            {"name": "ストライクフリーダムガンダム", "type": 2},
        )
        self.assertIsNone(mecha)

    def test_numeric_names_are_dropped_real_names_stay(self) -> None:
        self.assertTrue(numeric_only_name("556", "556"))
        self.assertTrue(numeric_only_name("", "556"))
        self.assertTrue(numeric_only_name("５５６", "5-5 6"))
        self.assertFalse(numeric_only_name("556", "Alice"))
        self.assertFalse(numeric_only_name("小明", "556"))
        show = {"id": 9, "name": "作品", "release": date(2026, 10, 4), "workHeat": 10}
        today = date(2026, 10, 19)
        dropped = build_bangumi_character(
            {"id": 556, "name": "556", "images": {}},
            show,
            today,
            {"name": "556"},
        )
        self.assertIsNone(dropped)
        kept = build_bangumi_character(
            {"id": 7, "name": "556", "images": {}},
            show,
            today,
            {"name": "556", "infobox": [{"key": "简体中文名", "value": "小明"}]},
        )
        self.assertIsNotNone(kept)
        assert kept is not None
        self.assertEqual("小明", kept["name"])

    def test_html_entities_are_unescaped_on_bangumi_names(self) -> None:
        show = {
            "id": 9,
            "name": "苍蓝传承 威尔什&amp;榭达",
            "release": date(2026, 10, 4),
            "workHeat": 4321,
        }
        row = build_bangumi_character(
            {"id": 3, "name": "Azaral", "images": {"large": "https://example.test/a.jpg"}},
            show,
            date(2026, 10, 19),
            {
                "name": "Azaral",
                "infobox": [
                    {"key": "简体中文名", "value": "威尔什&#38;榭达"},
                    {"key": "别名", "value": "A&amp;B"},
                ],
            },
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual("苍蓝传承 威尔什&榭达", row["work"])
        self.assertEqual("威尔什&榭达", row["name"])
        self.assertIn("A&B", row["aliases"])
        self.assertEqual(4321, row["workHeat"])
        self.assertNotIn("&amp;", row["work"])
        self.assertNotIn("&#", row["name"])


def _bangumi_previous(selection: str | None, count: int) -> dict:
    meta: dict = {}
    if selection is not None:
        meta["selection"] = selection
    return {
        "sources": {"bangumi": meta},
        "characters": [{"source": "bangumi", "key": f"bgm:{i}"} for i in range(count)],
    }


class ShrinkGuardSelectionTest(unittest.TestCase):
    def test_missing_or_different_selection_skips_guard(self) -> None:
        tiny = [{"source": "bangumi", "key": "bgm:new"}]
        self.assertEqual(tiny, shrink_guard("bangumi", tiny, _bangumi_previous(None, 40), selection=BANGUMI_SELECTION))
        self.assertEqual(
            tiny,
            shrink_guard("bangumi", tiny, _bangumi_previous("top-works-v0", 40), selection=BANGUMI_SELECTION),
        )

    def test_same_selection_still_guards(self) -> None:
        tiny = [{"source": "bangumi", "key": "bgm:new"}]
        previous = _bangumi_previous(BANGUMI_SELECTION, 40)
        self.assertIsNone(shrink_guard("bangumi", tiny, previous, selection=BANGUMI_SELECTION))
        half_ok = [{"source": "bangumi", "key": f"bgm:{i}"} for i in range(30)]
        self.assertEqual(half_ok, shrink_guard("bangumi", half_ok, previous, selection=BANGUMI_SELECTION))

    def test_other_sources_keep_the_guard(self) -> None:
        previous = {
            "sources": {"bangumi": {"selection": BANGUMI_SELECTION}},
            "characters": [{"source": "arknights", "key": f"ak:{i}"} for i in range(40)],
        }
        tiny = [{"source": "arknights", "key": "ak:new"}]
        self.assertIsNone(shrink_guard("arknights", tiny, previous))
        self.assertIsNone(shrink_guard("arknights", tiny, previous, selection=BANGUMI_SELECTION))


if __name__ == "__main__":
    unittest.main()
