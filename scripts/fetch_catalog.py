#!/usr/bin/env python3
"""Build catalog.json from the public character sources.

Run from the repo root:

    python scripts/fetch_catalog.py --out catalog.json
    python scripts/fetch_catalog.py --only endfield

The first import marks every character baseline. Later runs only stamp
firstSeenAt on keys that were not in the previous file. A source that fails
keeps that source's previous characters. `--only` refreshes the named sources
and leaves every other source's rows and metadata as stored.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.catalog_lib import (  # noqa: E402
    AUTO_TAG_BATCH_LIMIT,
    BEIJING,
    beijing_today,
    build_checked_hair,
    dedupe_aliases,
    flag_duplicate_samples,
    merge_characters,
    restore_previous_auto_tags,
    search_keys,
    status_from_release,
    strip_ruby,
)
from scripts.danbooru_tags import DanbooruClient, refresh_auto_tags  # noqa: E402
from scripts.haircolor import (  # noqa: E402
    PORTRAIT,
    POSTER,
    UNKNOWN,
    classify_art,
    extract_hair,
    pixels_from_rgba,
)

USER_AGENT = "WigRadar/0.1.0 (mikure; GitHubActions; cos wig selection tool)"
CACHE = ROOT / ".cache"
NANOKA = "https://static.nanoka.cc"
KURO_API = "https://api.kurobbs.com"
KURO_CATALOGUE = "1105"
AK_TABLE = (
    "https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/"
    "master/zh_CN/gamedata/excel/character_table.json"
)
SCHALE_STUDENTS = "https://schaledb.com/data/cn/students.json"
BANGUMI = "https://api.bgm.tv"
ENDFIELD_SUBJECT = 374638
ENDFIELD_WORK = "明日方舟：终末地"
ENDFIELD_WIKI = "https://endfield.wiki.gg/api.php"
# A file named like the operator but only a bust crop is not the full-body illustration.
ENDFIELD_PORTRAIT_MIN_EDGE = 512
KNOWN_SOURCES = (
    "nanoka-gi",
    "nanoka-hsr",
    "nanoka-zzz",
    "wuwa",
    "arknights",
    "endfield",
    "bluearchive",
    "bangumi",
)
# This season's hottest shows, then next season. A short side is filled from the other.
CURRENT_SEASON_KEEP = 20
NEXT_SEASON_KEEP = 10
MAIN_CAST_KEEP = 4
BANGUMI_PERSON = 1
BANGUMI_SELECTION = "top-works-v1"
COLLECTION_KEYS = ("wish", "collect", "doing", "on_hold", "dropped")

AK_PROFESSIONS = {
    "PIONEER",
    "WARRIOR",
    "TANK",
    "SNIPER",
    "CASTER",
    "MEDIC",
    "SUPPORT",
    "SPECIAL",
}
FIGURE_PRIORITY = ("角色立绘", "角色预告", "编队立绘", "基本信息")
NO_ICON = "no_icon"


class PermanentError(RuntimeError):
    pass


class TransientError(RuntimeError):
    pass


class Http:
    def __init__(self, user_agent: str, min_interval: float, extra_headers: dict[str, str] | None = None):
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.extra_headers = extra_headers or {}
        self._last = 0.0
        self._lock = threading.Lock()

    def _reserve(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def request(
        self,
        url: str,
        *,
        data: Any = None,
        headers: dict[str, str] | None = None,
        method: str | None = None,
        timeout: float = 40,
        retries: int = 3,
        form: dict[str, str] | None = None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(retries):
            self._reserve()
            hdrs = {"User-Agent": self.user_agent, "Accept": "application/json,text/plain,*/*"}
            hdrs.update(self.extra_headers)
            if headers:
                hdrs.update(headers)
            body = None
            req_method = method
            if form is not None:
                body = urllib.parse.urlencode(form).encode()
                hdrs["Content-Type"] = "application/x-www-form-urlencoded"
                req_method = req_method or "POST"
            elif data is not None:
                body = json.dumps(data).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/json")
                req_method = req_method or "POST"
            req = urllib.request.Request(url, data=body, headers=hdrs, method=req_method)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                    if not raw:
                        return None
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as error:
                last_error = error
                snippet = error.read()[:240].decode("utf-8", errors="replace")
                retryable = error.code in {408, 409, 412, 429, 500, 502, 503, 504}
                if not retryable or attempt == retries - 1:
                    kind = PermanentError if error.code in {400, 401, 403, 404, 410} else TransientError
                    if retryable and attempt == retries - 1:
                        kind = TransientError
                    raise kind(f"HTTP {error.code} {url} {snippet}") from error
                time.sleep(1.2 * (2**attempt) + random.random())
            except Exception as error:
                last_error = error
                if attempt == retries - 1:
                    raise TransientError(f"{type(error).__name__} {url} {error}") from error
                time.sleep(1.2 * (2**attempt) + random.random())
        raise TransientError(str(last_error) if last_error else "request failed")

    def bytes(self, url: str, timeout: float = 25) -> bytes:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": self.user_agent, "Accept": "image/*,*/*"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                content_type = resp.headers.get("content-type") or ""
        except urllib.error.HTTPError as error:
            if error.code in {404, 400, 410, 403}:
                raise PermanentError(f"HTTP {error.code} {url}") from error
            raise TransientError(f"HTTP {error.code} {url}") from error
        except Exception as error:
            raise TransientError(f"{type(error).__name__} {url}") from error
        if "text/html" in content_type or data[:15].lstrip().lower().startswith(b"<!doctype") or data[:1] == b"<":
            raise PermanentError(f"HTML {url}")
        if len(data) < 32:
            raise PermanentError(f"empty image {url}")
        return data


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def cache_get(url: str) -> Any | None:
    path = CACHE / "http" / f"{hashlib.sha1(url.encode()).hexdigest()}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def cache_put(url: str, payload: Any) -> None:
    path = CACHE / "http" / f"{hashlib.sha1(url.encode()).hexdigest()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def get_json(http: Http, url: str, *, timeout: float = 60, use_cache: bool = True) -> Any:
    if use_cache:
        cached = cache_get(url)
        if cached is not None:
            return cached
    payload = http.request(url, timeout=timeout)
    if use_cache and payload is not None:
        cache_put(url, payload)
    return payload


def version_label(raw: str) -> str:
    base = str(raw).split("+", 1)[0]
    parts = base.split(".")
    if len(parts) >= 2 and parts[0].isdigit():
        return f"{parts[0]}.{parts[1]}"
    return base


def parse_game_release(value: Any) -> date | None | str:
    """Return a date, 'unreleased', or None when the source has no date."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        if value < 946684800:
            return "unreleased"
        return datetime.fromtimestamp(int(value), tz=timezone.utc).astimezone(BEIJING).date()
    text = str(value).strip()
    if text.startswith("1970") or text.startswith("0000"):
        return "unreleased"
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def season_bounds(today: date) -> tuple[date, date, date, date]:
    year = today.year
    if today.month <= 3:
        start, end = date(year, 1, 1), date(year, 4, 1)
        nxt_start, nxt_end = date(year, 4, 1), date(year, 7, 1)
    elif today.month <= 6:
        start, end = date(year, 4, 1), date(year, 7, 1)
        nxt_start, nxt_end = date(year, 7, 1), date(year, 10, 1)
    elif today.month <= 9:
        start, end = date(year, 7, 1), date(year, 10, 1)
        nxt_start, nxt_end = date(year, 10, 1), date(year + 1, 1, 1)
    else:
        start, end = date(year, 10, 1), date(year + 1, 1, 1)
        nxt_start, nxt_end = date(year + 1, 1, 1), date(year + 1, 4, 1)
    return start, end, nxt_start, nxt_end


def parse_day(value: str | None) -> date | None:
    if not value or not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def usable_image(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    if NO_ICON in url:
        return None
    return url


def base_character(**fields: Any) -> dict[str, Any]:
    row = {
        "aliases": [],
        "version": None,
        "releaseDate": None,
        "artUrl": None,
        "hairSampleUrl": None,
        "artKind": UNKNOWN,
    }
    row.update(fields)
    row["aliases"] = dedupe_aliases(str(row.get("name") or ""), list(row.get("aliases") or []))
    return row


def source_run(status: str, endpoint: str, *, count: int = 0, error: str | None = None, extra: dict | None = None) -> dict[str, Any]:
    row = {
        "status": status,
        "fetchedAt": iso_now(),
        "error": error,
        "endpoint": endpoint,
        "count": count,
    }
    if extra:
        row.update(extra)
    return row


def shrink_guard(
    source: str,
    rows: list[dict] | None,
    previous: dict | None,
    *,
    selection: str | None = None,
) -> list[dict] | None:
    if rows is None or not previous:
        return rows
    # A new bangumi selection rule is allowed to cut the list in half once.
    # The same version keeps the guard so a broken fetch cannot wipe the source.
    if source == "bangumi":
        prev_meta = ((previous.get("sources") or {}).get("bangumi") or {})
        if not isinstance(prev_meta, dict):
            prev_meta = {}
        if prev_meta.get("selection") != selection:
            print(
                f"bangumi: selection {prev_meta.get('selection')!r} -> {selection!r}; shrink guard skipped",
                file=sys.stderr,
            )
            return rows
    old = [row for row in previous.get("characters") or [] if isinstance(row, dict) and row.get("source") == source]
    if len(old) >= 20 and len(rows) < len(old) * 0.5:
        print(f"{source}: new list {len(rows)} is under half of previous {len(old)}; keeping previous", file=sys.stderr)
        return None
    return rows


def parse_only(value: str) -> set[str]:
    names = {part.strip() for part in (value or "").split(",") if part.strip()}
    unknown = names.difference(KNOWN_SOURCES)
    if unknown:
        raise SystemExit("unknown source: " + ", ".join(sorted(unknown)))
    return names


def clean_label(value: Any) -> str:
    return html.unescape(str(value or "")).strip()


def collection_total(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    collection = payload.get("collection")
    if not isinstance(collection, dict):
        return 0
    total = 0
    for key in COLLECTION_KEYS:
        value = collection.get(key)
        if value is None:
            continue
        try:
            total += int(value)
        except (TypeError, ValueError):
            continue
    return total


def work_heats(show_ids: list[int], fetch_subject) -> dict[int, int]:
    """Sum subject collection counts. One failed subject scores 0 and does not abort."""
    heats: dict[int, int] = {}
    for sid in show_ids:
        try:
            heats[sid] = collection_total(fetch_subject(sid))
        except Exception as error:
            print(f"bangumi subject {sid} heat failed: {error}", file=sys.stderr)
            heats[sid] = 0
    return heats


def select_shows(
    current: list[dict[str, Any]],
    upcoming: list[dict[str, Any]],
    heats: dict[int, int],
    *,
    current_keep: int = CURRENT_SEASON_KEEP,
    next_keep: int = NEXT_SEASON_KEEP,
) -> list[dict[str, Any]]:
    """Keep the hottest current-season and next-season shows, filling to current_keep + next_keep."""
    target = current_keep + next_keep

    def ranked(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def sort_key(row: dict[str, Any]) -> tuple[int, int]:
            sid = row.get("id")
            heat = heats.get(sid, 0) if isinstance(sid, int) else 0
            try:
                heat = int(heat or 0)
            except (TypeError, ValueError):
                heat = 0
            return (-heat, sid if isinstance(sid, int) else 0)

        return sorted(rows, key=sort_key)

    current_ranked = ranked(current)
    next_ranked = ranked(upcoming)
    picked_current = current_ranked[:current_keep]
    picked_next = next_ranked[:next_keep]
    picked = list(picked_current) + list(picked_next)
    if len(picked) < target:
        if len(picked_current) < current_keep:
            filler = next_ranked[next_keep:]
        elif len(picked_next) < next_keep:
            filler = current_ranked[current_keep:]
        else:
            filler = []
        picked.extend(filler[: target - len(picked)])
    chosen: list[dict[str, Any]] = []
    for row in picked:
        copied = dict(row)
        sid = copied.get("id")
        heat = heats.get(sid, 0) if isinstance(sid, int) else 0
        try:
            copied["workHeat"] = int(heat or 0)
        except (TypeError, ValueError):
            copied["workHeat"] = 0
        chosen.append(copied)
    return chosen


def _numeric_token(text: str) -> bool:
    compact = text.strip()
    if not compact:
        return True
    for ch in compact:
        if ch.isspace() or ch.isdigit():
            continue
        if unicodedata.category(ch).startswith("P"):
            continue
        return False
    return True


def numeric_only_name(cn_name: str, original_name: str) -> bool:
    """True when every present name is only digits, whitespace, and punctuation."""
    labels = [text.strip() for text in (cn_name, original_name) if text and str(text).strip()]
    if not labels:
        return False
    return all(_numeric_token(text) for text in labels)


def _skip_traveler_form(cid: str, name: str) -> bool:
    """Element kits are the same traveler, not a new character."""
    if cid in {"10000005", "10000007"}:
        return False
    return "-" in cid or name == "旅行者"


def junk_name(*names: str) -> bool:
    for text in names:
        if not text:
            continue
        if text.startswith("(Test") or text.startswith("（测试") or "测试角色" in text:
            return True
    return False


def fetch_nanoka_game(http: Http, code: str, game: str, work_name: str) -> tuple[list[dict] | None, dict[str, Any]]:
    endpoint = f"{NANOKA}/manifest.json"
    try:
        manifest = get_json(http, endpoint, timeout=40)
        info = (manifest or {}).get(code) or {}
        latest = str(info.get("latest") or "")
        live = str(info.get("live") or latest)
        if not latest:
            raise RuntimeError(f"manifest has no {code}.latest")
        latest_url = f"{NANOKA}/{code}/{urllib.parse.quote(latest, safe='+')}/character.json"
        latest_idx = get_json(http, latest_url, timeout=60)
        live_idx = latest_idx
        live_url = latest_url
        if live and live != latest:
            live_url = f"{NANOKA}/{code}/{urllib.parse.quote(live, safe='+')}/character.json"
            live_idx = get_json(http, live_url, timeout=60)
        if not isinstance(latest_idx, dict):
            raise RuntimeError("character index is not an object")
        live_ids = set(live_idx) if isinstance(live_idx, dict) else set(latest_idx)
        today = date.fromisoformat(beijing_today())
        characters = []
        for cid, row in latest_idx.items():
            if not isinstance(row, dict):
                continue
            zh = str(row.get("zh") or "").strip()
            en = str(row.get("en") or "").strip()
            if junk_name(zh, en) or zh in {"...", "…", "？", "?"}:
                continue
            if not zh and not en:
                continue
            if code == "gi" and _skip_traveler_form(str(cid), zh):
                continue
            built = nanoka_character(code, game, work_name, str(cid), row, str(cid) in live_ids, latest, live, today)
            if built:
                characters.append(built)
        meta = source_run(
            "ok",
            latest_url,
            count=len(characters),
            extra={"liveEndpoint": live_url, "latest": latest, "live": live},
        )
        print(f"nanoka {code}: {len(characters)} characters (latest {latest}, live {live})")
        return characters, meta
    except Exception as error:
        print(f"nanoka {code} failed: {error}", file=sys.stderr)
        return None, source_run("failed", endpoint, error=str(error))


def nanoka_character(
    code: str,
    game: str,
    work_name: str,
    cid: str,
    row: dict[str, Any],
    in_live: bool,
    latest: str,
    live: str,
    today: date,
) -> dict[str, Any] | None:
    zh = str(row.get("zh") or "").strip()
    en = str(row.get("en") or "").strip()
    name = zh or en
    leaked = False
    release: date | None = None
    if code == "zzz":
        leaked = not in_live
    elif code in {"gi", "hsr"}:
        parsed = parse_game_release(row.get("release")) if "release" in row else None
        if parsed == "unreleased" or not in_live:
            leaked = True
        elif isinstance(parsed, date):
            release = parsed
    version_raw = latest if leaked else live
    version = version_label(version_raw)
    status = status_from_release("GAME", release, today, leaked=leaked)
    art, sample = nanoka_art(code, cid, row)
    aliases = [
        en,
        strip_ruby(str(row.get("ja") or "")),
        str(row.get("ko") or ""),
        str(row.get("nickname") or ""),
        str(row.get("code") or ""),
    ]
    return base_character(
        key=f"{code}:{cid}",
        name=name,
        aliases=aliases,
        work=f"{work_name} {version}",
        game=game,
        version=version,
        kind="GAME",
        status=status,
        releaseDate=release.isoformat() if release else None,
        artUrl=art,
        hairSampleUrl=sample,
        source=f"nanoka-{code}",
    )


def nanoka_art(code: str, cid: str, row: dict[str, Any]) -> tuple[str | None, str | None]:
    icon = str(row.get("icon") or "")
    if code == "gi":
        if not icon:
            return None, None
        gacha = icon.replace("AvatarIcon", "Gacha_AvatarImg")
        return f"{NANOKA}/assets/gi/{gacha}.webp", f"{NANOKA}/assets/gi/{icon}.webp"
    if code == "hsr":
        return (
            f"{NANOKA}/assets/hsr/avatardrawcard/{cid}.webp",
            f"{NANOKA}/assets/hsr/avatarroundicon/{cid}.webp",
        )
    if code == "zzz":
        if not icon:
            return None, None
        circle = icon.replace("IconRole", "IconRoleCircle") if icon.startswith("IconRole") else ""
        sample = f"{NANOKA}/assets/zzz/{circle}.webp" if circle else None
        return f"{NANOKA}/assets/zzz/{icon}.webp", sample
    return None, None


def kuro_post(http: Http, path: str, fields: dict[str, str]) -> dict[str, Any]:
    payload = http.request(
        KURO_API + path,
        form=fields,
        headers={
            "origin": "https://wiki.kurobbs.com",
            "referer": "https://wiki.kurobbs.com/",
            "wiki_type": "9",
            "source": "h5",
            "devcode": "wigradar" + "0" * 24,
        },
        timeout=40,
    )
    if not isinstance(payload, dict) or payload.get("code") != 200:
        message = payload.get("msg") if isinstance(payload, dict) else payload
        raise RuntimeError(f"kuro {path} {message}")
    return payload


def fetch_wuwa(http: Http, nanoka_rows: list[dict] | None, today: date) -> tuple[list[dict] | None, dict[str, Any]]:
    endpoint = f"{KURO_API}/wiki/core/catalogue/item/getPage?catalogueId={KURO_CATALOGUE}"
    nanoka_by_name: dict[str, dict] = {}
    nanoka_note = "nanoka ww index unavailable"
    if nanoka_rows is not None:
        nanoka_note = f"nanoka ww {len(nanoka_rows)}"
        for row in nanoka_rows:
            nanoka_by_name.setdefault(row["name"], row)
    try:
        page = kuro_post(
            http,
            "/wiki/core/catalogue/item/getPage",
            {"catalogueId": KURO_CATALOGUE, "page": "1", "pageSize": "100"},
        )
        records = ((page.get("data") or {}).get("results") or {}).get("records") or []
        if not isinstance(records, list) or not records:
            raise RuntimeError("resonator catalogue was empty")
        details: dict[str, dict[str, Any] | None] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            for record in records:
                entry_id = str(record.get("entryId") or "")
                if entry_id:
                    futures[pool.submit(fetch_kuro_role, http, entry_id)] = entry_id
            for future in as_completed(futures):
                entry_id = futures[future]
                try:
                    details[entry_id] = future.result()
                except Exception as error:
                    print(f"kuro detail {entry_id} failed: {error}", file=sys.stderr)
                    details[entry_id] = None
        version = None
        if nanoka_rows:
            version = nanoka_rows[0].get("version")
        characters = []
        used_nanoka: set[str] = set()
        for record in records:
            name = str(record.get("name") or "").strip()
            entry_id = str(record.get("entryId") or "")
            if not name or not entry_id:
                continue
            nanoka = nanoka_by_name.get(name)
            if nanoka:
                used_nanoka.add(nanoka["key"])
            role = details.get(entry_id)
            characters.append(wuwa_character(name, entry_id, role, nanoka, version, today))
        for row in nanoka_by_name.values():
            if row["key"] in used_nanoka:
                continue
            extra = dict(row)
            extra["source"] = "wuwa"
            extra["game"] = "wuwa"
            characters.append(extra)
        failed_details = sum(1 for value in details.values() if value is None)
        status = "partial" if failed_details else "ok"
        error = f"{failed_details} entry details failed" if failed_details else None
        meta = source_run(
            status,
            endpoint,
            count=len(characters),
            error=error,
            extra={
                "nanoka": nanoka_note,
                "kuroCatalogue": KURO_CATALOGUE,
                "kuroRecords": len(records),
            },
        )
        print(f"wuwa: {len(characters)} characters ({nanoka_note}, kuro {len(records)})")
        return characters, meta
    except Exception as error:
        print(f"wuwa kuro failed: {error}", file=sys.stderr)
        if nanoka_rows:
            fallback = []
            for row in nanoka_rows:
                copied = dict(row)
                copied["source"] = "wuwa"
                copied["game"] = "wuwa"
                copied["status"] = "EVERGREEN"
                fallback.append(copied)
            meta = source_run(
                "partial",
                endpoint,
                count=len(fallback),
                error=f"kuro failed, names kept from nanoka: {error}",
                extra={"nanoka": nanoka_note},
            )
            return fallback, meta
        return None, source_run("failed", endpoint, error=str(error), extra={"nanoka": nanoka_note})


def fetch_kuro_role(http: Http, entry_id: str) -> dict[str, Any]:
    payload = kuro_post(http, "/wiki/core/catalogue/item/getEntryDetail", {"id": entry_id})
    content = (payload.get("data") or {}).get("content") or {}
    for module in content.get("modules") or []:
        if not isinstance(module, dict):
            continue
        for component in module.get("components") or []:
            if isinstance(component, dict) and component.get("type") == "role-component":
                role = component.get("role") or {}
                if isinstance(role, dict):
                    return role
    return {}


def wuwa_character(
    name: str,
    entry_id: str,
    role: dict[str, Any] | None,
    nanoka: dict[str, Any] | None,
    version: str | None,
    today: date,
) -> dict[str, Any]:
    aliases = list((nanoka or {}).get("aliases") or [])
    if name == "心":
        aliases.append("心月狐")
    figures = []
    subtitle = ""
    if isinstance(role, dict):
        figures = [fig for fig in (role.get("figures") or []) if isinstance(fig, dict)]
        subtitle = str(role.get("subtitle") or "")
    if subtitle:
        aliases.append(subtitle)
    art = pick_figure(figures)
    labels = [
        str(fig.get("name") or "")
        for fig in figures
        if fig.get("url") and "海报" not in str(fig.get("name") or "")
    ]
    # Only a page that still has nothing but the drip image is unreleased.
    # Missing the literal label "角色立绘" is common on old resonators.
    if "角色预告" in labels and "角色立绘" not in labels and "编队立绘" not in labels:
        status = "LEAKED"
    else:
        status = status_from_release("GAME", None, today)
    key = nanoka["key"] if nanoka else f"kuro:{entry_id}"
    if nanoka and nanoka.get("key", "").startswith("ww:"):
        key = nanoka["key"]
    elif nanoka:
        # nanoka builder uses ww? fetch_nanoka uses code prefix. For ww the code is ww.
        key = nanoka["key"]
    work_version = version or (nanoka or {}).get("version")
    work = f"鸣潮 {work_version}" if work_version else "鸣潮"
    return base_character(
        key=key if str(key).startswith("ww:") or str(key).startswith("kuro:") else f"ww:{key}",
        name=name,
        aliases=aliases,
        work=work,
        game="wuwa",
        version=work_version,
        kind="GAME",
        status=status,
        releaseDate=None,
        artUrl=art,
        hairSampleUrl=None,
        source="wuwa",
    )


def pick_figure(figures: list[dict[str, Any]]) -> str | None:
    named = []
    for fig in figures:
        label = str(fig.get("name") or "")
        url = fig.get("url")
        if not url or "海报" in label:
            continue
        named.append((label, str(url)))
    for wanted in FIGURE_PRIORITY:
        for label, url in named:
            if label == wanted:
                return url
    return named[0][1] if named else None


def fetch_arknights(http: Http) -> tuple[list[dict] | None, dict[str, Any]]:
    try:
        table = get_json(http, AK_TABLE, timeout=120)
        if not isinstance(table, dict):
            raise RuntimeError("character_table is not an object")
        characters = []
        for cid, row in table.items():
            if not isinstance(row, dict):
                continue
            if row.get("profession") not in AK_PROFESSIONS:
                continue
            if row.get("isNotObtainable"):
                continue
            name = str(row.get("name") or "").strip()
            if not name or name.startswith("预备干员") or name.startswith("盟约"):
                continue
            appellation = str(row.get("appellation") or "").strip()
            characters.append(
                base_character(
                    key=f"ak:{cid}",
                    name=name,
                    aliases=[appellation],
                    work="明日方舟",
                    game="arknights",
                    version=None,
                    kind="GAME",
                    status="EVERGREEN",
                    releaseDate=None,
                    artUrl=(
                        "https://raw.githubusercontent.com/yuanyan3060/ArknightsGameResource/"
                        f"main/portrait/{cid}_1.png"
                    ),
                    hairSampleUrl=(
                        "https://raw.githubusercontent.com/yuanyan3060/ArknightsGameResource/"
                        f"main/avatar/{cid}.png"
                    ),
                    source="arknights",
                )
            )
        meta = source_run("ok", AK_TABLE, count=len(characters))
        print(f"arknights: {len(characters)} operators")
        return characters, meta
    except Exception as error:
        print(f"arknights failed: {error}", file=sys.stderr)
        return None, source_run("failed", AK_TABLE, error=str(error))


def fetch_blue_archive(http: Http, today: date) -> tuple[list[dict] | None, dict[str, Any]]:
    try:
        payload = get_json(http, SCHALE_STUDENTS, timeout=60)
        students = list(payload.values()) if isinstance(payload, dict) else payload
        if not isinstance(students, list):
            raise RuntimeError("students payload is not a list or object")
        characters = []
        for stu in students:
            if not isinstance(stu, dict):
                continue
            name = str(stu.get("Name") or "").strip()
            sid = stu.get("Id")
            if not name or sid is None:
                continue
            flags = list(stu.get("IsReleased") or [])
            released_somewhere = any(bool(flag) for flag in flags)
            status = status_from_release("GAME", None, today, leaked=not released_somewhere)
            dev = str(stu.get("DevName") or "").strip()
            characters.append(
                base_character(
                    key=f"ba:{sid}",
                    name=name,
                    aliases=[dev],
                    work="碧蓝档案",
                    game="bluearchive",
                    version=None,
                    kind="GAME",
                    status=status,
                    releaseDate=None,
                    artUrl=f"https://schaledb.com/images/student/portrait/{sid}.webp",
                    hairSampleUrl=f"https://schaledb.com/images/student/icon/{sid}.webp",
                    source="bluearchive",
                )
            )
        meta = source_run("ok", SCHALE_STUDENTS, count=len(characters))
        print(f"bluearchive: {len(characters)} students")
        return characters, meta
    except Exception as error:
        print(f"bluearchive failed: {error}", file=sys.stderr)
        return None, source_run("failed", SCHALE_STUDENTS, error=str(error))


def file_title_name(title: str) -> str:
    name = title[5:] if title.startswith("File:") else title
    return " ".join(name.replace("_", " ").split())


def canonical_gender(value: str) -> str:
    return {"女": "Female", "男": "Male", "female": "Female", "male": "Male"}.get(value.strip().casefold(), "")


def endfield_query_names(operators: list[dict]) -> list[str]:
    counts: dict[str, int] = {}
    rows: list[tuple[str, str]] = []
    for row in operators:
        if not isinstance(row, dict):
            continue
        operator = str(row.get("Operator") or "").strip()
        if not operator:
            continue
        rows.append((operator, canonical_gender(str(row.get("Gender") or ""))))
        counts[operator] = counts.get(operator, 0) + 1
    names: list[str] = []
    for operator, gender in rows:
        for group in endfield_file_names(operator, gender, unique=counts[operator] == 1).values():
            names.extend(group)
    return list(dict.fromkeys(name for name in names if name))


def endfield_file_names(operator: str, gender: str, *, unique: bool) -> dict[str, list[str]]:
    """Wiki file titles. A shared name only uses the gendered file, so the two protagonists stay apart."""
    portrait: list[str] = []
    icon: list[str] = []
    splash: list[str] = []
    if unique:
        portrait.append(f"{operator}.png")
        icon.append(f"{operator} icon.png")
        splash.append(f"{operator} Splash Art.png")
    if gender:
        portrait.append(f"{operator} ({gender}).png")
        icon.append(f"{operator} ({gender}) icon.png")
        splash.append(f"{operator} ({gender}) Splash Art.png")
    return {"portrait": portrait, "icon": icon, "splash": splash}


def _remember_art(index: dict[str, list[dict]], key: str, entry: dict) -> None:
    text = key.strip().casefold()
    if not text:
        return
    rows = index.setdefault(text, [])
    identity = (entry.get("portrait"), entry.get("icon"), entry.get("splash"), entry.get("gender"))
    if any(
        (row.get("portrait"), row.get("icon"), row.get("splash"), row.get("gender")) == identity
        for row in rows
    ):
        return
    rows.append(entry)


def _file_record(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str) and value:
        return {"url": value, "width": 0, "height": 0}
    if isinstance(value, dict) and value.get("url"):
        return value
    return None


def _first_file_url(folded: dict[str, dict], names: list[str], *, min_edge: int = 0) -> str:
    for name in names:
        record = folded.get(name.casefold())
        if not record:
            continue
        edge = max(int(record.get("width") or 0), int(record.get("height") or 0))
        if min_edge and edge and edge < min_edge:
            continue
        url = str(record.get("url") or "")
        if url:
            return url
    return ""


def index_endfield_art(
    operators: list[dict],
    files: dict[str, Any],
    labels: dict[str, list[str]] | None = None,
) -> dict[str, list[dict]]:
    folded: dict[str, dict] = {}
    for title, value in files.items():
        record = _file_record(value)
        if record:
            folded[file_title_name(title).casefold()] = record
    counts: dict[str, int] = {}
    cleaned: list[tuple[str, str]] = []
    for row in operators:
        if not isinstance(row, dict):
            continue
        operator = str(row.get("Operator") or "").strip()
        if not operator:
            continue
        gender = canonical_gender(str(row.get("Gender") or ""))
        cleaned.append((operator, gender))
        counts[operator] = counts.get(operator, 0) + 1
    labels = labels or {}
    index: dict[str, list[dict]] = {}
    for operator, gender in cleaned:
        names = endfield_file_names(operator, gender, unique=counts[operator] == 1)
        entry = {
            "gender": gender,
            "portrait": _first_file_url(folded, names["portrait"], min_edge=ENDFIELD_PORTRAIT_MIN_EDGE),
            "icon": _first_file_url(folded, names["icon"]),
            "splash": _first_file_url(folded, names["splash"]),
        }
        if not entry["portrait"] and not entry["icon"] and not entry["splash"]:
            continue
        _remember_art(index, operator, entry)
        for label in labels.get(operator.casefold(), []):
            _remember_art(index, label, entry)
    return index


def pick_endfield_art(index: dict, english: str, gender: str, chinese: str = "") -> dict | None:
    """One name can be two protagonists. Do not guess which file."""
    for text in (english, chinese):
        key = text.strip().casefold()
        rows = list(index.get(key) or [])
        if not key or not rows:
            continue
        if len(rows) == 1:
            return rows[0]
        matched = [row for row in rows if gender and row.get("gender") == gender]
        if len(matched) == 1:
            return matched[0]
    return None


def stable_image_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if not parts.scheme or not parts.netloc or not parts.path:
        return ""
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def wiki_file_urls(http: Http, names: list[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for start in range(0, len(names), 40):
        chunk = names[start:start + 40]
        query = urllib.parse.urlencode(
            {
                "action": "query",
                "format": "json",
                "prop": "imageinfo",
                "iiprop": "url|size",
                "titles": "|".join(f"File:{name}" for name in chunk),
            }
        )
        payload = get_json(http, f"{ENDFIELD_WIKI}?{query}", timeout=60)
        pages = ((payload or {}).get("query") or {}).get("pages") or {}
        if not isinstance(pages, dict):
            continue
        for page in pages.values():
            if not isinstance(page, dict) or page.get("missing") is not None:
                continue
            info = page.get("imageinfo") or []
            if not info or not isinstance(info[0], dict):
                continue
            url = stable_image_url(str(info[0].get("url") or ""))
            title = str(page.get("title") or "")
            if url and title:
                found[title] = {
                    "url": url,
                    "width": int(info[0].get("width") or 0),
                    "height": int(info[0].get("height") or 0),
                }
    return found


def fetch_endfield_art_index(http: Http) -> dict[str, list[dict]]:
    query = urllib.parse.urlencode(
        {
            "action": "cargoquery",
            "format": "json",
            "tables": "Operators",
            "fields": "Operator,Gender",
            "limit": "500",
        }
    )
    payload = get_json(http, f"{ENDFIELD_WIKI}?{query}", timeout=60)
    cargo = payload.get("cargoquery") if isinstance(payload, dict) else None
    if not isinstance(cargo, list):
        raise RuntimeError("wiki operator list is not cargoquery")
    operators = []
    for item in cargo:
        title = item.get("title") if isinstance(item, dict) else None
        if isinstance(title, dict) and title.get("Operator"):
            operators.append(title)
    files = wiki_file_urls(http, endfield_query_names(operators))
    try:
        labels = fetch_endfield_cn_labels(http)
    except Exception as error:
        print(f"endfield wiki names failed: {error}", file=sys.stderr)
        labels = {}
    return index_endfield_art(operators, files, labels)


def fetch_endfield_cn_labels(http: Http) -> dict[str, list[str]]:
    """Bangumi often has no 英文名. The wiki's cn locale is the other join key."""
    query = urllib.parse.urlencode(
        {
            "action": "cargoquery",
            "format": "json",
            "tables": "Operators,Locales",
            "join_on": "Operators.Operator=Locales.Entity",
            "fields": "Operators.Operator=op,Locales.Value=name",
            "where": 'Locales.Lang="cn" AND Locales.Param="name"',
            "limit": "500",
        }
    )
    payload = get_json(http, f"{ENDFIELD_WIKI}?{query}", timeout=60)
    cargo = payload.get("cargoquery") if isinstance(payload, dict) else None
    if not isinstance(cargo, list):
        raise RuntimeError("wiki names are not cargoquery")
    labels: dict[str, list[str]] = {}
    for item in cargo:
        title = item.get("title") if isinstance(item, dict) else None
        if not isinstance(title, dict):
            continue
        operator = str(title.get("op") or "").strip().casefold()
        name = str(title.get("name") or "").strip()
        if not operator or not name or name in labels.get(operator, []):
            continue
        labels.setdefault(operator, []).append(name)
    return labels


def endfield_english_name(detail: dict[str, Any] | None) -> str:
    if not isinstance(detail, dict):
        return ""
    names = infobox_labeled(detail.get("infobox"), "英文名")
    return names[0] if names else ""


def endfield_gender(detail: dict[str, Any] | None) -> str:
    if not isinstance(detail, dict):
        return ""
    values = infobox_values(detail.get("infobox"), "性别")
    return canonical_gender(values[0]) if values else ""


def build_endfield_character(
    item: dict[str, Any],
    detail: dict[str, Any] | None,
    art_index: dict,
) -> dict[str, Any] | None:
    cid = item.get("id")
    if not isinstance(cid, int):
        return None
    identity = bangumi_identity(item, detail)
    if identity is None:
        return None
    name, aliases = identity
    images = item.get("images") if isinstance(item.get("images"), dict) else {}
    art = usable_image(images.get("large"))
    sample = usable_image(images.get("grid") or images.get("medium"))
    picked = pick_endfield_art(
        art_index,
        endfield_english_name(detail),
        endfield_gender(detail),
        name,
    )
    if picked:
        # Portrait, then splash, then the Bangumi poster. The icon is the hair sample.
        art = picked.get("portrait") or picked.get("splash") or art
        if picked.get("icon"):
            sample = picked["icon"]
    return base_character(
        key=f"bgm:{cid}",
        name=name,
        aliases=aliases,
        work=ENDFIELD_WORK,
        game="endfield",
        version=None,
        kind="GAME",
        status="EVERGREEN",
        releaseDate=None,
        artUrl=art,
        hairSampleUrl=sample,
        source="endfield",
    )


def fetch_endfield(http: Http) -> tuple[list[dict] | None, dict[str, Any]]:
    endpoint = f"{BANGUMI}/v0/subjects/{ENDFIELD_SUBJECT}/characters"
    try:
        listed = get_json(http, endpoint, timeout=40)
        if not isinstance(listed, list):
            raise RuntimeError("character list is not a list")
    except Exception as error:
        print(f"endfield failed: {error}", file=sys.stderr)
        return None, source_run("failed", endpoint, error=str(error))
    try:
        art_index = fetch_endfield_art_index(http)
    except Exception as error:
        print(f"endfield wiki art failed: {error}", file=sys.stderr)
        art_index = {}
    # type 1 only. type 4 is an organization. 闲角 stays: 大山 is a named person.
    people = [row for row in listed if isinstance(row, dict) and is_person(row)]
    characters: dict[str, dict] = {}
    detail_errors = 0
    for item in people:
        cid = item.get("id")
        detail = None
        if isinstance(cid, int):
            try:
                loaded = get_json(http, f"{BANGUMI}/v0/characters/{cid}", timeout=40)
                detail = loaded if isinstance(loaded, dict) else None
            except Exception as error:
                detail_errors += 1
                print(f"endfield character {cid} detail failed: {error}", file=sys.stderr)
        row = build_endfield_character(item, detail, art_index)
        if row is None:
            continue
        characters[row["key"]] = row
    portraits = 0
    splashes = 0
    icons = 0
    for row in characters.values():
        art_url = str(row.get("artUrl") or "")
        if "Splash_Art" in art_url:
            splashes += 1
        elif "endfield.wiki.gg" in art_url:
            portraits += 1
        if str(row.get("hairSampleUrl") or "").endswith("icon.png"):
            icons += 1
    error = f"{detail_errors} character details failed" if detail_errors else None
    meta = source_run(
        "partial" if detail_errors else "ok",
        endpoint,
        count=len(characters),
        error=error,
        extra={
            "subjectId": ENDFIELD_SUBJECT,
            "wikiArt": portraits + splashes,
            "wikiPortrait": portraits,
            "wikiSplash": splashes,
            "wikiIcon": icons,
            "listed": len(listed),
        },
    )
    print(f"endfield: {len(characters)} characters, {portraits} portraits, {splashes} splash art, {icons} icons")
    return list(characters.values()), meta


def fetch_bangumi(http: Http, today: date) -> tuple[list[dict] | None, dict[str, Any]]:
    endpoint = f"{BANGUMI}/calendar"
    errors: list[str] = []
    shows: dict[int, dict[str, Any]] = {}
    try:
        calendar = get_json(http, endpoint, timeout=40)
        start, end, _, _ = season_bounds(today)
        if not isinstance(calendar, list):
            raise RuntimeError("calendar is not a list")
        airing = 0
        kept_shows = 0
        for day in calendar:
            for item in day.get("items") or []:
                if not isinstance(item, dict):
                    continue
                airing += 1
                sid = item.get("id")
                premier = parse_day(item.get("air_date") or item.get("date"))
                if not isinstance(sid, int) or premier is None:
                    continue
                if item.get("nsfw"):
                    continue
                if not (start <= premier < end):
                    continue
                kept_shows += 1
                shows[sid] = {
                    "id": sid,
                    "name": clean_label(item.get("name_cn") or item.get("name") or ""),
                    "release": premier,
                    "season": "current",
                }
        print(f"bangumi calendar: {airing} airing, {kept_shows} in {start.isoformat()}..{end.isoformat()}")
    except Exception as error:
        print(f"bangumi calendar failed: {error}", file=sys.stderr)
        return None, source_run("failed", endpoint, error=str(error))

    _, _, nxt_start, nxt_end = season_bounds(today)
    try:
        upcoming = list_subjects_between(http, nxt_start, nxt_end)
        added = 0
        for item in upcoming:
            sid = item.get("id")
            premier = parse_day(item.get("date"))
            if not isinstance(sid, int) or premier is None or sid in shows:
                continue
            if item.get("nsfw"):
                continue
            shows[sid] = {
                "id": sid,
                "name": clean_label(item.get("name_cn") or item.get("nameCN") or item.get("name") or ""),
                "release": premier,
                "season": "next",
            }
            added += 1
        print(f"bangumi next season {nxt_start.isoformat()}..{nxt_end.isoformat()}: {added}")
    except Exception as error:
        errors.append(f"next season: {error}")
        print(f"bangumi next season failed: {error}", file=sys.stderr)

    current_rows = [row for row in shows.values() if row.get("season") == "current"]
    next_rows = [row for row in shows.values() if row.get("season") == "next"]
    heats = work_heats(
        [row["id"] for row in shows.values() if isinstance(row.get("id"), int)],
        lambda sid: get_json(http, f"{BANGUMI}/v0/subjects/{sid}", timeout=40),
    )
    kept = select_shows(current_rows, next_rows, heats)
    kept_current = sum(1 for row in kept if row.get("season") == "current")
    kept_next = sum(1 for row in kept if row.get("season") == "next")
    print(
        f"bangumi keep {len(kept)} works "
        f"(candidates {len(shows)}, current {kept_current}, next {kept_next})"
    )
    for show in kept:
        print(f"bangumi work {show.get('season')} {show.get('workHeat')} {show.get('name')}")

    characters: dict[str, dict] = {}
    empty_shows = 0
    show_errors = 0
    for show in kept:
        try:
            listed = get_json(http, f"{BANGUMI}/v0/subjects/{show['id']}/characters", timeout=40)
        except Exception as error:
            show_errors += 1
            print(f"bangumi characters {show['id']} failed: {error}", file=sys.stderr)
            continue
        if not isinstance(listed, list) or not listed:
            empty_shows += 1
            continue
        picked = pick_cast(listed)
        if not picked:
            empty_shows += 1
            continue
        for item in picked:
            row = bangumi_character(http, item, show, today)
            if row is None:
                continue
            current = characters.get(row["key"])
            characters[row["key"]] = row if current is None else prefer_character(current, row)
    if show_errors and not characters:
        return None, source_run("failed", endpoint, error=f"{show_errors} character lists failed")
    if show_errors:
        errors.append(f"{show_errors} character lists failed")
    status = "partial" if errors else "ok"
    meta = source_run(
        status,
        endpoint,
        count=len(characters),
        error="; ".join(errors) or None,
        extra={
            "selection": BANGUMI_SELECTION,
            "shows": len(shows),
            "keptWorks": len(kept),
            "keptCurrent": kept_current,
            "keptNext": kept_next,
            "showsWithoutCast": empty_shows,
            "nextSeason": f"{nxt_start.isoformat()}/{nxt_end.isoformat()}",
        },
    )
    print(
        f"bangumi: {len(characters)} characters from {len(kept)} works "
        f"({empty_shows} without a kept cast)"
    )
    return list(characters.values()), meta


def list_subjects_between(http: Http, start: date, end: date) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    offset = 0
    older_run = 0
    limit = 50
    while offset < 2000:
        url = f"{BANGUMI}/v0/subjects?type=2&sort=date&limit={limit}&offset={offset}"
        payload = get_json(http, url, timeout=40)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            break
        for item in data:
            if not isinstance(item, dict):
                continue
            premier = parse_day(item.get("date"))
            if premier is None:
                continue
            if start <= premier < end:
                found.append(item)
                older_run = 0
            elif premier < start:
                older_run += 1
            else:
                older_run = 0
        if older_run >= 40:
            break
        offset += len(data)
        if len(data) < limit:
            break
    return found


def is_person(row: dict[str, Any] | None) -> bool:
    """Bangumi character type: 1 person, 2 mecha, 3 ship, 4 organization. Missing counts as person."""
    if not isinstance(row, dict):
        return True
    kind = row.get("type")
    return kind is None or kind == BANGUMI_PERSON


def pick_cast(listed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mains = [row for row in listed if row.get("relation") == "主角" and is_person(row)]
    return mains[:MAIN_CAST_KEEP]


def bangumi_identity(item: dict[str, Any], detail: dict[str, Any] | None) -> tuple[str, list[str]] | None:
    """Simplified Chinese name, then the same alias list the anime source keeps."""
    if not is_person(item) or not is_person(detail):
        return None
    list_name = clean_label(item.get("name"))
    original = list_name
    name = list_name
    aliases = [list_name] if list_name else []
    cn_name = ""
    if isinstance(detail, dict):
        infobox = detail.get("infobox")
        cn_names = infobox_values(infobox, "简体中文名")
        if cn_names:
            cn_name = cn_names[0]
            name = cn_name
        for key in ("别名", "英文名", "日文名", "纯假名", "罗马字", "第二中文名"):
            aliases.extend(infobox_values(infobox, key))
        if detail.get("name"):
            original = clean_label(detail.get("name"))
            aliases.append(original)
    if not name:
        return None
    if numeric_only_name(cn_name or name, original):
        return None
    return name, aliases


def build_bangumi_character(
    item: dict[str, Any],
    show: dict[str, Any],
    today: date,
    detail: dict[str, Any] | None,
) -> dict[str, Any] | None:
    cid = item.get("id")
    if not isinstance(cid, int):
        return None
    identity = bangumi_identity(item, detail)
    if identity is None:
        return None
    name, aliases = identity
    images = item.get("images") or {}
    try:
        work_heat = int(show.get("workHeat") or 0)
    except (TypeError, ValueError):
        work_heat = 0
    release: date = show["release"]
    status = status_from_release("ANIME", release, today)
    return base_character(
        key=f"bgm:{cid}",
        name=name,
        aliases=aliases,
        work=clean_label(show.get("name")),
        game="anime",
        version=None,
        kind="ANIME",
        status=status,
        releaseDate=release.isoformat(),
        artUrl=usable_image(images.get("large") if isinstance(images, dict) else None),
        hairSampleUrl=usable_image(
            (images.get("grid") or images.get("medium")) if isinstance(images, dict) else None
        ),
        bangumiSubjectId=show["id"],
        workHeat=work_heat,
        source="bangumi",
    )


def bangumi_character(http: Http, item: dict[str, Any], show: dict[str, Any], today: date) -> dict[str, Any] | None:
    cid = item.get("id")
    detail = None
    if isinstance(cid, int):
        try:
            loaded = get_json(http, f"{BANGUMI}/v0/characters/{cid}", timeout=40)
            detail = loaded if isinstance(loaded, dict) else None
        except Exception as error:
            print(f"bangumi character {cid} detail failed: {error}", file=sys.stderr)
            detail = None
    return build_bangumi_character(item, show, today, detail)


def infobox_values(infobox: Any, key: str) -> list[str]:
    found: list[str] = []
    if not isinstance(infobox, list):
        return found
    for item in infobox:
        if not isinstance(item, dict) or item.get("key") != key:
            continue
        value = item.get("value")
        if isinstance(value, str):
            found.append(value.strip())
        elif isinstance(value, list):
            for part in value:
                if isinstance(part, str):
                    found.append(part.strip())
                elif isinstance(part, dict):
                    text = part.get("v") or part.get("value") or ""
                    if str(text).strip():
                        found.append(str(text).strip())
    return [clean_label(text) for text in found if text and clean_label(text)]


def infobox_labeled(infobox: Any, label: str) -> list[str]:
    """Top-level key or a nested `{k, v}` under 别名. Bangumi uses both."""
    found = list(infobox_values(infobox, label))
    if not isinstance(infobox, list):
        return found
    for item in infobox:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if not isinstance(value, list):
            continue
        for part in value:
            if isinstance(part, dict) and part.get("k") == label:
                text = part.get("v") or part.get("value") or ""
                if str(text).strip():
                    found.append(str(text).strip())
    return [clean_label(text) for text in found if text and clean_label(text)]


def prefer_character(left: dict, right: dict) -> dict:
    rank = {"LEAKED": 0, "LIVE": 1, "EVERGREEN": 2}
    winner = left
    loser = right
    if rank.get(right["status"], 9) < rank.get(left["status"], 9):
        winner, loser = right, left
    elif rank.get(right["status"], 9) == rank.get(left["status"], 9):
        if (right.get("releaseDate") or "") > (left.get("releaseDate") or ""):
            winner, loser = right, left
    winner = dict(winner)
    winner["aliases"] = dedupe_aliases(winner["name"], list(winner.get("aliases") or []) + list(loser.get("aliases") or []))
    return winner


def prepare_image(image: Image.Image, max_edge: int = 768) -> Image.Image:
    rgba = image.convert("RGBA")
    width, height = rgba.size
    longest = max(width, height)
    if longest > max_edge:
        scale = max_edge / float(longest)
        rgba = rgba.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS)
    return rgba


def read_image_bytes(http: Http, url: str) -> bytes:
    digest = hashlib.sha1(url.encode()).hexdigest()
    path = CACHE / "img" / digest
    if path.exists() and path.stat().st_size > 32:
        return path.read_bytes()
    data = http.bytes(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def load_image(http: Http, url: str) -> Image.Image:
    data = read_image_bytes(http, url)
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def inspect_image(http: Http, url: str) -> tuple[str, int | None] | None:
    """Art kind, and the portrait primary when the image is a portrait."""
    try:
        image = prepare_image(load_image(http, url))
    except (PermanentError, TransientError):
        return None
    pixels, width, height = pixels_from_rgba(image)
    kind = classify_art(pixels, width, height).kind
    if kind != PORTRAIT:
        return kind, None
    extracted = extract_hair(pixels, width, height)
    if extracted is None:
        return kind, None
    return kind, extracted.primary


def _store_hair(character: dict, hair: dict) -> dict:
    character["hair"] = hair
    return character


def extract_one(http: Http, character: dict) -> dict[str, Any]:
    sample_urls = []
    if character.get("hairSampleUrl"):
        sample_urls.append(character["hairSampleUrl"])
    elif character.get("artUrl"):
        sample_urls.append(character["artUrl"])
    if not sample_urls and not character.get("artUrl"):
        character["artKind"] = UNKNOWN
        return _store_hair(character, {
            "primary": None,
            "secondary": None,
            "confidence": "UNKNOWN",
            "sampledFrom": None,
        })
    display_url = character.get("artUrl")
    last_permanent: str | None = None
    for url in sample_urls:
        try:
            raw = read_image_bytes(http, url)
            image = Image.open(io.BytesIO(raw))
            image.load()
            image = prepare_image(image)
        except PermanentError as error:
            last_permanent = str(error)
            if display_url and display_url != url:
                sample_urls.append(display_url)
            continue
        except TransientError as error:
            return _store_hair(character, {
                "primary": None,
                "secondary": None,
                "confidence": "UNKNOWN",
                "sampledFrom": None,
                "retry": True,
                "error": str(error),
            })
        sample_sha1 = hashlib.sha1(raw).hexdigest()
        pixels, width, height = pixels_from_rgba(image)
        verdict = classify_art(pixels, width, height)
        display_kind = verdict.kind
        display_color = None
        if display_url and display_url != url:
            inspected = inspect_image(http, display_url)
            if inspected is not None:
                display_kind, display_color = inspected
        character["artKind"] = display_kind
        # A poster sample has no trustworthy hair, unless the display is a separate portrait
        # and this file is only the head shot. Bangumi grids stay low: their display is a poster too.
        if verdict.kind == POSTER and display_kind != PORTRAIT:
            return _store_hair(character, {
                "primary": None,
                "secondary": None,
                "confidence": "LOW",
                "sampledFrom": url,
                "sampleSha1": sample_sha1,
            })
        extracted = extract_hair(pixels, width, height)
        if extracted is None:
            last_permanent = "no hair color"
            if display_url and display_url not in sample_urls:
                sample_urls.append(display_url)
            continue
        character["artKind"] = display_kind
        character["hair"] = build_checked_hair(
            extracted.primary,
            extracted.secondary,
            extracted.confidence,
            url,
            display_color,
            display_is_portrait=display_kind == PORTRAIT and bool(display_url) and display_url != url,
            sample_sha1=sample_sha1,
        )
        return character
    character["artKind"] = character.get("artKind") or UNKNOWN
    return _store_hair(character, {
        "primary": None,
        "secondary": None,
        "confidence": "UNKNOWN",
        "sampledFrom": None,
        "error": last_permanent or "image unavailable",
    })


def apply_hair(characters: list[dict], limit: int) -> dict[str, int]:
    pending = []
    for row in characters:
        hair = row.get("hair") or {}
        if hair.get("retry") or "hair" not in row:
            if row.get("artUrl") or row.get("hairSampleUrl"):
                pending.append(row)
            else:
                row["artKind"] = UNKNOWN
                row["hair"] = {
                    "primary": None,
                    "secondary": None,
                    "confidence": "UNKNOWN",
                    "sampledFrom": None,
                }
    if limit > 0:
        pending = pending[:limit]
    http = Http(USER_AGENT, min_interval=0.0)
    stats = {"attempted": 0, "ok": 0, "poster": 0, "failed": 0}
    started = time.monotonic()
    print(f"hair: extracting {len(pending)}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(extract_one, http, row) for row in pending]
        for index, future in enumerate(as_completed(futures), start=1):
            stats["attempted"] += 1
            try:
                row = future.result()
            except Exception as error:
                stats["failed"] += 1
                print(f"hair worker failed: {error}", file=sys.stderr)
                continue
            hair = row.get("hair") or {}
            if hair.get("primary"):
                stats["ok"] += 1
            elif row.get("artKind") == POSTER:
                stats["poster"] += 1
            else:
                stats["failed"] += 1
            if index % 50 == 0 or index == len(futures):
                elapsed = time.monotonic() - started
                print(f"hair {index}/{len(futures)} ok={stats['ok']} poster={stats['poster']} failed={stats['failed']} {elapsed:.0f}s")
    stats["seconds"] = round(time.monotonic() - started, 1)
    return stats


def settle_unattempted(characters: list[dict]) -> None:
    for row in characters:
        if "hair" in row:
            continue
        row.setdefault("artKind", UNKNOWN)
        retry = bool(row.get("artUrl") or row.get("hairSampleUrl"))
        row["hair"] = {
            "primary": None,
            "secondary": None,
            "confidence": "UNKNOWN",
            "sampledFrom": None,
        }
        if retry:
            row["hair"]["retry"] = True


def summarize(characters: list[dict]) -> dict[str, Any]:
    status_counts = {"LEAKED": 0, "LIVE": 0, "EVERGREEN": 0}
    games: dict[str, int] = {}
    hair = {"ok": 0, "poster": 0, "failed": 0, "pending": 0}
    baseline = 0
    leaked_names = []
    for row in characters:
        status_counts[row.get("status") or "?"] = status_counts.get(row.get("status") or "?", 0) + 1
        games[row.get("game") or "?"] = games.get(row.get("game") or "?", 0) + 1
        if row.get("baseline"):
            baseline += 1
        if row.get("status") == "LEAKED":
            leaked_names.append(f"{row.get('name')} ({row.get('work')})")
        state = row.get("hair") or {}
        if state.get("retry"):
            hair["pending"] += 1
        elif state.get("primary"):
            hair["ok"] += 1
        elif row.get("artKind") == POSTER and state.get("confidence") == "LOW":
            hair["poster"] += 1
        else:
            hair["failed"] += 1
    return {
        "total": len(characters),
        "baseline": baseline,
        "notBaseline": len(characters) - baseline,
        "status": status_counts,
        "games": games,
        "hair": hair,
        "leaked": leaked_names,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Build the wig-radar character catalog")
    parser.add_argument("--out", type=Path, default=ROOT / "catalog.json")
    parser.add_argument(
        "--only",
        default="",
        help="Comma-separated sources to refresh. Other sources stay as stored.",
    )
    parser.add_argument("--hair-limit", type=int, default=0, help="Max images to extract this run; 0 means all pending")
    parser.add_argument("--skip-hair", action="store_true", help="Write the catalog without downloading images")
    parser.add_argument(
        "--auto-tag-limit",
        type=int,
        default=AUTO_TAG_BATCH_LIMIT,
        help="Max Danbooru character lookups this run",
    )
    parser.add_argument("--skip-auto-tags", action="store_true", help="Keep previous autoTags and do not call Danbooru")
    args = parser.parse_args()
    started = time.monotonic()
    today = beijing_today()
    today_date = date.fromisoformat(today)
    previous = load_json(args.out)
    http = Http(USER_AGENT, min_interval=0.15)
    kuro_http = Http(USER_AGENT, min_interval=0.25)
    bangumi_http = Http(USER_AGENT, min_interval=0.28)

    only = parse_only(args.only)

    def selected(name: str) -> bool:
        return not only or name in only

    fetched: dict[str, list[dict] | None] = {}
    sources: dict[str, Any] = {}
    if only and isinstance(previous, dict) and isinstance(previous.get("sources"), dict):
        for name, meta in previous["sources"].items():
            if name not in only:
                sources[name] = meta

    if selected("nanoka-gi") or selected("nanoka-hsr") or selected("nanoka-zzz") or selected("wuwa"):
        if selected("nanoka-gi"):
            gi_rows, sources["nanoka-gi"] = fetch_nanoka_game(http, "gi", "genshin", "原神")
            fetched["nanoka-gi"] = shrink_guard("nanoka-gi", gi_rows, previous)
            if fetched["nanoka-gi"] is None and gi_rows is not None:
                sources["nanoka-gi"]["status"] = "failed"
                sources["nanoka-gi"]["error"] = "result shrank; kept previous"
        if selected("nanoka-hsr"):
            hsr_rows, sources["nanoka-hsr"] = fetch_nanoka_game(http, "hsr", "hsr", "崩坏：星穹铁道")
            fetched["nanoka-hsr"] = shrink_guard("nanoka-hsr", hsr_rows, previous)
            if fetched["nanoka-hsr"] is None and hsr_rows is not None:
                sources["nanoka-hsr"]["status"] = "failed"
                sources["nanoka-hsr"]["error"] = "result shrank; kept previous"
        if selected("nanoka-zzz"):
            zzz_rows, sources["nanoka-zzz"] = fetch_nanoka_game(http, "zzz", "zzz", "绝区零")
            fetched["nanoka-zzz"] = shrink_guard("nanoka-zzz", zzz_rows, previous)
            if fetched["nanoka-zzz"] is None and zzz_rows is not None:
                sources["nanoka-zzz"]["status"] = "failed"
                sources["nanoka-zzz"]["error"] = "result shrank; kept previous"
        if selected("wuwa"):
            ww_index, ww_index_meta = fetch_nanoka_game(http, "ww", "wuwa", "鸣潮")
            wuwa_rows, sources["wuwa"] = fetch_wuwa(kuro_http, ww_index, today_date)
            sources["wuwa"]["nanokaIndex"] = {
                "status": ww_index_meta.get("status"),
                "count": ww_index_meta.get("count"),
                "error": ww_index_meta.get("error"),
                "endpoint": ww_index_meta.get("endpoint"),
            }
            fetched["wuwa"] = shrink_guard("wuwa", wuwa_rows, previous)
            if fetched["wuwa"] is None and wuwa_rows is not None:
                sources["wuwa"]["status"] = "failed"
                sources["wuwa"]["error"] = "result shrank; kept previous"

    if selected("arknights"):
        ak_rows, sources["arknights"] = fetch_arknights(http)
        fetched["arknights"] = shrink_guard("arknights", ak_rows, previous)
    if selected("endfield"):
        endfield_rows, sources["endfield"] = fetch_endfield(bangumi_http)
        fetched["endfield"] = shrink_guard("endfield", endfield_rows, previous)
        if fetched["endfield"] is None and endfield_rows is not None:
            sources["endfield"]["status"] = "failed"
            sources["endfield"]["error"] = "result shrank; kept previous"
    if selected("bluearchive"):
        ba_rows, sources["bluearchive"] = fetch_blue_archive(http, today_date)
        fetched["bluearchive"] = shrink_guard("bluearchive", ba_rows, previous)
    if selected("bangumi"):
        bgm_rows, sources["bangumi"] = fetch_bangumi(bangumi_http, today_date)
        fetched["bangumi"] = shrink_guard("bangumi", bgm_rows, previous, selection=BANGUMI_SELECTION)
        if fetched["bangumi"] is None and bgm_rows is not None:
            sources["bangumi"]["status"] = "failed"
            sources["bangumi"]["error"] = (sources["bangumi"].get("error") or "") + "; result shrank; kept previous"

    characters = merge_characters(previous, fetched, today)
    for source, rows in fetched.items():
        if rows is None:
            kept = sum(1 for row in characters if row.get("source") == source)
            sources[source]["count"] = kept
            sources[source]["keptPrevious"] = True
    settle_unattempted(characters)
    for row in characters:
        row["searchKeys"] = search_keys(str(row.get("name") or ""), list(row.get("aliases") or []))
    restore_previous_auto_tags(characters, previous)
    auto_summary: dict[str, Any] = {"status": "skipped", "attempted": 0}
    if not args.skip_auto_tags:
        try:
            auto_summary = refresh_auto_tags(
                characters,
                previous,
                DanbooruClient(USER_AGENT),
                today,
                limit=args.auto_tag_limit,
            )
        except Exception as error:
            print(f"autoTags failed: {error}; keeping previous autoTags", file=sys.stderr)
            restore_previous_auto_tags(characters, previous)
            auto_summary = {"status": "skipped", "error": str(error), "attempted": 0}
    hair_stats = {"attempted": 0, "ok": 0, "poster": 0, "failed": 0, "seconds": 0, "skipped": True}
    if not args.skip_hair:
        hair_stats = apply_hair(characters, args.hair_limit)
    duplicate_groups = flag_duplicate_samples(characters)
    if duplicate_groups:
        print(f"hair: {len(duplicate_groups)} sample-duplicate groups")
        for keys in duplicate_groups:
            print("hair sample-duplicate: " + ", ".join(keys))
    characters.sort(key=lambda row: (str(row.get("game") or ""), str(row.get("name") or ""), str(row.get("key") or "")))
    stats = summarize(characters)
    stats["hairRun"] = hair_stats
    stats["autoTags"] = auto_summary
    stats["elapsedSeconds"] = round(time.monotonic() - started, 1)
    document = {
        "schemaVersion": 1,
        "generatedAt": iso_now(),
        "sources": sources,
        "characters": characters,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix(args.out.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.out)
    print(f"wrote {args.out}")
    print("CATALOG_STATS " + json.dumps(stats, ensure_ascii=False))
    failed = [name for name, row in sources.items() if row.get("status") == "failed"]
    return 1 if failed and not characters else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"fatal: {error}", file=sys.stderr)
        raise
