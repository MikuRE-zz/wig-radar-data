"""Pure catalog rules: satellite day, status window, and hair reuse.

Kept separate from the network fetch so the first-import baseline can be tested
without calling any data source.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone

from pypinyin import Style, pinyin

from scripts.haircolor import delta_e, hex_color

BEIJING = timezone(timedelta(hours=8))
GAME_WINDOW_DAYS = 40
ANIME_WINDOW_DAYS = 21
SEARCH_KEY_COMBOS = 16

RUBY_MARKUP = re.compile(r"\{RUBY_[BE]#[^}]*\}")


def beijing_today(now: datetime | None = None) -> str:
    current = now or datetime.now(BEIJING)
    return current.astimezone(BEIJING).date().isoformat()


def strip_ruby(value: str | None) -> str:
    return RUBY_MARKUP.sub("", value or "").strip()


def dedupe_aliases(name: str, values: list[str]) -> list[str]:
    seen = {name.strip().casefold()} if name else set()
    aliases: list[str] = []
    for value in values:
        text = strip_ruby(value).strip()
        if not text or _junk_alias(text):
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        aliases.append(text)
    return aliases


def _has_han(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf" for ch in text)


def pinyin_keys(text: str, limit: int = SEARCH_KEY_COMBOS) -> list[str]:
    """Full pinyin and initials for every reading combination, capped at `limit` each.

    Heteronyms matter: 洛克茜 is luokexi, 长离 is changli, 重云 is chongyun, and
    the default reading gets all three wrong.
    """
    if not _has_han(text):
        return []
    syllables: list[list[str]] = []
    for ch in text:
        if _has_han(ch):
            readings = [r for r in pinyin(ch, style=Style.NORMAL, heteronym=True)[0] if r.isascii()]
            syllables.append(sorted(set(readings)) or [ch])
        elif ch.isascii() and ch.isalnum():
            syllables.append([ch.lower()])
    combos: list[list[str]] = [[]]
    for options in syllables:
        combos = [prefix + [option] for prefix in combos for option in options][:limit]
    full = ["".join(parts) for parts in combos]
    initials = ["".join(part[0] for part in parts if part) for parts in combos]
    return list(dict.fromkeys(full + initials))


def search_keys(name: str, aliases: list[str]) -> list[str]:
    keys: list[str] = []
    for text in [name, *aliases]:
        keys.extend(pinyin_keys(str(text or "")))
    return list(dict.fromkeys(keys))


def _junk_alias(text: str) -> bool:
    lowered = text.casefold()
    if "an error occurred" in lowered or "customer service" in lowered:
        return True
    return len(text) > 80


def status_from_release(
    kind: str,
    release: date | None,
    today: date,
    *,
    leaked: bool = False,
) -> str:
    """LEAKED / LIVE / EVERGREEN.

    A source that already knows the character is unreleased wins. Otherwise the
    window is 40 days for games and 21 days for anime, measured from the
    release or premiere date. Baseline characters with no date stay evergreen
    so the first import does not invent a few hundred satellites.
    """
    if leaked:
        return "LEAKED"
    if release is None:
        return "EVERGREEN"
    delta = (today - release).days
    if delta < 0:
        return "LEAKED"
    window = ANIME_WINDOW_DAYS if kind == "ANIME" else GAME_WINDOW_DAYS
    if delta <= window:
        return "LIVE"
    return "EVERGREEN"


def hair_is_settled(character: dict) -> bool:
    hair = character.get("hair") or {}
    if hair.get("retry"):
        return False
    if hair.get("primary"):
        return True
    # Shared avatar bytes: neither character keeps a color, and the row is done.
    if hair.get("reason") == "sample-duplicate":
        return True
    if character.get("artKind") == "POSTER" and hair.get("confidence") == "LOW":
        return True
    if hair.get("error"):
        return True
    if hair.get("confidence") == "UNKNOWN" and not character.get("artUrl") and not character.get("hairSampleUrl"):
        return True
    return False


def hair_can_reuse(previous: dict, fresh: dict) -> bool:
    if (previous.get("artUrl"), previous.get("hairSampleUrl")) != (
        fresh.get("artUrl"),
        fresh.get("hairSampleUrl"),
    ):
        return False
    return hair_is_settled(previous)


def build_checked_hair(
    primary: int,
    secondary: int | None,
    confidence: str,
    sampled_from: str,
    display_color: int | None,
    *,
    display_is_portrait: bool,
    sample_sha1: str | None = None,
) -> dict:
    """Hair record for one sample image.

    A portrait/sample color gap is recorded for diagnosis only. It does not
    change confidence; a shared sample file is handled later, across the catalog.
    """
    hair = {
        "primary": hex_color(primary),
        "secondary": None if secondary is None else hex_color(secondary),
        "confidence": confidence,
        "sampledFrom": sampled_from,
    }
    if sample_sha1:
        hair["sampleSha1"] = sample_sha1
    if display_is_portrait and display_color is not None:
        hair["sampleColor"] = hex_color(primary)
        hair["displayColor"] = hex_color(display_color)
        hair["deltaE"] = round(delta_e(primary, display_color), 1)
    return hair


def flag_duplicate_samples(characters: list[dict]) -> list[list[str]]:
    """Drop the primary when two keys sampled the same image bytes.

    Rows with no sampleSha1 are left alone: older extracts were not hashed.
    The group keeps no color, because the file does not say which face is real.
    """
    groups: dict[str, list[dict]] = {}
    for row in characters:
        if not isinstance(row, dict) or not row.get("key"):
            continue
        hair = row.get("hair")
        if not isinstance(hair, dict):
            continue
        digest = hair.get("sampleSha1")
        if not isinstance(digest, str) or not digest:
            continue
        groups.setdefault(digest, []).append(row)
    flagged: list[list[str]] = []
    for rows in groups.values():
        keys = list(dict.fromkeys(str(row.get("key")) for row in rows))
        if len(keys) < 2:
            continue
        flagged.append(keys)
        for row in rows:
            hair = dict(row.get("hair") or {})
            hair["primary"] = None
            hair["secondary"] = None
            hair["confidence"] = "LOW"
            hair["reason"] = "sample-duplicate"
            row["hair"] = hair
    return flagged


def merge_characters(
    previous: dict | None,
    fetched: dict[str, list[dict] | None],
    today: str,
) -> list[dict]:
    """Merge a fetch into the previous catalog.

    `fetched[source] is None` means that source failed: keep its previous
    characters and do not treat them as disappeared. On the first import
    (`previous is None`) every character is baseline. The first time a source
    appears it is the same: every character from that source is baseline.
    A key that shows up later is not baseline, and an existing firstSeenAt
    is never rewritten.
    """
    prev_list: list[dict] = []
    if previous and isinstance(previous.get("characters"), list):
        prev_list = [
            row for row in previous["characters"] if isinstance(row, dict) and row.get("key")
        ]
    first_import = previous is None
    prev_by_key = {row["key"]: row for row in prev_list}
    prev_by_source: dict[str, list[dict]] = {}
    for row in prev_list:
        prev_by_source.setdefault(str(row.get("source") or ""), []).append(row)

    merged: dict[str, dict] = {}
    for source, rows in fetched.items():
        if rows is None:
            for old in prev_by_source.get(source, []):
                merged[old["key"]] = old
            continue
        source_is_new = source not in prev_by_source
        for fresh in rows:
            row = dict(fresh)
            row["source"] = source
            old = prev_by_key.get(row["key"])
            if old:
                row["firstSeenAt"] = old.get("firstSeenAt") or today
                row["baseline"] = bool(old.get("baseline"))
                if hair_can_reuse(old, row):
                    row["hair"] = old.get("hair")
                    if old.get("artKind"):
                        row["artKind"] = old.get("artKind")
            else:
                row["firstSeenAt"] = today
                row["baseline"] = first_import or source_is_new
            merged[row["key"]] = row

    for source, rows in prev_by_source.items():
        if source not in fetched:
            for old in rows:
                merged.setdefault(old["key"], old)
    return list(merged.values())


# Danbooru copyright tag names. The highest co-occurring copyright has to be
# one of these; a near-tie (series + game) still counts as the top.
GAME_COPYRIGHT = {
    "genshin": "genshin_impact",
    "hsr": "honkai:_star_rail",
    "zzz": "zenless_zone_zero",
    "wuwa": "wuthering_waves",
    "arknights": "arknights",
    "endfield": "arknights:_endfield",
    "bluearchive": "blue_archive",
}
AUTO_TAG_VERSION = "danbooru-v1"
AUTO_TAG_TTL_DAYS = 60
AUTO_TAG_BATCH_LIMIT = 80
AUTO_TAG_BUDGET_SECONDS = 6 * 60
AUTO_TAG_MIN_POSTS = 30
# Canonical hair tags in the reference set sit at 0.34 and up. Unrelated
# tags (stray ears, a costume braid) stay under 0.15. 0.30 is the gap.
TAG_FREQUENCY_MIN = 0.30
COPYRIGHT_TIE_EPSILON = 0.005
AUTO_TAG_STATUSES = ("ok", "not-found", "too-few-posts", "work-mismatch", "error")
FRESH_AUTO_TAG_STATUSES = ("ok", "not-found", "too-few-posts", "work-mismatch")

STRUCTURE_TAG_ORDER = (
    "超长（80cm+）",
    "长",
    "中长",
    "短发",
    "大发量",
    "渐变",
    "挑染",
    "内层异色",
    "多段异色",
    "编发",
    "盘发",
    "双马尾接片",
    "呆毛",
    "刘海定位",
    "硬定型（运输易塌）",
    "兽耳等配件",
)

_EXPLICIT_RAW = {
    "absurdly_long_hair",
    "very_long_hair",
    "long_hair",
    "medium_hair",
    "short_hair",
    "big_hair",
    "gradient_hair",
    "streaked_hair",
    "colored_inner_hair",
    "multicolored_hair",
    "two-tone_hair",
    "twintails",
    "low_twintails",
    "ahoge",
    "blunt_bangs",
    "hime_cut",
    "drill_hair",
    "twin_drills",
}
_RAW_ORDER = (
    "absurdly_long_hair",
    "very_long_hair",
    "long_hair",
    "medium_hair",
    "short_hair",
    "big_hair",
    "gradient_hair",
    "streaked_hair",
    "colored_inner_hair",
    "multicolored_hair",
    "two-tone_hair",
    "braid",
    "twintails",
    "low_twintails",
    "hair_bun",
    "ahoge",
    "blunt_bangs",
    "hime_cut",
    "drill_hair",
    "twin_drills",
    "animal_ears",
    "horns",
)
_TAG_SUFFIX = re.compile(r"^(.*)_\(([^)]+)\)$")
_SEASON_TAIL = re.compile(
    r"(?:[\s_\-]+)?(?:"
    r"第\s*[0-9０-９一二三四五六七八九十]+\s*[季期部]"
    r"|season\s*[0-9]+"
    r"|[0-9]+\s*(?:st|nd|rd|th)\s*season"
    r")$",
    re.IGNORECASE,
)
_KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")
_CODE_ALIAS = re.compile(r"^CH\d+$", re.IGNORECASE)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tag_base(name: str) -> str:
    match = _TAG_SUFFIX.match(name or "")
    return match.group(1) if match else (name or "")


def tag_suffix(name: str) -> str | None:
    match = _TAG_SUFFIX.match(name or "")
    return match.group(2) if match else None


def norm_name(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "").strip().casefold()
    return re.sub(r"[\s_\-·・.]+", "", folded)


def strip_season(text: str) -> str:
    current = (text or "").strip()
    for _ in range(2):
        nxt = _SEASON_TAIL.sub("", current).strip()
        if nxt == current:
            break
        current = nxt
    return current


def fold_label(text: str) -> str:
    stripped = strip_season(unicodedata.normalize("NFKC", text or ""))
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u31f0-\u31ff\u4e00-\u9fff]+", "", stripped.casefold())


def labels_match(left: list[str], right: list[str]) -> bool:
    folded_left = [fold_label(text) for text in left if fold_label(text)]
    folded_right = [fold_label(text) for text in right if fold_label(text)]
    for lhs in folded_left:
        for rhs in folded_right:
            if len(lhs) >= 3 and lhs == rhs:
                return True
            short, long = (lhs, rhs) if len(lhs) <= len(rhs) else (rhs, lhs)
            if len(short) >= 6 and short in long:
                return True
    return False


def latin_slugs(text: str) -> list[str]:
    raw = unicodedata.normalize("NFKC", text or "").strip()
    if not re.search(r"[A-Za-z]", raw):
        return []
    spaced = bool(re.search(r"[\s\-]", raw))
    slug = re.sub(r"[^0-9A-Za-z]+", "_", _CAMEL.sub("_", raw)).strip("_").lower()
    slug = re.sub(r"_+", "_", slug)
    compact = slug.replace("_", "")
    ordered = [slug, compact] if spaced else [compact, slug]
    out: list[str] = []
    for item in ordered:
        if item and len(item) >= 3 and item not in out:
            out.append(item)
    return out


def _script(text: str) -> str | None:
    if _KANA.search(text):
        return "ja"
    if _has_han(text):
        return "zh"
    if _HANGUL.search(text):
        return None
    if re.search(r"[A-Za-z]", text):
        return "en"
    return None


def usable_auto_query(text: str) -> bool:
    value = (text or "").strip()
    if not value or len(value) > 40 or _CODE_ALIAS.match(value) or value.isdigit():
        return False
    if "'" in value or "’" in value:
        return False
    script = _script(value)
    if script is None:
        return False
    if script == "en":
        letters = re.sub(r"[^A-Za-z]", "", value)
        if len(letters) < 3 or len(value.split()) > 6:
            return False
    return True


def auto_tag_queries(character: dict, limit: int = 5) -> list[tuple[str, str]]:
    """(matchedBy, text). Longer aliases go first so 心月狐 is tried before 心."""
    name = str(character.get("name") or "").strip()
    texts: list[tuple[bool, str]] = [(True, name)]
    for alias in character.get("aliases") or []:
        texts.append((False, str(alias or "")))
    buckets: dict[str, list[tuple[bool, str]]] = {"en": [], "ja": [], "zh": []}
    seen: set[str] = set()
    for is_name, text in texts:
        value = text.strip()
        if not usable_auto_query(value):
            continue
        key = norm_name(value)
        if not key or key in seen:
            continue
        script = _script(value)
        if script is None:
            continue
        seen.add(key)
        buckets[script].append((is_name, value))
    for script in buckets:
        buckets[script].sort(key=lambda item: (-len(item[1]), item[1].casefold()))
    ordered = buckets["en"] + buckets["ja"] + buckets["zh"]
    queries: list[tuple[str, str]] = []
    for is_name, value in ordered:
        script = _script(value) or "en"
        kind = "name" if is_name else "alias"
        queries.append((f"{kind}:{script}", value))
        if len(queries) >= limit:
            break
    return queries


def is_braid_tag(name: str) -> bool:
    if name in {"braid", "braided_hair", "braided_ponytail", "braided_bun"}:
        return True
    return name.endswith("_braid") or name.endswith("_braids")


def is_bun_tag(name: str) -> bool:
    if name in {"hair_bun", "hair_buns"}:
        return True
    return name.endswith("_bun") or name.endswith("_buns")


def is_ear_or_horn_tag(name: str) -> bool:
    if name in {"fake_animal_ears", "pointy_ears", "elf_ears"}:
        return False
    if name == "animal_ears" or (name.endswith("_ears") and not name.startswith("fake_")):
        return True
    if name in {"horns", "antlers"} or name.endswith("_horns"):
        return True
    return False


def is_drill_tag(name: str) -> bool:
    if name in {"drill_hair", "twin_drills"}:
        return True
    return name.endswith("_drill") or name.endswith("_drills")


def mapped_raw_tag(name: str) -> bool:
    if name in _EXPLICIT_RAW:
        return True
    return is_braid_tag(name) or is_bun_tag(name) or is_ear_or_horn_tag(name) or is_drill_tag(name)


def _freq(freq: dict[str, float], name: str) -> float:
    try:
        return float(freq.get(name) or 0)
    except (TypeError, ValueError):
        return 0.0


def _hit(freq: dict[str, float], pred) -> bool:
    return any(pred(name) and _freq(freq, name) >= TAG_FREQUENCY_MIN for name in freq)


def _length_tag(freq: dict[str, float]) -> str | None:
    # very_long_hair is a subset of long_hair, so the longer tag wins when it
    # clears the threshold even though its frequency is lower.
    very = max(_freq(freq, "absurdly_long_hair"), _freq(freq, "very_long_hair"))
    if very >= TAG_FREQUENCY_MIN:
        return "超长（80cm+）"
    best_name = ""
    best = 0.0
    for name in ("long_hair", "medium_hair", "short_hair"):
        value = _freq(freq, name)
        if value > best:
            best = value
            best_name = name
    if best < TAG_FREQUENCY_MIN:
        return None
    return {"long_hair": "长", "medium_hair": "中长", "short_hair": "短发"}[best_name]


def order_structure_tags(tags: list[str]) -> list[str]:
    picked = set(tags)
    return [tag for tag in STRUCTURE_TAG_ORDER if tag in picked]


MULTICOLOR_RAW = ("multicolored_hair", "two-tone_hair")
_COLOR_CRAFT_TAGS = {"渐变", "挑染", "内层异色"}


def with_multisection(tags: list[str], freq: dict[str, float]) -> list[str]:
    """Block-colored hair (长离's black and red) with no finer color craft is 多段异色.

    multicolored_hair also fires for gradients and streaks, so it only counts when
    none of 渐变 / 挑染 / 内层异色 was found.
    """
    if "多段异色" in tags or _COLOR_CRAFT_TAGS & set(tags):
        return list(tags)
    if any(_freq(freq or {}, name) >= TAG_FREQUENCY_MIN for name in MULTICOLOR_RAW):
        return order_structure_tags([*tags, "多段异色"])
    return list(tags)


def structure_tags_from_frequencies(freq: dict[str, float], post_count: int) -> tuple[str, list[str]]:
    """One length tag, then every other family that clears TAG_FREQUENCY_MIN."""
    if post_count < AUTO_TAG_MIN_POSTS:
        return "too-few-posts", []
    tags: list[str] = []
    length = _length_tag(freq or {})
    if length:
        tags.append(length)
    if _hit(freq, lambda name: name == "big_hair"):
        tags.append("大发量")
    if _hit(freq, lambda name: name == "gradient_hair"):
        tags.append("渐变")
    if _hit(freq, lambda name: name == "streaked_hair"):
        tags.append("挑染")
    if _hit(freq, lambda name: name == "colored_inner_hair"):
        tags.append("内层异色")
    if _hit(freq, is_braid_tag):
        tags.append("编发")
    if _hit(freq, is_bun_tag):
        tags.append("盘发")
    if _hit(freq, lambda name: name in {"twintails", "low_twintails"}):
        tags.append("双马尾接片")
    if _hit(freq, lambda name: name == "ahoge"):
        tags.append("呆毛")
    if _hit(freq, lambda name: name in {"blunt_bangs", "hime_cut"}):
        tags.append("刘海定位")
    if _hit(freq, is_drill_tag):
        tags.append("硬定型（运输易塌）")
    if _hit(freq, is_ear_or_horn_tag):
        tags.append("兽耳等配件")
    return "ok", with_multisection(order_structure_tags(tags), freq)


def frequency_stats(freq: dict[str, float]) -> dict[str, float]:
    names = [name for name in freq if mapped_raw_tag(name)]
    ordered = [name for name in _RAW_ORDER if name in names]
    ordered.extend(sorted(name for name in names if name not in ordered))
    stats: dict[str, float] = {}
    for name in ordered:
        value = round(_freq(freq, name), 2)
        if value <= 0:
            continue
        stats[name] = value
    return stats


def top_copyright_names(pairs: list[tuple[str, float]]) -> list[str]:
    if not pairs:
        return []
    top = max(freq for _, freq in pairs)
    return [name for name, freq in pairs if name and freq >= top - COPYRIGHT_TIE_EPSILON]


def game_copyright_matches(game: str, pairs: list[tuple[str, float]]) -> bool:
    expected = GAME_COPYRIGHT.get(game or "")
    if not expected:
        return False
    if expected in top_copyright_names(pairs):
        return True
    # Endfield posts are also tagged arknights, so the parent sits at 1.0 and
    # arknights:_endfield lands just outside the 0.005 tie. A high second place
    # is still this game. A low one is the original work.
    if game != "endfield" or "arknights" not in top_copyright_names(pairs):
        return False
    freq = {name: value for name, value in pairs}
    return freq.get(expected, 0.0) >= 0.95


def anime_copyright_matches(
    pairs: list[tuple[str, float]],
    work_names: list[str],
    other_names: dict[str, list[str]],
) -> bool:
    for tag in top_copyright_names(pairs):
        labels = [tag, *list(other_names.get(tag) or [])]
        if labels_match(work_names, labels):
            return True
    return False


def auto_tags_fresh(auto: dict | None, today: str) -> bool:
    """error is retried. ok / not-found / too-few-posts / work-mismatch last 60 days."""
    if not isinstance(auto, dict):
        return False
    if auto.get("version") != AUTO_TAG_VERSION:
        return False
    if auto.get("status") not in FRESH_AUTO_TAG_STATUSES:
        return False
    try:
        fetched = date.fromisoformat(str(auto.get("fetchedAt") or "")[:10])
        current = date.fromisoformat(str(today)[:10])
    except ValueError:
        return False
    return (current - fetched).days <= AUTO_TAG_TTL_DAYS


def rank_auto_tag_targets(characters: list[dict], today: str) -> list[dict]:
    """LEAKED, then LIVE, then the rest. Fresh autoTags are left out."""
    pending = [row for row in characters if isinstance(row, dict) and not auto_tags_fresh(row.get("autoTags"), today)]

    def sort_key(row: dict) -> tuple[int, str]:
        status = row.get("status")
        if status == "LEAKED":
            rank = 0
        elif status == "LIVE":
            rank = 1
        else:
            rank = 2
        return rank, str(row.get("key") or "")

    return sorted(pending, key=sort_key)


def build_auto_tag_record(
    *,
    status: str,
    tags: list[str] | None = None,
    danbooru_tag: str | None = None,
    post_count: int | None = None,
    matched_by: str | None = None,
    stats: dict[str, float] | None = None,
    today: str,
) -> dict:
    usable = list(tags or []) if status == "ok" else []
    return {
        "version": AUTO_TAG_VERSION,
        "status": status,
        "tags": order_structure_tags(usable),
        "danbooruTag": danbooru_tag,
        "postCount": post_count,
        "matchedBy": matched_by,
        "stats": dict(stats or {}),
        "fetchedAt": today[:10],
    }


def wiki_identity_ok(
    character: dict,
    query: str,
    other_names: list[str],
    name_index: dict[str, set[str]],
) -> bool:
    """The page has to name this character, and not only some other catalog name."""
    norms = [norm_name(text) for text in other_names if norm_name(text)]
    own = norm_name(str(character.get("name") or ""))
    key = str(character.get("key") or "")
    if own and own in norms:
        return True
    query_norm = norm_name(query)
    if query_norm not in norms:
        return False
    for item in norms:
        owners = name_index.get(item) or set()
        if owners and key not in owners:
            return False
    return True


def bangumi_subject_names(subject: dict | None) -> list[str]:
    if not isinstance(subject, dict):
        return []
    names: list[str] = []
    for key in ("name", "name_cn"):
        text = str(subject.get(key) or "").strip()
        if text:
            names.append(text)
    for item in subject.get("infobox") or []:
        if not isinstance(item, dict) or item.get("key") not in {"别名", "英文名", "罗马字", "日文名"}:
            continue
        value = item.get("value")
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
        elif isinstance(value, list):
            for part in value:
                if isinstance(part, str) and part.strip():
                    names.append(part.strip())
                elif isinstance(part, dict):
                    text = str(part.get("v") or part.get("value") or "").strip()
                    if text:
                        names.append(text)
    return names


def character_name_index(characters: list[dict]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for row in characters:
        if not isinstance(row, dict) or not row.get("key"):
            continue
        key = norm_name(str(row.get("name") or ""))
        if key:
            index.setdefault(key, set()).add(str(row["key"]))
    return index


def latin_slug_index(characters: list[dict]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for row in characters:
        if not isinstance(row, dict) or not row.get("key"):
            continue
        texts = [str(row.get("name") or ""), *[str(alias or "") for alias in (row.get("aliases") or [])]]
        for text in texts:
            if _script(text) != "en":
                continue
            slugs = latin_slugs(text)
            if not slugs:
                continue
            index.setdefault(slugs[0], set()).add(str(row["key"]))
    return index


def restore_previous_auto_tags(characters: list[dict], previous: dict | None) -> None:
    """Copy the last autoTags onto the merged rows. A failed Danbooru run keeps them."""
    saved: dict[str, dict] = {}
    if isinstance(previous, dict):
        for row in previous.get("characters") or []:
            if isinstance(row, dict) and row.get("key") and isinstance(row.get("autoTags"), dict):
                saved[str(row["key"])] = row["autoTags"]
    for row in characters:
        if not isinstance(row, dict) or not row.get("key"):
            continue
        old = saved.get(str(row["key"]))
        if old is not None and not isinstance(row.get("autoTags"), dict):
            row["autoTags"] = with_stored_multisection(old)


def with_stored_multisection(auto: dict) -> dict:
    """Reused rows pick up 多段异色 from their stored stats without a new Danbooru call."""
    if auto.get("status") != "ok" or not isinstance(auto.get("stats"), dict):
        return auto
    tags = with_multisection(list(auto.get("tags") or []), auto["stats"])
    if tags == list(auto.get("tags") or []):
        return auto
    return {**auto, "tags": tags}
