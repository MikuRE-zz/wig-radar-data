"""Heat queue, time budget, and history retention. No network."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from scripts.fetch_heat import (
    DAILY_CAP,
    HISTORY_KEEP,
    beijing_day,
    build_history,
    build_queue,
    previous_metrics,
    run,
    take_within_budget,
)


def _character(
    key: str,
    status: str,
    *,
    kind: str = "GAME",
    game: str = "genshin",
    work: str = "原神",
    heat: int = 0,
    subject: int | None = None,
    name: str | None = None,
) -> dict:
    row = {
        "key": key,
        "name": name or key,
        "aliases": [],
        "status": status,
        "kind": kind,
        "game": game,
        "work": work,
        "source": "bangumi" if kind == "ANIME" else "nanoka-gi",
    }
    if kind == "ANIME":
        row["workHeat"] = heat
        if subject is not None:
            row["bangumiSubjectId"] = subject
    return row


def _flat(name: str, videos: int) -> dict:
    return {
        "name": name,
        "bangumiCollects": 1,
        "bangumiSubjectCollect": None,
        "bilibiliRecentVideos7d": videos,
        "bilibiliRecentPlaySum7d": 10,
        "heatScore": videos,
    }


def _live(name: str, videos: int, fetched_at: str, key: str | None = None) -> dict:
    return {
        "key": key or name,
        "name": name,
        "work": "原神",
        "kind": "GAME",
        "heatScore": videos,
        "heatFetchedAt": fetched_at,
        "heatComponents": {},
        "metrics": {
            "bangumi": {"collects": 1, "subjectCollect": None},
            "bilibili": {"recentVideos7d": videos, "recentPlaySum7d": 10},
        },
    }


class HeatQueueTest(unittest.TestCase):
    def test_priority_and_evergreen_oldest_first(self) -> None:
        characters = [
            _character("e:old", "EVERGREEN"),
            _character("bgm:low", "LIVE", kind="ANIME", game="anime", work="低热", heat=10, subject=2),
            _character("g:live", "LIVE"),
            _character("bgm:a", "LEAKED", kind="ANIME", game="anime", work="高热", heat=90, subject=1),
            _character("g:leak", "LEAKED"),
            _character("e:m", "EVERGREEN"),
            _character("e:a", "EVERGREEN"),
            _character("e:new", "EVERGREEN"),
            _character("bgm:b", "LIVE", kind="ANIME", game="anime", work="高热", heat=90, subject=1),
            _character("bgm:mid", "LEAKED", kind="ANIME", game="anime", work="中热", heat=50, subject=3),
        ]
        previous = {
            "e:old": {"heatFetchedAt": "2026-10-01T00:00:00Z"},
            "e:new": {"heatFetchedAt": "2026-10-10T00:00:00Z"},
            "e:a": {"heatFetchedAt": "2026-10-05T00:00:00Z"},
            "e:m": {"heatFetchedAt": "2026-10-05T00:00:00Z"},
        }
        plan = build_queue(characters, previous, daily_cap=DAILY_CAP)
        self.assertEqual(
            ["g:leak", "g:live", "bgm:a", "bgm:b", "bgm:mid", "bgm:low", "e:old", "e:a", "e:m", "e:new"],
            [row["key"] for row in plan["selected"]],
        )
        self.assertEqual([], plan["deferred"])

    def test_same_work_heat_uses_catalog_order_of_the_work(self) -> None:
        characters = [
            _character("bgm:late", "LIVE", kind="ANIME", game="anime", work="后", heat=40, subject=2),
            _character("bgm:early", "LIVE", kind="ANIME", game="anime", work="先", heat=40, subject=1),
        ]
        plan = build_queue(characters, {}, daily_cap=10)
        self.assertEqual(["bgm:late", "bgm:early"], [row["key"] for row in plan["selected"]])

    def test_cap_truncates_and_counts_tiers(self) -> None:
        characters = [_character(f"g:{i}", "LEAKED") for i in range(5)]
        characters.extend(_character(f"e:{i:03d}", "EVERGREEN") for i in range(200))
        plan = build_queue(characters, {}, daily_cap=120)
        self.assertEqual(120, len(plan["selected"]))
        self.assertEqual(85, len(plan["deferred"]))
        self.assertEqual({"queued": 5, "deferred": 0}, plan["stats"]["game-leaked"])
        self.assertEqual({"queued": 115, "deferred": 85}, plan["stats"]["evergreen"])
        self.assertEqual([f"e:{i:03d}" for i in range(115)], [row["key"] for row in plan["selected"][5:]])
        self.assertEqual([f"e:{i:03d}" for i in range(115, 200)], [row["key"] for row in plan["deferred"]])

    def test_never_fetched_evergreen_comes_before_stale_rows(self) -> None:
        characters = [
            _character("e:b", "EVERGREEN"),
            _character("e:a", "EVERGREEN"),
        ]
        previous = {"e:b": {"heatFetchedAt": "2026-10-01T00:00:00Z"}}
        plan = build_queue(characters, previous, daily_cap=10)
        self.assertEqual(["e:a", "e:b"], [row["key"] for row in plan["selected"]])


class TimeBudgetTest(unittest.TestCase):
    def test_take_within_budget_stops_before_the_next_item(self) -> None:
        clock = {"n": 0}

        def expired() -> bool:
            return clock["n"] >= 2

        def consume(item: int) -> None:
            clock["n"] += 1
            self.assertLess(item, 3)

        taken, rest = take_within_budget([1, 2, 3, 4], expired, consume)
        self.assertEqual([1, 2], taken)
        self.assertEqual([3, 4], rest)

    def test_budget_exhaustion_carries_the_rest(self) -> None:
        clock = {"now": 0.0}

        def monotonic() -> float:
            return clock["now"]

        def fetch_bangumi(character: dict, name: str, fetched_at: str) -> dict:
            clock["now"] += 100
            return {"collects": 10, "subjectCollect": None, "airing": False, "fetchedAt": fetched_at}

        def fetch_bilibili(character: dict, name: str, fetched_at: str) -> dict:
            clock["now"] += 100
            return {"recentVideos7d": 4, "recentPlaySum7d": 100, "fetchedAt": fetched_at}

        names = ["常青甲", "常青乙", "常青丙"]
        characters = [_character(f"e:{index}", "EVERGREEN", name=name) for index, name in enumerate(names)]
        previous_characters = [
            _live(name, 17 + index, "2026-10-17T04:00:00Z", key=f"e:{index}")
            for index, name in enumerate(names)
        ]
        previous_characters.append(_live("已移出", 99, "2026-10-17T04:00:00Z", key="gone:1"))
        previous = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-18T04:00:00Z",
            "characters": previous_characters,
            "history": [
                {
                    "capturedAt": "2026-10-01T04:00:00Z",
                    "characters": [_flat("常青甲", 1), _flat("已移出", 2)],
                },
                {"capturedAt": "2020-01-01T04:00:00Z", "characters": [_flat("常青甲", 9)]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog_path = root / "catalog.json"
            heat_path = root / "heat.json"
            catalog_path.write_text(
                json.dumps({"characters": characters}, ensure_ascii=False),
                encoding="utf-8",
            )
            heat_path.write_text(json.dumps(previous, ensure_ascii=False), encoding="utf-8")
            code = run(
                heat_path,
                False,
                catalog_path,
                daily_cap=10,
                time_budget_seconds=350,
                monotonic=monotonic,
                fetch_bangumi=fetch_bangumi,
                fetch_bilibili=fetch_bilibili,
                weibo_fetcher=lambda names: {"hotHits": [], "hotCount": 0},
            )
            self.assertEqual(0, code)
            feed = json.loads(heat_path.read_text(encoding="utf-8"))
        by_key = {row["key"]: row for row in feed["characters"]}
        self.assertEqual({"e:0", "e:1", "e:2"}, set(by_key))
        self.assertEqual(feed["generatedAt"], by_key["e:0"]["heatFetchedAt"])
        self.assertEqual("2026-10-17T04:00:00Z", by_key["e:1"]["heatFetchedAt"])
        self.assertEqual(18, by_key["e:1"]["heatScore"])
        self.assertEqual("2026-10-17T04:00:00Z", by_key["e:2"]["heatFetchedAt"])
        self.assertEqual("partial", feed["sources"]["bilibili"]["status"])
        self.assertEqual("time budget exhausted", feed["sources"]["bilibili"]["error"])
        names_in_history = {
            row["name"]
            for entry in feed["history"]
            for row in entry["characters"]
        }
        self.assertNotIn("已移出", names_in_history)
        self.assertNotIn("2020-01-01", [entry["capturedAt"][:10] for entry in feed["history"]])
        self.assertIn("常青甲", names_in_history)


class HeatHistoryTest(unittest.TestCase):
    def test_history_keeps_only_rows_fetched_that_run(self) -> None:
        previous = {
            "generatedAt": "2026-10-13T04:00:00Z",
            "characters": [
                _live("新鲜", 3, "2026-10-13T04:00:00Z", key="a"),
                _live("沿用", 1, "2026-10-01T00:00:00Z", key="b"),
            ],
            "history": [],
        }
        history = build_history(previous, "2026-10-14T06:00:00Z", allowed_names={"新鲜", "沿用"})
        self.assertEqual(["新鲜"], [row["name"] for row in history[0]["characters"]])
        self.assertEqual(
            {
                "name",
                "bangumiCollects",
                "bangumiSubjectCollect",
                "bilibiliRecentVideos7d",
                "bilibiliRecentPlaySum7d",
                "heatScore",
            },
            set(history[0]["characters"][0]),
        )

    def test_history_window_is_thirty_beijing_days_and_drops_removed_names(self) -> None:
        anchor = date(2026, 10, 19)
        keep_day = (anchor - timedelta(days=HISTORY_KEEP)).isoformat()
        drop_day = (anchor - timedelta(days=HISTORY_KEEP + 1)).isoformat()
        previous = {
            "generatedAt": "2026-10-19T01:00:00Z",
            "characters": [],
            "history": [
                {"capturedAt": f"{keep_day}T04:00:00Z", "characters": [_flat("甲", 1), _flat("已移出", 8)]},
                {"capturedAt": f"{drop_day}T04:00:00Z", "characters": [_flat("甲", 2)]},
                {"capturedAt": "2026-10-01T04:00:00Z", "characters": [_flat("甲", 3)]},
            ],
        }
        history = build_history(previous, "2026-10-19T02:00:00Z", allowed_names={"甲"})
        days = [beijing_day(entry["capturedAt"]) for entry in history]
        self.assertIn(keep_day, days)
        self.assertNotIn(drop_day, days)
        self.assertIn("2026-10-01", days)
        for entry in history:
            self.assertEqual(["甲"], [row["name"] for row in entry["characters"]])

    def test_same_day_dedupe_keeps_the_later_snapshot(self) -> None:
        previous = {
            "generatedAt": "2026-10-13T04:00:00Z",
            "characters": [
                {
                    "name": "锁暝",
                    "heatScore": 2,
                    "metrics": {
                        "bangumi": {"collects": 1, "subjectCollect": None},
                        "bilibili": {"recentVideos7d": 2, "recentPlaySum7d": 10},
                    },
                }
            ],
            "history": [
                {"capturedAt": "2026-10-13T01:00:00Z", "characters": [_flat("锁暝", 1)]},
                {"capturedAt": "2026-10-12T03:00:00Z", "characters": [_flat("锁暝", 50)]},
            ],
        }
        history = build_history(previous, "2026-10-13T06:00:00Z")
        self.assertEqual(["2026-10-13", "2026-10-12"], [beijing_day(entry["capturedAt"]) for entry in history])
        self.assertEqual("2026-10-13T04:00:00Z", history[0]["capturedAt"])
        self.assertEqual(2, history[0]["characters"][0]["bilibiliRecentVideos7d"])

    def test_same_day_runs_merge_rows_fetched_in_either_run(self) -> None:
        previous = {
            "generatedAt": "2026-10-13T04:00:00Z",
            "characters": [
                _live("锁暝", 2, "2026-10-13T04:00:00Z", key="a"),
                _live("常青乙", 7, "2026-10-13T04:00:00Z", key="c"),
            ],
            "history": [
                {"capturedAt": "2026-10-13T01:00:00Z", "characters": [_flat("锁暝", 1), _flat("常青甲", 5)]},
            ],
        }
        history = build_history(previous, "2026-10-14T06:00:00Z")
        self.assertEqual(1, len(history))
        self.assertEqual("2026-10-13T04:00:00Z", history[0]["capturedAt"])
        rows = {row["name"]: row["bilibiliRecentVideos7d"] for row in history[0]["characters"]}
        self.assertEqual({"锁暝": 2, "常青甲": 5, "常青乙": 7}, rows)

    def test_growth_baseline_uses_real_fetch_time(self) -> None:
        stale = _live("锁暝", 100, "2026-10-01T00:00:00Z", key="a")
        previous = {
            "generatedAt": "2026-10-13T04:00:00Z",
            "characters": [stale],
            "history": [{"capturedAt": "2026-10-12T03:00:00Z", "characters": [_flat("锁暝", 50)]}],
        }
        baseline = previous_metrics(previous, "锁暝", "2026-10-14T06:00:00Z")
        self.assertIsNotNone(baseline)
        assert baseline is not None
        self.assertEqual(50, baseline["bilibiliRecentVideos7d"])

        same_day_file = {
            "generatedAt": "2026-10-13T04:00:00Z",
            "characters": [_live("锁暝", 9, "2026-10-12T03:00:00Z", key="a")],
            "history": [],
        }
        carried = previous_metrics(same_day_file, "锁暝", "2026-10-13T06:00:00Z")
        self.assertIsNotNone(carried)
        assert carried is not None
        self.assertEqual(9, carried["bilibiliRecentVideos7d"])


if __name__ == "__main__":
    unittest.main()
