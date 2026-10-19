"""Pure catalog rules: satellite day, status window, and hair reuse.

Kept separate from the network fetch so the first-import baseline can be tested
without calling any data source.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
GAME_WINDOW_DAYS = 40
ANIME_WINDOW_DAYS = 21

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
