"""明日方舟：终末地 roster parsing, art matching, and first-import baseline."""

from __future__ import annotations

import unittest

from scripts.catalog_lib import GAME_COPYRIGHT, game_copyright_matches, merge_characters
from scripts.fetch_catalog import (
    build_endfield_character,
    index_endfield_art,
    pick_endfield_art,
    shrink_guard,
)
from scripts.fetch_heat import as_watch_item


def _detail(name: str, infobox: list | None = None, kind: int = 1) -> dict:
    return {"name": name, "type": kind, "infobox": infobox or []}


def _item(cid: int, name: str, kind: int = 1, relation: str = "配角") -> dict:
    return {
        "id": cid,
        "name": name,
        "type": kind,
        "relation": relation,
        "images": {
            "large": f"https://lain.bgm.tv/pic/crt/l/{cid}.jpg",
            "grid": f"https://lain.bgm.tv/pic/crt/g/{cid}.jpg",
        },
    }


class EndfieldParseTest(unittest.TestCase):
    def test_people_only_and_two_administrators_stay_distinct(self) -> None:
        female = build_endfield_character(
            _item(141263, "管理员", relation="主角"),
            _detail(
                "管理员",
                [
                    {
                        "key": "别名",
                        "value": [
                            {"k": "英文名", "v": "Endministrator"},
                            {"k": "昵称", "v": "黑夜叉"},
                        ],
                    },
                    {"key": "性别", "value": "女"},
                ],
            ),
            {},
        )
        male = build_endfield_character(
            _item(141264, "管理员", relation="主角"),
            _detail(
                "管理员",
                [
                    {"key": "别名", "value": [{"k": "英文名", "v": "Endministrator"}]},
                    {"key": "性别", "value": "男"},
                ],
            ),
            {},
        )
        org = build_endfield_character(_item(191946, "罗德岛", kind=4), _detail("罗德岛", kind=4), {})
        extra = build_endfield_character(
            _item(142460, "大山", relation="闲角"),
            _detail("大山", [{"key": "别名", "value": [{"k": "英文名", "v": "Fjall"}]}]),
            {},
        )
        blank = build_endfield_character(_item(224762, "-"), _detail("-"), {})
        assert female is not None and male is not None and extra is not None
        self.assertIsNone(org)
        self.assertIsNone(blank)
        self.assertEqual("bgm:141263", female["key"])
        self.assertEqual("bgm:141264", male["key"])
        self.assertEqual("管理员", female["name"])
        self.assertEqual("管理员", male["name"])
        self.assertNotEqual(female["key"], male["key"])
        self.assertEqual("endfield", female["game"])
        self.assertEqual("明日方舟：终末地", female["work"])
        self.assertEqual("GAME", female["kind"])
        self.assertEqual("EVERGREEN", female["status"])
        self.assertIsNone(female["releaseDate"])
        self.assertIn("Endministrator", female["aliases"])
        self.assertIn("黑夜叉", female["aliases"])
        self.assertEqual("大山", extra["name"])
        self.assertIn("Fjall", extra["aliases"])

    def test_simplified_chinese_name_and_html_unescape(self) -> None:
        row = build_endfield_character(
            _item(1, "Chen"),
            _detail(
                "Chen",
                [
                    {"key": "简体中文名", "value": "陈&#38;语"},
                    {"key": "别名", "value": "A&amp;B"},
                ],
            ),
            {},
        )
        assert row is not None
        self.assertEqual("陈&语", row["name"])
        self.assertIn("A&B", row["aliases"])
        self.assertNotIn("&#", row["name"])
        self.assertNotIn("&amp;", " ".join(row["aliases"]))

    def test_portrait_then_splash_then_bangumi_and_icon_is_the_sample(self) -> None:
        index = index_endfield_art(
            [
                {"Operator": "Chen Qianyu", "Gender": "Female"},
                {"Operator": "Endministrator", "Gender": "Female"},
                {"Operator": "Endministrator", "Gender": "Male"},
                {"Operator": "Camille", "Gender": "Female"},
            ],
            {
                "File:Chen Qianyu.png": "https://endfield.wiki.gg/images/Chen_Qianyu.png",
                "File:Chen Qianyu icon.png": "https://endfield.wiki.gg/images/Chen_Qianyu_icon.png",
                "File:Chen Qianyu Splash Art.png": "https://endfield.wiki.gg/images/Chen_Qianyu_Splash_Art.png",
                "File:Endministrator.png": "https://endfield.wiki.gg/images/Endministrator.png",
                "File:Endministrator icon.png": "https://endfield.wiki.gg/images/Endministrator_icon.png",
                "File:Endministrator (Female) icon.png": "https://endfield.wiki.gg/images/endmin_f_icon.png",
                "File:Endministrator (Male) icon.png": "https://endfield.wiki.gg/images/endmin_m_icon.png",
                "File:Endministrator (Female) Splash Art.png": "https://endfield.wiki.gg/images/endmin_f.png",
                "File:Endministrator (Male) Splash Art.png": "https://endfield.wiki.gg/images/endmin_m.png",
                "File:Camille Splash Art.png": "https://endfield.wiki.gg/images/Camille_Splash_Art.png",
            },
            {"camille": ["卡缪"]},
        )
        chen = build_endfield_character(
            _item(142397, "陈"),
            _detail(
                "陈",
                [
                    {"key": "简体中文名", "value": "陈千语"},
                    {"key": "别名", "value": [{"k": "英文名", "v": "Chen Qianyu"}]},
                    {"key": "性别", "value": "女"},
                ],
            ),
            index,
        )
        female = build_endfield_character(
            _item(141263, "管理员"),
            _detail(
                "管理员",
                [
                    {"key": "别名", "value": [{"k": "英文名", "v": "Endministrator"}]},
                    {"key": "性别", "value": "女"},
                ],
            ),
            index,
        )
        unknown = build_endfield_character(
            _item(141264, "管理员"),
            _detail("管理员", [{"key": "别名", "value": [{"k": "英文名", "v": "Endministrator"}]}]),
            index,
        )
        camille = build_endfield_character(_item(208374, "卡缪"), _detail("卡缪"), index)
        assert chen is not None and female is not None and unknown is not None and camille is not None
        self.assertEqual("陈千语", chen["name"])
        self.assertEqual("https://endfield.wiki.gg/images/Chen_Qianyu.png", chen["artUrl"])
        self.assertEqual("https://endfield.wiki.gg/images/Chen_Qianyu_icon.png", chen["hairSampleUrl"])
        self.assertEqual("https://endfield.wiki.gg/images/endmin_f.png", female["artUrl"])
        self.assertEqual("https://endfield.wiki.gg/images/endmin_f_icon.png", female["hairSampleUrl"])
        self.assertIsNone(pick_endfield_art(index, "Endministrator", ""))
        self.assertEqual("https://lain.bgm.tv/pic/crt/l/141264.jpg", unknown["artUrl"])
        self.assertEqual("https://lain.bgm.tv/pic/crt/g/141264.jpg", unknown["hairSampleUrl"])
        self.assertNotIn("Endministrator.png", unknown["artUrl"])
        self.assertEqual("https://endfield.wiki.gg/images/Camille_Splash_Art.png", camille["artUrl"])
        self.assertEqual("https://lain.bgm.tv/pic/crt/g/208374.jpg", camille["hairSampleUrl"])
        bust = index_endfield_art(
            [{"Operator": "Tangtang", "Gender": "Female"}],
            {
                "File:Tangtang.png": {
                    "url": "https://endfield.wiki.gg/images/Tangtang.png",
                    "width": 152,
                    "height": 212,
                },
                "File:Tangtang Splash Art.png": "https://endfield.wiki.gg/images/Tangtang_Splash_Art.png",
                "File:Tangtang icon.png": "https://endfield.wiki.gg/images/Tangtang_icon.png",
            },
        )
        tangtang = build_endfield_character(
            _item(190912, "汤汤"),
            _detail("汤汤", [{"key": "别名", "value": [{"k": "英文名", "v": "Tangtang"}]}]),
            bust,
        )
        assert tangtang is not None
        self.assertEqual("https://endfield.wiki.gg/images/Tangtang_Splash_Art.png", tangtang["artUrl"])
        self.assertEqual("https://endfield.wiki.gg/images/Tangtang_icon.png", tangtang["hairSampleUrl"])


class EndfieldMergeTest(unittest.TestCase):
    def test_first_endfield_import_is_baseline_and_other_sources_stay(self) -> None:
        previous = {
            "characters": [
                {
                    "key": "ak:chen",
                    "name": "陈",
                    "source": "arknights",
                    "work": "明日方舟",
                    "firstSeenAt": "2026-10-19",
                    "baseline": True,
                    "artUrl": "https://example.test/chen.png",
                },
                {
                    "key": "bgm:1",
                    "name": "猫猫",
                    "source": "bangumi",
                    "work": "药屋",
                    "firstSeenAt": "2026-10-19",
                    "baseline": True,
                },
            ]
        }
        fresh = build_endfield_character(_item(142397, "陈千语"), _detail("陈千语"), {})
        assert fresh is not None
        merged = merge_characters(previous, {"endfield": [fresh]}, "2026-10-20")
        by_key = {row["key"]: row for row in merged}
        self.assertEqual({"ak:chen", "bgm:1", "bgm:142397"}, set(by_key))
        self.assertEqual("arknights", by_key["ak:chen"]["source"])
        self.assertEqual("明日方舟", by_key["ak:chen"]["work"])
        self.assertEqual("2026-10-19", by_key["ak:chen"]["firstSeenAt"])
        self.assertTrue(by_key["bgm:142397"]["baseline"])
        self.assertEqual("EVERGREEN", by_key["bgm:142397"]["status"])
        self.assertEqual("2026-10-20", by_key["bgm:142397"]["firstSeenAt"])
        later = merge_characters(
            {"characters": merged},
            {"endfield": [fresh, build_endfield_character(_item(9, "新干员"), _detail("新干员"), {})]},
            "2026-10-21",
        )
        later_by_key = {row["key"]: row for row in later}
        self.assertTrue(later_by_key["bgm:142397"]["baseline"])
        self.assertEqual("2026-10-20", later_by_key["bgm:142397"]["firstSeenAt"])
        self.assertFalse(later_by_key["bgm:9"]["baseline"])
        self.assertEqual("2026-10-21", later_by_key["bgm:9"]["firstSeenAt"])
        self.assertEqual("arknights", later_by_key["ak:chen"]["source"])

    def test_shrink_guard_lets_a_new_source_through(self) -> None:
        previous = {
            "characters": [{"source": "arknights", "key": f"ak:{i}"} for i in range(40)],
        }
        rows = [{"source": "endfield", "key": "bgm:1"}]
        self.assertEqual(rows, shrink_guard("endfield", rows, previous))
        self.assertEqual(rows, shrink_guard("endfield", rows, None))
        grown = {
            "characters": [{"source": "endfield", "key": f"bgm:{i}"} for i in range(40)],
        }
        self.assertIsNone(shrink_guard("endfield", rows, grown))


class EndfieldHeatTest(unittest.TestCase):
    def test_synonyms_include_the_short_title(self) -> None:
        item = as_watch_item(
            {
                "name": "佩丽卡",
                "aliases": ["Perlica"],
                "game": "endfield",
                "work": "明日方舟：终末地",
                "kind": "GAME",
            },
            "佩丽卡",
        )
        self.assertIn("终末地", item["workSynonyms"])
        self.assertIn("明日方舟：终末地", item["workSynonyms"])
        self.assertIn("Arknights: Endfield", item["workSynonyms"])
        self.assertEqual(["佩丽卡 明日方舟：终末地"], item["bilibiliKeywords"])

    def test_copyright_constant_splits_the_two_games(self) -> None:
        self.assertEqual("arknights:_endfield", GAME_COPYRIGHT["endfield"])
        self.assertTrue(game_copyright_matches("endfield", [("arknights:_endfield", 0.9), ("arknights", 0.2)]))
        self.assertTrue(game_copyright_matches("endfield", [("arknights", 1.0), ("arknights:_endfield", 0.992)]))
        self.assertFalse(game_copyright_matches("endfield", [("arknights", 1.0)]))
        self.assertFalse(game_copyright_matches("endfield", [("arknights", 1.0), ("arknights:_endfield", 0.2)]))
        self.assertFalse(game_copyright_matches("arknights", [("arknights:_endfield", 1.0)]))


if __name__ == "__main__":
    unittest.main()
