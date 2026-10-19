#!/usr/bin/env python3
"""Fetch character heat signals and write data/heat.json.

Sources are isolated: one failure is recorded and the rest continue.
Stdlib only. Run from repo root: python scripts/fetch_heat.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WATCHLIST_PATH = Path(__file__).resolve().parent / "watchlist.json"
CATALOG_PATH = ROOT / "catalog.json"
DEFAULT_OUT = ROOT / "heat.json"
ASSETS_OUT = ROOT / "app" / "src" / "main" / "assets" / "heat.json"

SCHEMA_VERSION = 1
SCORE_METHOD = "velocity_v1"
USER_AGENT = "WigRadar/0.1.0 (mikure; GitHubActions; cos wig selection tool)"
BILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
RECENT_DAYS = 7
HISTORY_KEEP = 30
# Radar candidates and evergreen rotation share this cap.
DAILY_CAP = 120
# Shared by the Bangumi and Bilibili stages, measured from process start.
# Leave headroom so one in-flight character still ends the run inside 15 minutes.
TIME_BUDGET_SECONDS = 840
TIER_GAME_LEAKED = "game-leaked"
TIER_GAME_LIVE = "game-live"
TIER_ANIME = "anime"
TIER_EVERGREEN = "evergreen"
TIER_ORDER = (TIER_GAME_LEAKED, TIER_GAME_LIVE, TIER_ANIME, TIER_EVERGREEN)
TIER_LABELS = {
    TIER_GAME_LEAKED: "游戏LEAKED",
    TIER_GAME_LIVE: "游戏LIVE",
    TIER_ANIME: "番剧",
    TIER_EVERGREEN: "常青",
}

# Bilibili WBI mixin table (public algorithm).
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

BANGUMI_TYPE_ANIME = 2
BANGUMI_TYPE_GAME = 4

SEED_HEAT = {
    "锁暝": 58,
    "洛克茜": 62,
    "心月狐": 74,
    "Lucy": 68,
    "沃雅妮莎": 78,
    "猫猫": 92,
}


class Http:
    def __init__(self, user_agent: str, min_interval: float, extra_headers: dict[str, str] | None = None):
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.extra_headers = extra_headers or {}
        self._last = 0.0

    def _sleep(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)

    def request(
        self,
        url: str,
        *,
        data: Any = None,
        headers: dict[str, str] | None = None,
        method: str | None = None,
        retries: int = 3,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(retries):
            self._sleep()
            hdrs = {
                "User-Agent": self.user_agent,
                "Accept": "application/json,text/plain,*/*",
            }
            hdrs.update(self.extra_headers)
            if headers:
                hdrs.update(headers)
            body = None
            req_method = method
            if data is not None:
                if isinstance(data, (dict, list)):
                    body = json.dumps(data).encode("utf-8")
                    hdrs.setdefault("Content-Type", "application/json")
                elif isinstance(data, bytes):
                    body = data
                else:
                    body = str(data).encode("utf-8")
                req_method = req_method or "POST"
            req = urllib.request.Request(url, data=body, headers=hdrs, method=req_method)
            try:
                with urllib.request.urlopen(req, timeout=25) as resp:
                    raw = resp.read()
                    self._last = time.monotonic()
                    text = raw.decode("utf-8", errors="replace")
                    if not text:
                        return None
                    stripped = text.lstrip()
                    if stripped.startswith("{") or stripped.startswith("["):
                        return json.loads(text)
                    return text
            except urllib.error.HTTPError as e:
                self._last = time.monotonic()
                last_error = e
                retryable = e.code in {408, 409, 412, 429, 500, 502, 503, 504}
                if not retryable or attempt == retries - 1:
                    snippet = e.read()[:300].decode("utf-8", errors="replace")
                    raise RuntimeError(f"HTTP {e.code} {url} {snippet}") from e
                time.sleep(1.5 * (2**attempt) + random.random())
            except Exception as e:
                self._last = time.monotonic()
                last_error = e
                if attempt == retries - 1:
                    raise
                time.sleep(1.5 * (2**attempt) + random.random())
        raise RuntimeError(str(last_error) if last_error else "request failed")


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


BEIJING = timezone(timedelta(hours=8))


def parse_instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def beijing_day(value: str) -> str:
    """Calendar date in Beijing (UTC+8). Growth and history dedupe use this, not the UTC date."""
    return parse_instant(value).astimezone(BEIJING).date().isoformat()


def log_score(value: float, ref: float) -> float:
    value = max(0.0, float(value))
    ref = max(1.0, float(ref))
    return max(0.0, min(100.0, 100.0 * math.log1p(value) / math.log1p(ref)))


def growth_score(curr: float | None, prev: float | None, floor: float) -> float | None:
    if curr is None or prev is None:
        return None
    base = max(float(prev), floor)
    delta = (float(curr) - float(prev)) / base
    return max(0.0, min(100.0, 40.0 + 50.0 * math.tanh(delta)))


def weighted_blend(parts: dict[str, float | None], weights: dict[str, float]) -> tuple[int, dict[str, int | None]]:
    used: list[tuple[str, float, float]] = []
    for key, weight in weights.items():
        value = parts.get(key)
        if value is None:
            continue
        used.append((key, float(value), weight))
    components = {key: (None if parts.get(key) is None else int(round(parts[key]))) for key in weights}
    if not used:
        return 0, components
    total_w = sum(weight for _, _, weight in used)
    raw = sum(value * weight for _, value, weight in used) / total_w
    return int(round(max(0.0, min(100.0, raw)))), components


def load_watchlist() -> list[dict[str, Any]]:
    payload = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    return list(payload["characters"])


GAME_SYNONYMS = {
    "genshin": ["原神", "Genshin"],
    "hsr": ["崩坏：星穹铁道", "星穹铁道", "崩坏星穹铁道"],
    "zzz": ["绝区零", "Zenless Zone Zero", "绝区零"],
    "wuwa": ["鸣潮", "Wuthering Waves"],
    "arknights": ["明日方舟", "Arknights", "明日方舟"],
    "bluearchive": ["碧蓝档案", "Blue Archive"],
    "anime": [],
}


def load_catalog(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"missing {path}; run scripts/fetch_catalog.py first")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("characters"), list) or not payload["characters"]:
        raise SystemExit(f"{path} has no characters")
    return payload


def character_tier(character: dict[str, Any]) -> str:
    status = character.get("status")
    is_anime = character.get("kind") == "ANIME" or character.get("game") == "anime"
    if is_anime and status in {"LEAKED", "LIVE"}:
        return TIER_ANIME
    if status == "LEAKED":
        return TIER_GAME_LEAKED
    if status == "LIVE":
        return TIER_GAME_LIVE
    return TIER_EVERGREEN


def _work_heat(character: dict[str, Any]) -> int:
    try:
        return int(character.get("workHeat") or 0)
    except (TypeError, ValueError):
        return 0


def _anime_ordered(indexed: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    groups: dict[Any, list[tuple[int, dict[str, Any]]]] = {}
    for index, row in indexed:
        subject_id = row.get("bangumiSubjectId")
        group_id: Any = subject_id if isinstance(subject_id, int) else ("work", str(row.get("work") or ""))
        groups.setdefault(group_id, []).append((index, row))

    def group_key(group_id: Any) -> tuple[int, int]:
        members = groups[group_id]
        heat = max(_work_heat(row) for _, row in members)
        return (-heat, members[0][0])

    ordered: list[dict[str, Any]] = []
    for group_id in sorted(groups, key=group_key):
        members = sorted(groups[group_id], key=lambda pair: pair[0])
        ordered.extend(row for _, row in members)
    return ordered


def _evergreen_ordered(
    indexed: list[tuple[int, dict[str, Any]]],
    previous_by_key: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    def sort_key(pair: tuple[int, dict[str, Any]]) -> tuple[int, str, str]:
        _index, row = pair
        previous = previous_by_key.get(str(row.get("key") or ""))
        fetched = ""
        if isinstance(previous, dict) and isinstance(previous.get("heatFetchedAt"), str):
            fetched = previous["heatFetchedAt"]
        missing = 0 if not fetched else 1
        return (missing, fetched, str(row.get("key") or ""))

    return [row for _, row in sorted(indexed, key=sort_key)]


def build_queue(
    characters: list[dict[str, Any]],
    previous_by_key: dict[str, dict[str, Any]] | None = None,
    *,
    daily_cap: int = DAILY_CAP,
) -> dict[str, Any]:
    """Order today's fetches. Evergreen slots are whatever the cap has left."""
    previous_by_key = previous_by_key or {}
    buckets: dict[str, list[tuple[int, dict[str, Any]]]] = {tier: [] for tier in TIER_ORDER}
    for index, row in enumerate(characters):
        if not isinstance(row, dict) or not row.get("key"):
            continue
        buckets[character_tier(row)].append((index, row))
    ordered: list[tuple[str, dict[str, Any]]] = []
    ordered.extend((TIER_GAME_LEAKED, row) for _, row in buckets[TIER_GAME_LEAKED])
    ordered.extend((TIER_GAME_LIVE, row) for _, row in buckets[TIER_GAME_LIVE])
    ordered.extend((TIER_ANIME, row) for row in _anime_ordered(buckets[TIER_ANIME]))
    ordered.extend((TIER_EVERGREEN, row) for row in _evergreen_ordered(buckets[TIER_EVERGREEN], previous_by_key))
    cap = max(0, int(daily_cap))
    chosen = ordered[:cap]
    delayed = ordered[cap:]
    stats = {tier: {"queued": 0, "deferred": 0} for tier in TIER_ORDER}
    for tier, _row in chosen:
        stats[tier]["queued"] += 1
    for tier, _row in delayed:
        stats[tier]["deferred"] += 1
    return {
        "selected": [row for _tier, row in chosen],
        "deferred": [row for _tier, row in delayed],
        "stats": stats,
    }


def take_within_budget(items: list, expired, consume) -> tuple[list, list]:
    """Run consume until the shared budget is gone. Do not start the next item after that."""
    taken: list = []
    for index, item in enumerate(items):
        if expired():
            return taken, items[index:]
        consume(item)
        taken.append(item)
    return taken, []


def work_compatible(character: dict[str, Any], previous_row: dict[str, Any]) -> bool:
    """Legacy heat rows are matched by name only. An alias may be reused across works."""
    new = short_work(str(character.get("work") or ""))
    old = short_work(str(previous_row.get("work") or ""))
    if not new or not old:
        return False
    return new == old or new in old or old in new


def display_name(character: dict[str, Any], previous_by_name: dict[str, dict[str, Any]]) -> str:
    """Keep a legacy heat name only when the alias belongs to the same work.

    心月狐 is the name already shipped for 鸣潮的「心」. Lucy on a different
    game is not that Edgerunners row.
    """
    for alias in character.get("aliases") or []:
        previous = previous_by_name.get(str(alias))
        if previous and work_compatible(character, previous):
            return str(alias)
    return str(character.get("name") or "")


def short_work(work: str) -> str:
    text = (work or "").strip()
    if not text:
        return ""
    return text.split()[0]


def as_watch_item(character: dict[str, Any], name: str) -> dict[str, Any]:
    work = short_work(str(character.get("work") or ""))
    synonyms = list(GAME_SYNONYMS.get(str(character.get("game") or ""), []))
    if work and work not in synonyms:
        synonyms.insert(0, work)
    keyword = f"{name} {work}".strip() if work and work not in name else name
    search_names = [name]
    for alias in character.get("aliases") or []:
        if alias and alias not in search_names:
            search_names.append(str(alias))
        if len(search_names) >= 3:
            break
    return {
        "name": name,
        "kind": character.get("kind") or "GAME",
        "searchNames": search_names,
        "workSynonyms": synonyms,
        "bilibiliKeywords": [keyword],
    }


def load_previous(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"previous snapshot unreadable: {e}", file=sys.stderr)
        return None


def source_run(status: str, fetched_at: str | None = None, error: str | None = None) -> dict[str, Any]:
    return {"status": status, "fetchedAt": fetched_at, "error": error}


class BangumiClient:
    def __init__(self, http: Http):
        self.http = http
        self.calendar_ids: set[int] = set()
        self.subject_cache: dict[int, dict[str, Any]] = {}

    def load_calendar(self) -> None:
        payload = self.http.request("https://api.bgm.tv/calendar")
        if not isinstance(payload, list):
            return
        for day in payload:
            for item in day.get("items") or []:
                sid = item.get("id")
                if isinstance(sid, int):
                    self.calendar_ids.add(sid)

    def search(self, keyword: str) -> list[dict[str, Any]]:
        payload = self.http.request(
            "https://api.bgm.tv/v0/search/characters?limit=12&offset=0",
            data={"keyword": keyword},
            method="POST",
        )
        if not isinstance(payload, dict):
            return []
        data = payload.get("data") or []
        return data if isinstance(data, list) else []

    def subjects(self, character_id: int) -> list[dict[str, Any]]:
        payload = self.http.request(f"https://api.bgm.tv/v0/characters/{character_id}/subjects")
        return payload if isinstance(payload, list) else []

    def subject(self, subject_id: int) -> dict[str, Any] | None:
        cached = self.subject_cache.get(subject_id)
        if cached is not None:
            return cached
        payload = self.http.request(f"https://api.bgm.tv/v0/subjects/{subject_id}")
        if isinstance(payload, dict):
            self.subject_cache[subject_id] = payload
            return payload
        return None

    def character(self, character_id: int) -> dict[str, Any] | None:
        payload = self.http.request(f"https://api.bgm.tv/v0/characters/{character_id}")
        return payload if isinstance(payload, dict) else None


class BilibiliClient:
    def __init__(self, http: Http):
        self.http = http
        self.mixin_key: str | None = None
        http.extra_headers.setdefault("Referer", "https://search.bilibili.com/")
        http.extra_headers.setdefault("Origin", "https://www.bilibili.com")
        http.extra_headers.setdefault("Cookie", f"buvid3={uuid.uuid4()}; buvid4={uuid.uuid4()}")

    def ensure_wbi(self) -> None:
        if self.mixin_key:
            return
        payload = self.http.request("https://api.bilibili.com/x/web-interface/nav")
        if not isinstance(payload, dict):
            return
        wbi = ((payload.get("data") or {}).get("wbi_img") or {})
        img = str(wbi.get("img_url") or "")
        sub = str(wbi.get("sub_url") or "")
        img_key = Path(urllib.parse.urlparse(img).path).stem
        sub_key = Path(urllib.parse.urlparse(sub).path).stem
        orig = img_key + sub_key
        if len(orig) < 32:
            return
        self.mixin_key = "".join(orig[i] for i in MIXIN_KEY_ENC_TAB if i < len(orig))[:32]

    def sign(self, params: dict[str, Any]) -> dict[str, Any]:
        signed = dict(params)
        if not self.mixin_key:
            return signed
        signed["wts"] = int(time.time())
        cleaned = {}
        for key, value in signed.items():
            text = str(value)
            cleaned[key] = re.sub(r"[!'()*]", "", text)
        ordered = dict(sorted(cleaned.items()))
        query = urllib.parse.urlencode(ordered)
        ordered["w_rid"] = hashlib.md5((query + self.mixin_key).encode("utf-8")).hexdigest()
        return ordered

    def search_videos(self, keyword: str, pages: int = 2) -> dict[str, Any]:
        self.ensure_wbi()
        videos: list[dict[str, Any]] = []
        result_count = 0
        for page in range(1, pages + 1):
            params = {
                "search_type": "video",
                "keyword": keyword,
                "page": page,
                "page_size": 50,
                "order": "pubdate",
            }
            signed = self.sign(params)
            query = urllib.parse.urlencode(signed)
            url = f"https://api.bilibili.com/x/web-interface/wbi/search/type?{query}"
            payload = self.http.request(url, method="GET")
            if not isinstance(payload, dict):
                raise RuntimeError(f"bilibili non-json for {keyword}")
            if payload.get("code") not in (0, None):
                raise RuntimeError(f"bilibili code={payload.get('code')} {payload.get('message')} keyword={keyword}")
            data = payload.get("data") or {}
            result_count = int(data.get("numResults") or result_count or 0)
            chunk = data.get("result") or []
            if not isinstance(chunk, list):
                break
            videos.extend(item for item in chunk if isinstance(item, dict))
            if len(chunk) < 10:
                break
        return {"resultCount": result_count, "videos": videos}


def season_tokens(hint: int | None) -> list[str]:
    if not hint:
        return []
    mapping = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五"}
    cn = mapping.get(hint, str(hint))
    return [
        f"第{hint}期",
        f"第{hint}季",
        f"第{cn}季",
        f"第{cn}期",
        f"S{hint}",
        f"Season {hint}",
    ]


def subject_matches_work(subject: dict[str, Any], synonyms: list[str]) -> bool:
    hay = f"{subject.get('name') or ''} {subject.get('name_cn') or ''}"
    return any(token and token.lower() in hay.lower() for token in synonyms)


def pick_anime_subject(subjects: list[dict[str, Any]], synonyms: list[str], season_hint: int | None) -> dict[str, Any] | None:
    anime = [
        s for s in subjects
        if s.get("type") == BANGUMI_TYPE_ANIME and subject_matches_work(s, synonyms)
    ]
    if not anime:
        anime = [s for s in subjects if s.get("type") == BANGUMI_TYPE_ANIME]
    if not anime:
        return None
    tokens = season_tokens(season_hint)
    if tokens:
        seasonal = [
            s for s in anime
            if any(token.lower() in f"{s.get('name') or ''} {s.get('name_cn') or ''}".lower() for token in tokens)
        ]
        if seasonal:
            anime = seasonal
    leads = [s for s in anime if "主角" in str(s.get("staff") or "")]
    pool = leads or anime
    return pool[-1]


def fetch_bangumi_character(
    client: BangumiClient,
    item: dict[str, Any],
    fetched_at: str,
) -> dict[str, Any] | None:
    synonyms = list(item.get("workSynonyms") or [])
    kind = item.get("kind") or "GAME"
    wanted_type = BANGUMI_TYPE_ANIME if kind == "ANIME" else BANGUMI_TYPE_GAME
    names = list(item.get("searchNames") or [item["name"]])
    for keyword in names:
        hits = client.search(keyword)
        for hit in hits[:8]:
            cid = hit.get("id")
            if not isinstance(cid, int):
                continue
            subjects = client.subjects(cid)
            matched = [
                s for s in subjects
                if subject_matches_work(s, synonyms) and "客串" not in str(s.get("staff") or "")
            ]
            if not matched:
                continue
            if wanted_type and not any(s.get("type") == wanted_type for s in matched) and not any(
                s.get("type") in {BANGUMI_TYPE_ANIME, BANGUMI_TYPE_GAME} for s in matched
            ):
                continue
            detail = client.character(cid) or hit
            stat = detail.get("stat") or hit.get("stat") or {}
            subject_row = None
            subject_collect = None
            subject_id = None
            airing = False
            if kind == "ANIME":
                subject_row = pick_anime_subject(matched, synonyms, item.get("seasonHint"))
            else:
                games = [s for s in matched if s.get("type") == BANGUMI_TYPE_GAME]
                subject_row = games[0] if games else matched[0]
            if subject_row:
                subject_id = subject_row.get("id")
                if isinstance(subject_id, int):
                    airing = subject_id in client.calendar_ids
                    info = client.subject(subject_id) if kind == "ANIME" else None
                    if info:
                        col = info.get("collection") or {}
                        subject_collect = int(col.get("wish") or 0) + int(col.get("doing") or 0) + int(col.get("collect") or 0)
            return {
                "characterId": cid,
                "characterName": detail.get("name") or hit.get("name"),
                "collects": int((stat or {}).get("collects") or 0),
                "comments": int((stat or {}).get("comments") or 0),
                "subjectId": subject_id,
                "subjectName": (subject_row or {}).get("name_cn") or (subject_row or {}).get("name"),
                "subjectCollect": subject_collect,
                "airing": airing,
                "matchedBy": keyword,
                "fetchedAt": fetched_at,
            }
    return None


def summarize_bili_videos(videos: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    start = now - timedelta(days=RECENT_DAYS)
    latest_ok = now + timedelta(hours=2)
    recent = []
    for video in videos:
        ts = video.get("pubdate") or video.get("senddate") or 0
        try:
            pub = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        if start <= pub <= latest_ok:
            recent.append(video)
    play_sum = 0
    for video in recent:
        play = video.get("play") or 0
        try:
            play_sum += int(play)
        except (TypeError, ValueError):
            continue
    oldest_recent = True
    if videos:
        try:
            oldest_ts = min(int(v.get("pubdate") or v.get("senddate") or 0) for v in videos)
            oldest = datetime.fromtimestamp(oldest_ts, tz=timezone.utc)
            oldest_recent = oldest >= start
        except (TypeError, ValueError, OSError):
            oldest_recent = False
    return {
        "recentVideos7d": len(recent),
        "recentPlaySum7d": play_sum,
        "sampledVideos": len(videos),
        "windowSaturated": bool(videos) and oldest_recent and len(videos) >= 40,
    }


def fetch_bangumi_direct(
    client: BangumiClient,
    character_id: int,
    subject_id: int | None,
    kind: str,
    fetched_at: str,
) -> dict[str, Any] | None:
    detail = client.character(character_id) or {}
    stat = detail.get("stat") or {}
    subject_collect = None
    subject_name = None
    airing = False
    if isinstance(subject_id, int):
        airing = subject_id in client.calendar_ids
        if kind == "ANIME":
            info = client.subject(subject_id)
            if info:
                subject_name = info.get("name_cn") or info.get("name")
                col = info.get("collection") or {}
                subject_collect = int(col.get("wish") or 0) + int(col.get("doing") or 0) + int(col.get("collect") or 0)
    return {
        "characterId": character_id,
        "characterName": detail.get("name"),
        "collects": int((stat or {}).get("collects") or 0),
        "comments": int((stat or {}).get("comments") or 0),
        "subjectId": subject_id,
        "subjectName": subject_name,
        "subjectCollect": subject_collect,
        "airing": airing,
        "matchedBy": "catalog",
        "fetchedAt": fetched_at,
    }


def fetch_bilibili_character(
    client: BilibiliClient,
    item: dict[str, Any],
    now: datetime,
    fetched_at: str,
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    keywords = list(item.get("bilibiliKeywords") or [item["name"]])
    for keyword in keywords:
        payload = client.search_videos(keyword, pages=1)
        summary = summarize_bili_videos(payload["videos"], now)
        row = {
            "keyword": keyword,
            "resultCount": payload["resultCount"],
            "resultCountCapped": payload["resultCount"] >= 1000,
            **summary,
            "fetchedAt": fetched_at,
        }
        if best is None:
            best = row
        else:
            better = (
                row["recentVideos7d"],
                row["recentPlaySum7d"],
            ) > (best["recentVideos7d"], best["recentPlaySum7d"])
            if better:
                best = row
        if row["recentVideos7d"] >= 8 and not row["windowSaturated"]:
            break
        if row["recentVideos7d"] >= 8:
            break
    return best


def bangumi_component(kind: str, metrics: dict[str, Any] | None) -> float | None:
    if not metrics:
        return None
    collects = metrics.get("collects") or 0
    subject_collect = metrics.get("subjectCollect")
    if kind == "ANIME":
        subject_score = log_score(subject_collect or 0, 50_000)
        char_score = log_score(collects, 8_000)
        score = 0.7 * subject_score + 0.3 * char_score
        if metrics.get("airing"):
            score = min(100.0, score + 4.0)
        return score
    return log_score(collects, 2_000)


def bili_component(metrics: dict[str, Any] | None) -> float | None:
    if not metrics:
        return None
    videos = metrics.get("recentVideos7d") or 0
    plays = metrics.get("recentPlaySum7d") or 0
    if metrics.get("windowSaturated"):
        videos = max(videos, 80)
    video_score = log_score(videos, 180)
    play_score = log_score(plays, 3_000_000)
    return 0.72 * video_score + 0.28 * play_score


def flatten_character(ch: dict[str, Any]) -> dict[str, Any]:
    bangumi = (ch.get("metrics") or {}).get("bangumi") or {}
    bili = (ch.get("metrics") or {}).get("bilibili") or {}
    if "metrics" not in ch and "bilibiliRecentVideos7d" in ch:
        return ch
    return {
        "name": ch.get("name"),
        "bangumiCollects": bangumi.get("collects"),
        "bangumiSubjectCollect": bangumi.get("subjectCollect"),
        "bilibiliRecentVideos7d": bili.get("recentVideos7d"),
        "bilibiliRecentPlaySum7d": bili.get("recentPlaySum7d"),
        "heatScore": ch.get("heatScore"),
    }


def previous_metrics(previous: dict[str, Any] | None, name: str, generated_at: str) -> dict[str, Any] | None:
    """Baseline for day-over-day growth.

    A second run on the same Beijing date must not be compared with the earlier
    run that day — a half-hour delta is ~0 and the score collapses. Use the
    newest snapshot from a previous Beijing date, or nothing.
    """
    if not previous:
        return None
    today = beijing_day(generated_at)
    best_at = ""
    best: dict[str, Any] | None = None

    def consider(captured_at: str | None, row: dict[str, Any] | None) -> None:
        nonlocal best_at, best
        if not captured_at or not row or row.get("name") != name:
            return
        if beijing_day(captured_at) >= today:
            return
        if captured_at > best_at:
            best_at = captured_at
            best = row

    # Carried rows keep the day they were actually fetched. The file's generatedAt
    # is only a fallback for snapshots that predate heatFetchedAt.
    prev_at = previous.get("generatedAt") if isinstance(previous.get("generatedAt"), str) else None
    for ch in previous.get("characters") or []:
        if not isinstance(ch, dict):
            continue
        captured = ch.get("heatFetchedAt")
        if not isinstance(captured, str) or not captured:
            captured = prev_at
        consider(captured, flatten_character(ch))
    for entry in previous.get("history") or []:
        if not isinstance(entry, dict):
            continue
        captured_at = entry.get("capturedAt")
        for row in entry.get("characters") or []:
            if isinstance(row, dict):
                consider(captured_at if isinstance(captured_at, str) else None, row)
    return best


def _filter_history_rows(rows: list, allowed_names: set[str] | None) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if allowed_names is not None and row.get("name") not in allowed_names:
            continue
        kept.append(row)
    return kept


def build_history(
    previous: dict[str, Any] | None,
    generated_at: str,
    allowed_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if previous:
        prev_at = previous.get("generatedAt")
        if isinstance(prev_at, str) and prev_at and prev_at != generated_at:
            fresh_rows: list[dict[str, Any]] = []
            for ch in previous.get("characters") or []:
                if not isinstance(ch, dict):
                    continue
                # Rows without heatFetchedAt are legacy snapshots: they belong to generatedAt.
                # Carried rows keep an older heatFetchedAt and must not be archived again.
                fetched_at = ch.get("heatFetchedAt")
                if not isinstance(fetched_at, str) or not fetched_at:
                    fetched_at = prev_at
                if fetched_at != prev_at:
                    continue
                fresh_rows.append(flatten_character(ch))
            fresh_rows = _filter_history_rows(fresh_rows, allowed_names)
            if fresh_rows:
                entries.append({"capturedAt": prev_at, "characters": fresh_rows})
        for entry in previous.get("history") or []:
            if not isinstance(entry, dict):
                continue
            rows = _filter_history_rows(list(entry.get("characters") or []), allowed_names)
            if not rows:
                continue
            cloned = dict(entry)
            cloned["characters"] = rows
            entries.append(cloned)
    # One row per Beijing date. A delayed second cron run the same day replaces
    # the earlier snapshot instead of stacking a near-zero growth step.
    by_day: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        captured_at = entry.get("capturedAt")
        if not isinstance(captured_at, str) or not captured_at:
            continue
        day = beijing_day(captured_at)
        current = by_day.get(day)
        if current is None or captured_at > str(current.get("capturedAt") or ""):
            by_day[day] = entry
    ordered = sorted(by_day.values(), key=lambda entry: str(entry.get("capturedAt") or ""), reverse=True)
    anchor = parse_instant(generated_at).astimezone(BEIJING).date()
    kept: list[dict[str, Any]] = []
    for entry in ordered:
        captured_at = entry.get("capturedAt")
        if not isinstance(captured_at, str):
            continue
        delta = (anchor - parse_instant(captured_at).astimezone(BEIJING).date()).days
        if 0 <= delta <= HISTORY_KEEP:
            kept.append(entry)
    return kept


def fetch_nanoka_discoveries(http: Http, watch_names: set[str]) -> list[dict[str, Any]]:
    manifest = http.request("https://static.nanoka.cc/manifest.json")
    if not isinstance(manifest, dict):
        raise RuntimeError("nanoka manifest is not an object")
    games = {
        "gi": "原神",
        "zzz": "绝区零",
        "hsr": "崩铁",
        "ww": "鸣潮",
    }
    found: list[dict[str, Any]] = []
    for game, work in games.items():
        info = manifest.get(game) or {}
        version = info.get("latest")
        new_ids = [str(x) for x in ((info.get("new") or {}).get("character") or [])]
        if not version or not new_ids:
            continue
        index = http.request(f"https://static.nanoka.cc/{game}/{version}/character.json")
        if not isinstance(index, dict):
            continue
        for cid in new_ids:
            row = index.get(cid) or {}
            if not isinstance(row, dict):
                continue
            zh = str(row.get("zh") or "").strip()
            en = str(row.get("en") or "").strip()
            if not zh and not en:
                continue
            if zh.startswith("(Test"):
                continue
            if zh in watch_names or en in watch_names:
                continue
            found.append(
                {
                    "game": game,
                    "work": work,
                    "id": cid,
                    "nameZh": zh,
                    "nameEn": en,
                    "source": "nanoka",
                    "indexVersion": version,
                }
            )
    return found


def carry_heat(
    previous_row: dict[str, Any],
    character: dict[str, Any],
    name: str,
    generated_fallback: str | None,
) -> dict[str, Any]:
    return {
        "key": character.get("key"),
        "name": name,
        "work": character.get("work") or previous_row.get("work") or "",
        "kind": character.get("kind") or previous_row.get("kind") or "",
        "heatScore": previous_row.get("heatScore") or 0,
        "heatFetchedAt": previous_row.get("heatFetchedAt") or generated_fallback,
        "heatComponents": previous_row.get("heatComponents") or {},
        "metrics": previous_row.get("metrics"),
    }


def fetch_weibo(http: Http, names: list[str]) -> dict[str, Any]:
    payload = http.request("https://weibo.com/ajax/side/hotSearch")
    if isinstance(payload, str):
        raise RuntimeError("weibo returned HTML (visitor wall)")
    if not isinstance(payload, dict):
        raise RuntimeError(f"weibo unexpected payload type {type(payload).__name__}")
    data = payload.get("data") or {}
    band = data.get("realtime") or data.get("hotgov") or []
    if not isinstance(band, list):
        band = []
    texts: list[str] = []
    for row in band:
        if isinstance(row, dict):
            texts.append(str(row.get("word") or row.get("note") or ""))
    hits = [name for name in names if any(name and name in text for text in texts)]
    return {"hotHits": hits, "hotCount": len(band)}


def run(
    out_path: Path,
    write_assets: bool,
    catalog_path: Path,
    *,
    daily_cap: int = DAILY_CAP,
    time_budget_seconds: float = TIME_BUDGET_SECONDS,
    monotonic=None,
    fetch_bangumi=None,
    fetch_bilibili=None,
    weibo_fetcher=None,
) -> int:
    generated = now_utc()
    generated_at = iso(generated)
    today = generated.astimezone(BEIJING).date().isoformat()
    clock = monotonic or time.monotonic
    started = clock()

    def budget_expired() -> bool:
        return (clock() - started) >= time_budget_seconds

    catalog = load_catalog(catalog_path)
    catalog_characters = [row for row in catalog["characters"] if isinstance(row, dict) and row.get("key")]
    previous = load_previous(out_path)
    previous_by_key: dict[str, dict[str, Any]] = {}
    previous_by_name: dict[str, dict[str, Any]] = {}
    if previous:
        for row in previous.get("characters") or []:
            if not isinstance(row, dict):
                continue
            if row.get("key"):
                previous_by_key[str(row["key"])] = row
            if row.get("name"):
                previous_by_name[str(row["name"])] = row
    named = []
    for character in catalog_characters:
        named.append((character, display_name(character, previous_by_name)))
    plan = build_queue(catalog_characters, previous_by_key, daily_cap=daily_cap)
    queue = plan["selected"]
    name_by_key = {str(character["key"]): name for character, name in named}
    queue_pairs = [(character, name_by_key[str(character["key"])]) for character in queue]
    print(f"catalog {len(catalog_characters)} cap {daily_cap} queued {len(queue)} on {today}")
    for tier in TIER_ORDER:
        stat = plan["stats"][tier]
        print(f"queue {TIER_LABELS[tier]} 入队 {stat['queued']} 推迟 {stat['deferred']}")

    sources = {
        "bangumi": source_run("failed"),
        "bilibili": source_run("failed"),
        "weibo": source_run("skipped", error="not wired as a scoring source this round"),
        "nanoka": source_run("skipped", error="character index is read from catalog.json"),
        "pixiv": source_run("skipped", error="needs PIXIV_REFRESH_TOKEN; deferred"),
    }

    bangumi_by_key: dict[str, dict[str, Any] | None] = {}
    bili_by_key: dict[str, dict[str, Any] | None] = {}
    weibo_raw: dict[str, Any] | None = None
    bili_aborted = False
    bangumi_budget_stopped = False
    bili_budget_stopped = False
    bangumi_started_pairs: list[tuple[dict[str, Any], str]] = []
    completed_keys: set[str] = set()

    bangumi_ok = False
    try:
        fetched_at = iso(now_utc())
        if fetch_bangumi is None:
            bangumi_http = Http(USER_AGENT, min_interval=0.55)
            bangumi = BangumiClient(bangumi_http)
            bangumi.load_calendar()

            def fetch_one(character: dict[str, Any], name: str, fetched_at: str) -> dict[str, Any] | None:
                key = str(character["key"])
                subject_id = character.get("bangumiSubjectId")
                if str(key).startswith("bgm:") and str(key).split(":", 1)[1].isdigit():
                    return fetch_bangumi_direct(
                        bangumi,
                        int(key.split(":", 1)[1]),
                        subject_id if isinstance(subject_id, int) else None,
                        str(character.get("kind") or "ANIME"),
                        fetched_at,
                    )
                return fetch_bangumi_character(bangumi, as_watch_item(character, name), fetched_at)
        else:
            fetch_one = fetch_bangumi

        def consume_bangumi(pair: tuple[dict[str, Any], str]) -> None:
            character, name = pair
            key = str(character["key"])
            try:
                bangumi_by_key[key] = fetch_one(character, name, fetched_at)
            except Exception as e:
                print(f"bangumi {name} failed: {e}", file=sys.stderr)
                bangumi_by_key[key] = None

        bangumi_started_pairs, bangumi_rest = take_within_budget(queue_pairs, budget_expired, consume_bangumi)
        bangumi_budget_stopped = bool(bangumi_rest)
        bangumi_ok = True
        if bangumi_budget_stopped:
            sources["bangumi"] = source_run("partial", fetched_at, error="time budget exhausted")
        else:
            sources["bangumi"] = source_run("ok", fetched_at)
    except Exception as e:
        print(f"bangumi source failed: {e}", file=sys.stderr)
        sources["bangumi"] = source_run("failed", error=str(e))

    bili_ok = False
    try:
        fetched_at = iso(now_utc())
        if fetch_bilibili is None:
            bili_http = Http(BILI_UA, min_interval=1.25)
            bili = BilibiliClient(bili_http)

            def fetch_bili_one(character: dict[str, Any], name: str, fetched_at: str) -> dict[str, Any] | None:
                return fetch_bilibili_character(
                    bili,
                    as_watch_item(character, name),
                    generated,
                    fetched_at,
                )
        else:
            fetch_bili_one = fetch_bilibili

        risk_streak = 0
        bili_started_pairs: list[tuple[dict[str, Any], str]] = []
        # A Bangumi client failure must not skip Bilibili. A budget stop must:
        # characters Bangumi never started stay carried.
        bili_plan = bangumi_started_pairs if bangumi_ok else queue_pairs
        for character, name in bili_plan:
            if budget_expired():
                bili_budget_stopped = True
                break
            if bili_aborted:
                break
            key = str(character["key"])
            try:
                bili_by_key[key] = fetch_bili_one(character, name, fetched_at)
                risk_streak = 0
            except Exception as e:
                print(f"bilibili {name} failed: {e}", file=sys.stderr)
                bili_by_key[key] = None
                text = str(e)
                if "412" in text or "429" in text:
                    risk_streak += 1
                    time.sleep(12 * risk_streak)
                    if risk_streak >= 2:
                        bili_aborted = True
                        print("bilibili rate limit; stopped the rest of this run", file=sys.stderr)
            bili_started_pairs.append((character, name))
        completed_keys = {str(character["key"]) for character, _name in bili_started_pairs}
        fetched_any = any(bili_by_key.get(key) for key in completed_keys)
        if bili_aborted and fetched_any:
            sources["bilibili"] = source_run("partial", fetched_at, error="stopped after HTTP 412/429")
            bili_ok = True
        elif bili_budget_stopped or (bangumi_budget_stopped and not fetched_any):
            sources["bilibili"] = source_run("partial", fetched_at, error="time budget exhausted")
            bili_ok = True
        elif fetched_any:
            bili_ok = True
            sources["bilibili"] = source_run("ok", fetched_at)
        else:
            sources["bilibili"] = source_run("failed", fetched_at, error="no character returned videos")
    except Exception as e:
        print(f"bilibili source failed: {e}", file=sys.stderr)
        sources["bilibili"] = source_run("failed", error=str(e))

    weibo_names = [
        name
        for character, name in named
        if str(character["key"]) in completed_keys and len(name) >= 2
    ]
    if budget_expired():
        print("weibo skipped: time budget exhausted", file=sys.stderr)
        sources["weibo"] = source_run("failed", error="time budget exhausted")
    else:
        try:
            if weibo_fetcher is None:
                weibo_http = Http(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
                    min_interval=0.8,
                    extra_headers={"Referer": "https://weibo.com/"},
                )
                weibo_raw = fetch_weibo(weibo_http, weibo_names)
            else:
                weibo_raw = weibo_fetcher(weibo_names)
            sources["weibo"] = source_run("ok", iso(now_utc()))
        except Exception as e:
            print(f"weibo source failed: {e}", file=sys.stderr)
            sources["weibo"] = source_run("failed", error=str(e))

    discoveries = [
        {
            "key": character.get("key"),
            "nameZh": character.get("name"),
            "nameEn": next((alias for alias in character.get("aliases") or [] if alias), ""),
            "work": character.get("work"),
            "game": character.get("game"),
            "source": character.get("source"),
            "firstSeenAt": character.get("firstSeenAt"),
            "status": character.get("status"),
        }
        for character in catalog_characters
        if character.get("baseline") is False and character.get("status") == "LEAKED"
    ]

    characters: list[dict[str, Any]] = []
    fresh_count = 0
    carried_count = 0
    print()
    print(f"{'角色':<12} {'B站7日投稿':>10} {'7日播放':>10} {'BGM收藏':>8} {'热度':>6}")
    for character, name in named:
        key = str(character["key"])
        previous_row = previous_by_key.get(key)
        if previous_row is None:
            named_prev = previous_by_name.get(name)
            if named_prev and (name == character.get("name") or work_compatible(character, named_prev)):
                previous_row = named_prev
        if key not in completed_keys:
            if previous_row:
                characters.append(carry_heat(previous_row, character, name, previous.get("generatedAt") if previous else None))
                carried_count += 1
            continue
        bangumi_m = bangumi_by_key.get(key)
        bili_m = bili_by_key.get(key)
        if bili_aborted and not bili_m and previous_row:
            characters.append(carry_heat(previous_row, character, name, previous.get("generatedAt") if previous else None))
            carried_count += 1
            continue
        if bangumi_m is None and bili_m is None and previous_row:
            characters.append(carry_heat(previous_row, character, name, previous.get("generatedAt") if previous else None))
            carried_count += 1
            continue
        kind = character.get("kind") or "GAME"
        prev = previous_metrics(previous, name, generated_at)
        bili_score = bili_component(bili_m)
        bgm_score = bangumi_component(kind, bangumi_m)
        video_growth = growth_score(
            None if bili_m is None else bili_m.get("recentVideos7d"),
            None if prev is None else prev.get("bilibiliRecentVideos7d"),
            floor=3,
        )
        play_growth = growth_score(
            None if bili_m is None else bili_m.get("recentPlaySum7d"),
            None if prev is None else prev.get("bilibiliRecentPlaySum7d"),
            floor=1000,
        )
        growth = None
        if video_growth is not None or play_growth is not None:
            growth_parts = [part for part in (video_growth, play_growth) if part is not None]
            growth = sum(growth_parts) / len(growth_parts)
        weights = {"bilibili": 0.50, "bangumi": 0.20, "growth": 0.30} if growth is not None else {"bilibili": 0.75, "bangumi": 0.25}
        heat, components = weighted_blend(
            {"bilibili": bili_score, "bangumi": bgm_score, "growth": growth},
            weights,
        )
        if weibo_raw and name in (weibo_raw.get("hotHits") or []):
            heat = min(100, heat + 6)
            components = dict(components)
        row = {
            "key": key,
            "name": name,
            "work": character.get("work") or "",
            "kind": kind,
            "heatScore": heat,
            "heatFetchedAt": generated_at,
            "heatComponents": components,
            "metrics": {
                "bangumi": bangumi_m,
                "bilibili": bili_m,
                "weibo": {
                    "onHotSearch": bool(weibo_raw and name in (weibo_raw.get("hotHits") or [])),
                    "fetchedAt": sources["weibo"].get("fetchedAt"),
                } if sources["weibo"]["status"] == "ok" else None,
            },
        }
        characters.append(row)
        fresh_count += 1
        videos = "-" if bili_m is None else bili_m.get("recentVideos7d")
        plays = "-" if bili_m is None else bili_m.get("recentPlaySum7d")
        collects = "-" if bangumi_m is None else bangumi_m.get("collects")
        print(f"{name:<12} {str(videos):>10} {str(plays):>10} {str(collects):>8} {heat:>6}")
    print(
        f"fetched {fresh_count}, carried {carried_count}, bili_aborted={bili_aborted}, "
        f"budget 入队 {len(queue)} 实抓 {len(completed_keys)} 推迟 {len(queue) - len(completed_keys)}"
    )

    allowed_names = {name for _character, name in named}
    history = build_history(previous, generated_at, allowed_names=allowed_names)
    feed = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": generated_at,
        "scoreMethod": SCORE_METHOD,
        "sources": sources,
        "characters": characters,
        "discoveries": discoveries,
        "history": history,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(feed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print()
    print(f"wrote {out_path}")
    if write_assets:
        ASSETS_OUT.parent.mkdir(parents=True, exist_ok=True)
        ASSETS_OUT.write_text(out_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"wrote {ASSETS_OUT}")
    failed = [name for name, row in sources.items() if row["status"] == "failed"]
    if failed:
        print("failed sources:", ", ".join(failed))
    if not bangumi_ok and not bili_ok:
        print("warning: both required sources failed; scores may be zero", file=sys.stderr)
    return 0


def check_dedupe() -> int:
    """Local check: same Beijing date keeps the later snapshot, and growth skips that day."""
    def row(name: str, videos: int) -> dict[str, Any]:
        return {
            "name": name,
            "bangumiCollects": 1,
            "bangumiSubjectCollect": None,
            "bilibiliRecentVideos7d": videos,
            "bilibiliRecentPlaySum7d": 10,
            "heatScore": videos,
        }

    def live(name: str, videos: int) -> dict[str, Any]:
        return {
            "name": name,
            "heatScore": videos,
            "metrics": {
                "bangumi": {"collects": 1, "subjectCollect": None},
                "bilibili": {"recentVideos7d": videos, "recentPlaySum7d": 10},
            },
        }

    previous = {
        "generatedAt": "2026-10-13T04:00:00Z",
        "characters": [live("锁暝", 2)],
        "history": [
            {"capturedAt": "2026-10-13T01:00:00Z", "characters": [row("锁暝", 1)]},
            {"capturedAt": "2026-10-12T03:00:00Z", "characters": [row("锁暝", 50)]},
        ],
    }
    history = build_history(previous, "2026-10-13T06:00:00Z")
    days = [beijing_day(entry["capturedAt"]) for entry in history]
    if days != ["2026-10-13", "2026-10-12"]:
        print("dedupe days", days)
        return 1
    today = history[0]
    if today["capturedAt"] != "2026-10-13T04:00:00Z":
        print("kept", today["capturedAt"], "expected the later Oct 13 snapshot")
        return 1
    if today["characters"][0]["bilibiliRecentVideos7d"] != 2:
        print("later snapshot was not the one kept", today["characters"])
        return 1
    keys = set(today["characters"][0])
    expected = {
        "name",
        "bangumiCollects",
        "bangumiSubjectCollect",
        "bilibiliRecentVideos7d",
        "bilibiliRecentPlaySum7d",
        "heatScore",
    }
    if keys != expected:
        print("history schema changed", keys)
        return 1
    baseline = previous_metrics(previous, "锁暝", "2026-10-13T06:00:00Z")
    if baseline is None or baseline["bilibiliRecentVideos7d"] != 50:
        print("growth baseline", baseline)
        return 1
    only_today = {
        "generatedAt": "2026-10-13T04:00:00Z",
        "characters": [live("锁暝", 2)],
        "history": [{"capturedAt": "2026-10-13T01:00:00Z", "characters": [row("锁暝", 1)]}],
    }
    if previous_metrics(only_today, "锁暝", "2026-10-13T06:00:00Z") is not None:
        print("same-day-only history should not be a growth baseline")
        return 1
    print("dedupe ok: Oct 13 kept 04:00Z, growth baseline is Oct 12 (videos=50)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch heat signals for wig-radar")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    parser.add_argument("--write-assets", action="store_true", dest="write_assets")
    parser.add_argument("--no-write-assets", action="store_false", dest="write_assets")
    parser.set_defaults(write_assets=False)
    parser.add_argument("--daily-cap", type=int, default=DAILY_CAP)
    parser.add_argument("--time-budget-seconds", type=float, default=TIME_BUDGET_SECONDS)
    parser.add_argument("--check-dedupe", action="store_true", help="Run the local history-dedupe check and exit")
    args = parser.parse_args()
    if args.check_dedupe:
        return check_dedupe()
    try:
        return run(
            args.out,
            args.write_assets,
            args.catalog,
            daily_cap=args.daily_cap,
            time_budget_seconds=args.time_budget_seconds,
        )
    except Exception as e:
        print(f"fatal: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
