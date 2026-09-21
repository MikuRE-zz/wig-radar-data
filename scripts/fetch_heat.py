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
DEFAULT_OUT = ROOT / "data" / "heat.json"
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
            matched = [s for s in subjects if subject_matches_work(s, synonyms)]
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


def fetch_bilibili_character(
    client: BilibiliClient,
    item: dict[str, Any],
    now: datetime,
    fetched_at: str,
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    keywords = list(item.get("bilibiliKeywords") or [item["name"]])
    for keyword in keywords:
        payload = client.search_videos(keyword, pages=2)
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


def previous_metrics(previous: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if not previous:
        return None
    for ch in previous.get("characters") or []:
        if ch.get("name") != name:
            continue
        bangumi = (ch.get("metrics") or {}).get("bangumi") or {}
        bili = (ch.get("metrics") or {}).get("bilibili") or {}
        return {
            "name": name,
            "bangumiCollects": bangumi.get("collects"),
            "bangumiSubjectCollect": bangumi.get("subjectCollect"),
            "bilibiliRecentVideos7d": bili.get("recentVideos7d"),
            "bilibiliRecentPlaySum7d": bili.get("recentPlaySum7d"),
            "heatScore": ch.get("heatScore"),
        }
    for entry in previous.get("history") or []:
        for row in entry.get("characters") or []:
            if row.get("name") == name:
                return row
    return None


def build_history(previous: dict[str, Any] | None, generated_at: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if previous:
        prev_at = previous.get("generatedAt")
        if prev_at and prev_at != generated_at:
            entries.append(
                {
                    "capturedAt": prev_at,
                    "characters": [
                        {
                            "name": ch.get("name"),
                            "bangumiCollects": ((ch.get("metrics") or {}).get("bangumi") or {}).get("collects"),
                            "bangumiSubjectCollect": ((ch.get("metrics") or {}).get("bangumi") or {}).get("subjectCollect"),
                            "bilibiliRecentVideos7d": ((ch.get("metrics") or {}).get("bilibili") or {}).get("recentVideos7d"),
                            "bilibiliRecentPlaySum7d": ((ch.get("metrics") or {}).get("bilibili") or {}).get("recentPlaySum7d"),
                            "heatScore": ch.get("heatScore"),
                        }
                        for ch in previous.get("characters") or []
                    ],
                }
            )
        entries.extend(previous.get("history") or [])
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for entry in entries:
        key = str(entry.get("capturedAt") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(entry)
    return uniq[:HISTORY_KEEP]


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


def run(out_path: Path, write_assets: bool) -> int:
    generated = now_utc()
    generated_at = iso(generated)
    watchlist = load_watchlist()
    previous = load_previous(out_path)

    sources = {
        "bangumi": source_run("failed"),
        "bilibili": source_run("failed"),
        "weibo": source_run("skipped", error="not wired as a scoring source this round"),
        "nanoka": source_run("failed"),
        "pixiv": source_run("skipped", error="needs PIXIV_REFRESH_TOKEN; deferred"),
    }

    bangumi_http = Http(USER_AGENT, min_interval=0.7)
    bili_http = Http(BILI_UA, min_interval=1.0)
    extra_http = Http(USER_AGENT, min_interval=0.5)

    bangumi_by_name: dict[str, dict[str, Any] | None] = {}
    bili_by_name: dict[str, dict[str, Any] | None] = {}
    discoveries: list[dict[str, Any]] = []
    weibo_raw: dict[str, Any] | None = None

    bangumi_ok = False
    try:
        bangumi = BangumiClient(bangumi_http)
        bangumi.load_calendar()
        fetched_at = iso(now_utc())
        for item in watchlist:
            try:
                bangumi_by_name[item["name"]] = fetch_bangumi_character(bangumi, item, fetched_at)
            except Exception as e:
                print(f"bangumi {item['name']} failed: {e}", file=sys.stderr)
                bangumi_by_name[item["name"]] = None
        bangumi_ok = True
        sources["bangumi"] = source_run("ok", fetched_at)
    except Exception as e:
        print(f"bangumi source failed: {e}", file=sys.stderr)
        sources["bangumi"] = source_run("failed", error=str(e))

    bili_ok = False
    try:
        bili = BilibiliClient(bili_http)
        fetched_at = iso(now_utc())
        for item in watchlist:
            try:
                bili_by_name[item["name"]] = fetch_bilibili_character(bili, item, generated, fetched_at)
            except Exception as e:
                print(f"bilibili {item['name']} failed: {e}", file=sys.stderr)
                bili_by_name[item["name"]] = None
        if any(bili_by_name.get(item["name"]) for item in watchlist):
            bili_ok = True
            sources["bilibili"] = source_run("ok", fetched_at)
        else:
            sources["bilibili"] = source_run("failed", fetched_at, error="no character returned videos")
    except Exception as e:
        print(f"bilibili source failed: {e}", file=sys.stderr)
        sources["bilibili"] = source_run("failed", error=str(e))

    try:
        weibo_http = Http(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
            min_interval=0.8,
            extra_headers={"Referer": "https://weibo.com/"},
        )
        weibo_raw = fetch_weibo(weibo_http, [item["name"] for item in watchlist])
        sources["weibo"] = source_run("ok", iso(now_utc()))
    except Exception as e:
        print(f"weibo source failed: {e}", file=sys.stderr)
        sources["weibo"] = source_run("failed", error=str(e))

    try:
        watch_names = {item["name"] for item in watchlist}
        for item in watchlist:
            watch_names.update(item.get("searchNames") or [])
        discoveries = fetch_nanoka_discoveries(extra_http, watch_names)
        sources["nanoka"] = source_run("ok", iso(now_utc()))
    except Exception as e:
        print(f"nanoka source failed: {e}", file=sys.stderr)
        sources["nanoka"] = source_run("failed", error=str(e))

    characters: list[dict[str, Any]] = []
    print()
    print(f"{'角色':<8} {'B站7日投稿':>10} {'7日播放':>10} {'BGM收藏':>8} {'热度':>6} {'原假值':>6} {'差值':>6}")
    for item in watchlist:
        name = item["name"]
        kind = item["kind"]
        bangumi_m = bangumi_by_name.get(name)
        bili_m = bili_by_name.get(name)
        prev = previous_metrics(previous, name)
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
            growth_parts = [p for p in (video_growth, play_growth) if p is not None]
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
            "name": name,
            "work": item["work"],
            "kind": kind,
            "heatScore": heat,
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
        seed = SEED_HEAT.get(name)
        videos = "-" if bili_m is None else bili_m.get("recentVideos7d")
        plays = "-" if bili_m is None else bili_m.get("recentPlaySum7d")
        collects = "-" if bangumi_m is None else bangumi_m.get("collects")
        delta = "-" if seed is None else heat - seed
        print(f"{name:<8} {str(videos):>10} {str(plays):>10} {str(collects):>8} {heat:>6} {str(seed):>6} {str(delta):>6}")

    history = build_history(previous, generated_at)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch heat signals for wig-radar")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--write-assets", action="store_true", default=True)
    parser.add_argument("--no-write-assets", action="store_false", dest="write_assets")
    args = parser.parse_args()
    try:
        return run(args.out, args.write_assets)
    except Exception as e:
        print(f"fatal: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
