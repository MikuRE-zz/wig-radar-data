"""Pure catalog rules: satellite day, status window, and hair reuse.

Kept separate from the network fetch so the first-import baseline can be tested
without calling any data source.
"""

from __future__ import annotations

import re
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
    (`previous is None`) every character is baseline. A key that shows up
    later is not baseline, and an existing firstSeenAt is never rewritten.
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
                row["baseline"] = first_import
            merged[row["key"]] = row

    for source, rows in prev_by_source.items():
        if source not in fetched:
            for old in rows:
                merged.setdefault(old["key"], old)
    return list(merged.values())
