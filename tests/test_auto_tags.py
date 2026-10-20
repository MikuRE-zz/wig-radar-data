"""Danbooru structure tags. No network."""

from __future__ import annotations

import unittest
from datetime import date

from scripts.catalog_lib import (
    AUTO_TAG_BATCH_LIMIT,
    AUTO_TAG_MIN_POSTS,
    AUTO_TAG_VERSION,
    anime_copyright_matches,
    auto_tag_queries,
    auto_tags_fresh,
    bangumi_subject_names,
    game_copyright_matches,
    latin_slugs,
    rank_auto_tag_targets,
    restore_previous_auto_tags,
    structure_tags_from_frequencies,
    frequency_stats,
)
from scripts.danbooru_tags import DanbooruOutage, refresh_auto_tags, resolve_auto_tags


TODAY = "2026-10-20"


def _freq(**values: float) -> dict[str, float]:
    return dict(values)


class FrequencyMapTest(unittest.TestCase):
    def test_very_long_wins_over_higher_long_hair(self) -> None:
        status, tags = structure_tags_from_frequencies(
            _freq(very_long_hair=0.49, long_hair=0.87, short_hair=0.4),
            100,
        )
        self.assertEqual("ok", status)
        self.assertEqual(["超长（80cm+）"], tags)

    def test_long_when_very_long_is_under_the_threshold(self) -> None:
        status, tags = structure_tags_from_frequencies(_freq(very_long_hair=0.299, long_hair=0.92), 100)
        self.assertEqual("ok", status)
        self.assertEqual(["长"], tags)

    def test_threshold_boundary(self) -> None:
        self.assertEqual(["长"], structure_tags_from_frequencies(_freq(long_hair=0.30), 100)[1])
        self.assertEqual([], structure_tags_from_frequencies(_freq(long_hair=0.299), 100)[1])

    def test_higher_length_wins_and_a_tie_keeps_the_longer_one(self) -> None:
        self.assertEqual(["中长"], structure_tags_from_frequencies(_freq(long_hair=0.31, medium_hair=0.55), 100)[1])
        self.assertEqual(["短发"], structure_tags_from_frequencies(_freq(short_hair=0.53, long_hair=0.14), 100)[1])
        self.assertEqual(["长"], structure_tags_from_frequencies(_freq(long_hair=0.40, medium_hair=0.40), 100)[1])

    def test_too_few_posts_emit_nothing(self) -> None:
        status, tags = structure_tags_from_frequencies(_freq(long_hair=0.99, twintails=0.99), AUTO_TAG_MIN_POSTS - 1)
        self.assertEqual("too-few-posts", status)
        self.assertEqual([], tags)
        status, tags = structure_tags_from_frequencies(_freq(long_hair=0.99), AUTO_TAG_MIN_POSTS)
        self.assertEqual("ok", status)
        self.assertEqual(["长"], tags)

    def test_families_and_one_length_only(self) -> None:
        status, tags = structure_tags_from_frequencies(
            _freq(
                very_long_hair=0.42,
                long_hair=0.74,
                braid=0.65,
                twin_braids=0.55,
                ahoge=0.35,
                big_hair=0.4,
                gradient_hair=0.5,
                streaked_hair=0.5,
                colored_inner_hair=0.5,
                double_bun=0.4,
                low_twintails=0.4,
                hime_cut=0.4,
                drill_hair=0.4,
                fox_ears=0.5,
                multicolored_hair=0.8,
                **{"two-tone_hair": 0.7},
            ),
            200,
        )
        self.assertEqual("ok", status)
        self.assertEqual(
            ["超长（80cm+）", "大发量", "渐变", "挑染", "内层异色", "编发", "盘发", "双马尾接片", "呆毛", "刘海定位", "硬定型（运输易塌）", "兽耳等配件"],
            tags,
        )
        self.assertNotIn("长", tags)

    def test_color_mix_is_stats_only(self) -> None:
        freq = _freq(long_hair=0.83, twintails=0.64, multicolored_hair=0.58, **{"two-tone_hair": 0.64})
        _status, tags = structure_tags_from_frequencies(freq, 100)
        self.assertEqual(["长", "双马尾接片"], tags)
        stats = frequency_stats(freq)
        self.assertEqual(0.58, stats["multicolored_hair"])
        self.assertEqual(0.64, stats["two-tone_hair"])
        self.assertNotIn("multicolored_hair", tags)
        self.assertNotIn("two-tone_hair", tags)

    def test_pointy_and_fake_ears_are_not_animal_ears(self) -> None:
        self.assertEqual([], structure_tags_from_frequencies(_freq(pointy_ears=0.9, fake_animal_ears=0.9), 100)[1])
        self.assertEqual(["兽耳等配件"], structure_tags_from_frequencies(_freq(horns=0.5), 100)[1])

    def test_reference_rows_follow_the_frequency_not_the_hand_labels(self) -> None:
        # 锁暝: very_long is only 0.08, so the length tag is 长. 编发 is absent.
        tags = structure_tags_from_frequencies(
            _freq(long_hair=0.92, very_long_hair=0.08, twintails=0.50, ahoge=0.56),
            304,
        )[1]
        self.assertEqual(["长", "双马尾接片", "呆毛"], tags)
        # 猫猫 is tagged long_hair, not medium, and the blunt bangs clear the line.
        tags = structure_tags_from_frequencies(
            _freq(long_hair=0.64, medium_hair=0.07, hair_bun=0.34, blunt_bangs=0.46),
            2186,
        )[1]
        self.assertEqual(["长", "盘发", "刘海定位"], tags)


class CopyrightMatchTest(unittest.TestCase):
    def test_game_requires_the_top_copyright_and_a_near_tie_counts(self) -> None:
        tied = [("honkai_(series)", 0.9998), ("honkai:_star_rail", 0.9998), ("mihoyo", 0.0162)]
        self.assertTrue(game_copyright_matches("hsr", tied))
        self.assertFalse(game_copyright_matches("genshin", tied))
        series_wins = [("honkai_(series)", 1.0), ("honkai:_star_rail", 0.5)]
        self.assertFalse(game_copyright_matches("hsr", series_wins))
        self.assertTrue(game_copyright_matches("zzz", [("zenless_zone_zero", 1.0)]))
        self.assertFalse(game_copyright_matches("wuwa", []))

    def test_anime_matches_original_romaji_or_other_names_and_rejects_the_rest(self) -> None:
        pairs = [("kusuriya_no_hitorigoto", 0.999)]
        others = {"kusuriya_no_hitorigoto": ["薬屋のひとりごと", "药屋少女的呢喃"]}
        self.assertTrue(anime_copyright_matches(pairs, ["药屋少女的呢喃 第三季"], others))
        self.assertTrue(
            anime_copyright_matches(pairs, ["Kusuriya no Hitorigoto 3rd Season"], {"kusuriya_no_hitorigoto": []})
        )
        self.assertFalse(anime_copyright_matches([("pokemon", 0.9)], ["药屋少女的呢喃"], others))

    def test_bangumi_subject_names_include_infobox_aliases(self) -> None:
        names = bangumi_subject_names(
            {
                "name": "薬屋のひとりごと 第3期",
                "name_cn": "药屋少女的呢喃 第三季",
                "infobox": [{"key": "别名", "value": [{"v": "Kusuriya no Hitorigoto 3rd Season"}]}],
            }
        )
        self.assertEqual(
            ["薬屋のひとりごと 第3期", "药屋少女的呢喃 第三季", "Kusuriya no Hitorigoto 3rd Season"],
            names,
        )


class QueryAndCacheTest(unittest.TestCase):
    def test_queries_skip_junk_and_try_latin_before_a_short_chinese_name(self) -> None:
        queries = auto_tag_queries(
            {"name": "心", "aliases": ["心月狐", "Hsin", "CH0368", "Jinhsi's Title", "여우"]}
        )
        self.assertEqual([("alias:en", "Hsin"), ("alias:zh", "心月狐"), ("name:zh", "心")], queries)
        self.assertEqual(["hu_tao", "hutao"], latin_slugs("Hu Tao"))
        self.assertEqual(["greythroat", "grey_throat"], latin_slugs("GreyThroat"))

    def test_fresh_window_and_error_is_not_reused(self) -> None:
        def record(status: str, day: str) -> dict:
            return {"version": AUTO_TAG_VERSION, "status": status, "fetchedAt": day}

        self.assertTrue(auto_tags_fresh(record("ok", "2026-08-21"), TODAY))
        self.assertFalse(auto_tags_fresh(record("ok", "2026-08-20"), TODAY))
        self.assertFalse(auto_tags_fresh(record("error", TODAY), TODAY))
        self.assertFalse(auto_tags_fresh(record("not-found", "2020-01-01"), TODAY))
        self.assertFalse(auto_tags_fresh({"version": "other", "status": "ok", "fetchedAt": TODAY}, TODAY))
        self.assertEqual(60, (date(2026, 10, 20) - date(2026, 8, 21)).days)
        self.assertEqual(61, (date(2026, 10, 20) - date(2026, 8, 20)).days)

    def test_radar_characters_come_first_and_the_batch_is_capped(self) -> None:
        rows = []
        for index in range(90):
            rows.append({"key": f"e{index:02d}", "status": "EVERGREEN", "name": "e"})
        rows.append({"key": "live-b", "status": "LIVE", "name": "b"})
        rows.append({"key": "live-a", "status": "LIVE", "name": "a"})
        rows.append({"key": "leak-b", "status": "LEAKED", "name": "b"})
        rows.append({"key": "leak-a", "status": "LEAKED", "name": "a"})
        rows.append(
            {
                "key": "fresh-leak",
                "status": "LEAKED",
                "name": "f",
                "autoTags": {"version": AUTO_TAG_VERSION, "status": "ok", "fetchedAt": TODAY},
            }
        )
        ranked = rank_auto_tag_targets(rows, TODAY)
        self.assertEqual(["leak-a", "leak-b", "live-a", "live-b"], [row["key"] for row in ranked[:4]])
        self.assertNotIn("fresh-leak", [row["key"] for row in ranked])
        capped = ranked[:AUTO_TAG_BATCH_LIMIT]
        self.assertEqual(AUTO_TAG_BATCH_LIMIT, len(capped))
        self.assertEqual(["leak-a", "leak-b", "live-a", "live-b"], [row["key"] for row in capped[:4]])


class _FakeDanbooru:
    def __init__(self) -> None:
        self.calls: list = []
        self.outage = False
        self.tags: dict[str, list[dict]] = {}
        self.lookup: dict[str, dict] = {}
        self.wiki: dict[str, list[dict]] = {}
        self.pages: dict[str, dict | None] = {}
        self.alias: dict[str, str] = {}
        self.related_map: dict[tuple[str, str], tuple[int, list]] = {}
        self.works: dict[int, list[str]] = {}

    def probe(self) -> None:
        self.calls.append("probe")
        if self.outage:
            raise DanbooruOutage("HTTP 403 blocked")

    def search_tags(self, slug: str) -> list[dict]:
        self.calls.append(("tags", slug))
        return list(self.tags.get(slug, []))

    def lookup_tag(self, name: str) -> dict | None:
        self.calls.append(("lookup", name))
        return self.lookup.get(name)

    def search_wiki(self, text: str) -> list[dict]:
        self.calls.append(("wiki", text))
        return list(self.wiki.get(text, []))

    def wiki_page(self, title: str) -> dict | None:
        self.calls.append(("page", title))
        return self.pages.get(title)

    def tag_alias(self, name: str) -> str | None:
        self.calls.append(("alias", name))
        return self.alias.get(name)

    def related(self, tag: str, category: str):
        self.calls.append(("related", tag, category))
        return self.related_map.get((tag, category), (0, []))

    def work_names(self, character: dict) -> list[str]:
        self.calls.append(("work", character.get("key")))
        subject = character.get("bangumiSubjectId")
        return list(self.works.get(subject, [])) if isinstance(subject, int) else []


def _indexes(rows: list[dict]) -> tuple[dict, dict]:
    from scripts.catalog_lib import character_name_index, latin_slug_index

    return character_name_index(rows), latin_slug_index(rows)


class ResolveTest(unittest.TestCase):
    def test_wrong_work_is_not_adopted(self) -> None:
        row = {"key": "gi:hutao", "name": "胡桃", "aliases": ["Hu Tao"], "game": "genshin", "status": "EVERGREEN"}
        fake = _FakeDanbooru()
        fake.tags["hu_tao"] = [{"name": "hu_tao_(blue_archive)", "post_count": 800, "category": 4}]
        fake.pages["hu_tao_(blue_archive)"] = {"title": "hu_tao_(blue_archive)", "other_names": ["胡桃", "Hu Tao"]}
        fake.related_map[("hu_tao_(blue_archive)", "copyright")] = (800, [("blue_archive", 1.0)])
        names, slugs = _indexes([row])
        record = resolve_auto_tags(row, fake, TODAY, names, slugs)
        self.assertEqual("work-mismatch", record["status"])
        self.assertEqual([], record["tags"])
        self.assertIsNone(record["danbooruTag"])
        self.assertNotIn(("related", "hu_tao_(blue_archive)", "general"), fake.calls)

    def test_matching_work_maps_frequencies(self) -> None:
        row = {"key": "gi:hutao", "name": "胡桃", "aliases": ["Hu Tao"], "game": "genshin", "status": "EVERGREEN"}
        fake = _FakeDanbooru()
        tag = "hu_tao_(genshin_impact)"
        fake.tags["hu_tao"] = [{"name": tag, "post_count": 14671, "category": 4}]
        fake.pages[tag] = {"title": tag, "other_names": ["胡桃", "Hu Tao"]}
        fake.related_map[(tag, "copyright")] = (14671, [("genshin_impact", 0.99)])
        fake.related_map[(tag, "general")] = (
            14671,
            [("long_hair", 0.83), ("twintails", 0.64), ("multicolored_hair", 0.58)],
        )
        names, slugs = _indexes([row])
        record = resolve_auto_tags(row, fake, TODAY, names, slugs)
        self.assertEqual("ok", record["status"])
        self.assertEqual(["长", "双马尾接片"], record["tags"])
        self.assertEqual(tag, record["danbooruTag"])
        self.assertEqual(14671, record["postCount"])
        self.assertEqual("alias:en", record["matchedBy"])
        self.assertEqual(0.58, record["stats"]["multicolored_hair"])
        self.assertEqual(TODAY, record["fetchedAt"])

    def test_same_game_alias_of_someone_else_is_not_found(self) -> None:
        suisui = {"key": "ww:suisui", "name": "穗穗", "aliases": ["Carlotta"], "game": "wuwa", "status": "EVERGREEN"}
        carlotta = {"key": "ww:carlotta", "name": "珂莱塔", "aliases": ["Carlotta"], "game": "wuwa", "status": "EVERGREEN"}
        fake = _FakeDanbooru()
        tag = "carlotta_(wuthering_waves)"
        fake.tags["carlotta"] = [{"name": tag, "post_count": 1000, "category": 4}]
        fake.pages[tag] = {"title": tag, "other_names": ["Carlotta", "珂莱塔"]}
        fake.related_map[(tag, "copyright")] = (1000, [("wuthering_waves", 1.0)])
        names, slugs = _indexes([suisui, carlotta])
        record = resolve_auto_tags(suisui, fake, TODAY, names, slugs)
        self.assertEqual("not-found", record["status"])
        self.assertEqual([], record["tags"])
        self.assertNotIn(("related", tag, "copyright"), fake.calls)

    def test_too_few_posts_skip_general_tags(self) -> None:
        row = {"key": "gi:1", "name": "胡桃", "aliases": ["Hu Tao"], "game": "genshin", "status": "EVERGREEN"}
        fake = _FakeDanbooru()
        tag = "hu_tao_(genshin_impact)"
        fake.tags["hu_tao"] = [{"name": tag, "post_count": 12, "category": 4}]
        fake.pages[tag] = {"title": tag, "other_names": ["胡桃"]}
        fake.related_map[(tag, "copyright")] = (12, [("genshin_impact", 1.0)])
        names, slugs = _indexes([row])
        record = resolve_auto_tags(row, fake, TODAY, names, slugs)
        self.assertEqual("too-few-posts", record["status"])
        self.assertEqual([], record["tags"])
        self.assertEqual(12, record["postCount"])
        self.assertNotIn(("related", tag, "general"), fake.calls)

    def test_anime_uses_bangumi_romaji_when_the_chinese_title_does_not_match(self) -> None:
        row = {
            "key": "bgm:1",
            "name": "猫猫",
            "aliases": ["Maomao"],
            "game": "anime",
            "work": "药屋少女的呢喃 第三季",
            "bangumiSubjectId": 568244,
            "status": "LIVE",
        }
        fake = _FakeDanbooru()
        tag = "maomao_(kusuriya_no_hitorigoto)"
        fake.tags["maomao"] = [{"name": tag, "post_count": 2000, "category": 4}]
        fake.pages[tag] = {"title": tag, "other_names": ["Maomao", "猫猫"]}
        fake.pages["kusuriya_no_hitorigoto"] = {"title": "kusuriya_no_hitorigoto", "other_names": []}
        fake.related_map[(tag, "copyright")] = (2000, [("kusuriya_no_hitorigoto", 0.99)])
        fake.related_map[(tag, "general")] = (2000, [("medium_hair", 0.52)])
        fake.works[568244] = ["Kusuriya no Hitorigoto"]
        names, slugs = _indexes([row])
        record = resolve_auto_tags(row, fake, TODAY, names, slugs)
        self.assertEqual("ok", record["status"])
        self.assertEqual(["中长"], record["tags"])
        self.assertIn(("work", "bgm:1"), fake.calls)

    def test_game_suffix_beats_a_more_posted_same_name(self) -> None:
        row = {"key": "gi:layla", "name": "莱依拉", "aliases": ["Layla"], "game": "genshin", "status": "EVERGREEN"}
        fake = _FakeDanbooru()
        tag = "layla_(genshin_impact)"
        fake.lookup[tag] = {"name": tag, "post_count": 1371, "category": 4}
        fake.tags["layla"] = [{"name": "layla_(idolmaster)", "post_count": 9000, "category": 4}]
        fake.pages[tag] = {"title": tag, "other_names": ["莱依拉", "Layla"]}
        fake.related_map[(tag, "copyright")] = (1371, [("genshin_impact", 1.0)])
        fake.related_map[(tag, "general")] = (1371, [("long_hair", 0.7)])
        names, slugs = _indexes([row])
        record = resolve_auto_tags(row, fake, TODAY, names, slugs)
        self.assertEqual("ok", record["status"])
        self.assertEqual(tag, record["danbooruTag"])
        self.assertNotIn(("tags", "layla"), fake.calls)
        self.assertNotIn(("related", "layla_(idolmaster)", "copyright"), fake.calls)

    def test_unique_game_suffix_is_accepted_when_the_wiki_page_is_missing(self) -> None:
        row = {"key": "gi:1", "name": "胡桃", "aliases": ["Hu Tao"], "game": "genshin", "status": "EVERGREEN"}
        fake = _FakeDanbooru()
        tag = "hu_tao_(genshin_impact)"
        fake.tags["hu_tao"] = [{"name": tag, "post_count": 100, "category": 4}]
        fake.pages[tag] = None
        fake.related_map[(tag, "copyright")] = (100, [("genshin_impact", 1.0)])
        fake.related_map[(tag, "general")] = (100, [("long_hair", 0.8)])
        names, slugs = _indexes([row])
        record = resolve_auto_tags(row, fake, TODAY, names, slugs)
        self.assertEqual("ok", record["status"])
        self.assertEqual(["长"], record["tags"])


class RefreshTest(unittest.TestCase):
    def test_outage_keeps_previous_values(self) -> None:
        old = {"version": AUTO_TAG_VERSION, "status": "ok", "tags": ["长"], "fetchedAt": "2026-01-01"}
        previous = {"characters": [{"key": "gi:1", "autoTags": old}]}
        rows = [{"key": "gi:1", "name": "胡桃", "aliases": ["Hu Tao"], "game": "genshin", "status": "LEAKED"}]
        fake = _FakeDanbooru()
        fake.outage = True
        summary = refresh_auto_tags(rows, previous, fake, TODAY, limit=80, budget_seconds=360)
        self.assertEqual("skipped", summary["status"])
        self.assertEqual(old, rows[0]["autoTags"])
        self.assertEqual(["probe"], fake.calls)

    def test_limit_prefers_leaked_and_the_budget_stops_the_batch(self) -> None:
        leaked = {"key": "zz:leak", "name": "甲", "aliases": ["Alpha"], "game": "zzz", "status": "LEAKED"}
        live = {"key": "gi:live", "name": "乙", "aliases": ["Beta"], "game": "genshin", "status": "LIVE"}
        fresh = {
            "key": "gi:fresh",
            "name": "丙",
            "aliases": ["Gamma"],
            "game": "genshin",
            "status": "LEAKED",
            "autoTags": {"version": AUTO_TAG_VERSION, "status": "not-found", "fetchedAt": TODAY},
        }
        fake = _FakeDanbooru()
        values = iter([0, 10, 9999])

        def clock() -> float:
            return next(values)

        summary = refresh_auto_tags(
            [live, fresh, leaked],
            None,
            fake,
            TODAY,
            limit=1,
            budget_seconds=100,
            clock=clock,
        )
        self.assertEqual(1, summary["attempted"])
        self.assertEqual("not-found", leaked["autoTags"]["status"])
        self.assertNotIn("autoTags", live)
        self.assertEqual("not-found", fresh["autoTags"]["status"])
        self.assertIn(("tags", "alpha"), fake.calls)
        self.assertNotIn(("tags", "beta"), fake.calls)
        self.assertNotIn(("tags", "gamma"), fake.calls)

    def test_restore_copies_previous_tags_onto_rebuilt_rows(self) -> None:
        old = {"version": AUTO_TAG_VERSION, "status": "ok", "tags": ["盘发"], "fetchedAt": TODAY}
        rows = [{"key": "ak:1", "name": "阿米娅", "aliases": ["Amiya"]}]
        restore_previous_auto_tags(rows, {"characters": [{"key": "ak:1", "autoTags": old}]})
        self.assertEqual(["盘发"], rows[0]["autoTags"]["tags"])


if __name__ == "__main__":
    unittest.main()
