"""Danbooru character-tag lookup for structure tags. Read-only, serial, <=1 req/s."""

from __future__ import annotations

import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from scripts.catalog_lib import (
    AUTO_TAG_BATCH_LIMIT,
    AUTO_TAG_BUDGET_SECONDS,
    AUTO_TAG_MIN_POSTS,
    GAME_COPYRIGHT,
    anime_copyright_matches,
    auto_tag_queries,
    bangumi_subject_names,
    build_auto_tag_record,
    character_name_index,
    frequency_stats,
    game_copyright_matches,
    latin_slug_index,
    latin_slugs,
    norm_name,
    rank_auto_tag_targets,
    restore_previous_auto_tags,
    structure_tags_from_frequencies,
    tag_base,
    tag_suffix,
    top_copyright_names,
    wiki_identity_ok,
)

USER_AGENT = "WigRadar/0.1.0 (mikure; GitHubActions; cos wig selection tool)"
DANBOORU = "https://danbooru.donmai.us"
BANGUMI = "https://api.bgm.tv"
DANBOORU_INTERVAL = 1.05
BANGUMI_INTERVAL = 0.35
MAX_CANDIDATES = 4


class DanbooruOutage(RuntimeError):
    """The API is unreachable or refusing the client. Keep the previous autoTags."""


class _QueryError(RuntimeError):
    pass


class _Http:
    def __init__(self, user_agent: str, min_interval: float):
        self.user_agent = user_agent
        self.min_interval = min_interval
        self._last = 0.0
        self._cache: dict[str, Any] = {}

    def get_json(self, url: str) -> Any:
        if url in self._cache:
            return self._cache[url]
        last_error: Exception | None = None
        for attempt in range(3):
            self._reserve()
            req = urllib.request.Request(
                url,
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=40) as resp:
                    raw = resp.read()
                    if not raw:
                        payload = None
                    else:
                        payload = json.loads(raw.decode("utf-8"))
                self._cache[url] = payload
                return payload
            except urllib.error.HTTPError as error:
                snippet = error.read()[:240].decode("utf-8", errors="replace")
                last_error = error
                if error.code == 404:
                    self._cache[url] = None
                    return None
                if error.code == 400:
                    raise _QueryError(f"HTTP {error.code} {url} {snippet}") from error
                if error.code in {403, 401, 408, 409, 412, 429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(1.2 * (2**attempt) + random.random())
                    continue
                if error.code in {403, 401, 408, 409, 412, 429, 500, 502, 503, 504}:
                    raise DanbooruOutage(f"HTTP {error.code} {url} {snippet}") from error
                raise _QueryError(f"HTTP {error.code} {url} {snippet}") from error
            except DanbooruOutage:
                raise
            except _QueryError:
                raise
            except Exception as error:
                last_error = error
                if attempt < 2:
                    time.sleep(1.2 * (2**attempt) + random.random())
                    continue
                raise DanbooruOutage(f"{type(error).__name__} {url} {error}") from error
        raise DanbooruOutage(str(last_error) if last_error else "request failed")

    def _reserve(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()


def _url(base: str, path: str, params: dict[str, str]) -> str:
    return base + path + "?" + urllib.parse.urlencode(params)


def _tag_row(row: dict) -> dict | None:
    name = str(row.get("name") or "")
    if not name:
        return None
    try:
        posts = int(row.get("post_count") or 0)
    except (TypeError, ValueError):
        posts = 0
    try:
        category = int(row.get("category") or 0)
    except (TypeError, ValueError):
        category = 0
    return {"name": name, "post_count": posts, "category": category}


class DanbooruClient:
    def __init__(self, user_agent: str = USER_AGENT):
        self.danbooru = _Http(user_agent, DANBOORU_INTERVAL)
        self.bangumi = _Http(user_agent, BANGUMI_INTERVAL)
        self._work_names: dict[int, list[str]] = {}

    def probe(self) -> None:
        payload = self.danbooru.get_json(_url(DANBOORU, "/tags.json", {"search[name]": "long_hair", "limit": "1"}))
        if not isinstance(payload, list) or not payload or not payload[0].get("name"):
            raise DanbooruOutage(f"probe returned {payload!r}"[:300])
        print(f"danbooru probe: HTTP 200 {payload[0].get('name')} posts={payload[0].get('post_count')}")

    def search_tags(self, slug: str) -> list[dict]:
        try:
            payload = self.danbooru.get_json(
                _url(
                    DANBOORU,
                    "/tags.json",
                    {
                        "search[name_matches]": slug + "*",
                        "search[category]": "4",
                        "search[order]": "count",
                        "limit": "20",
                    },
                )
            )
        except _QueryError:
            return []
        if not isinstance(payload, list):
            return []
        rows = [tag for row in payload if isinstance(row, dict) and (tag := _tag_row(row)) and tag["category"] == 4]
        rows.sort(key=lambda tag: -tag["post_count"])
        return rows

    def lookup_tag(self, name: str) -> dict | None:
        try:
            payload = self.danbooru.get_json(_url(DANBOORU, "/tags.json", {"search[name]": name, "limit": "1"}))
        except _QueryError:
            return None
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            return None
        return _tag_row(payload[0])

    def search_wiki(self, text: str) -> list[dict]:
        try:
            payload = self.danbooru.get_json(
                _url(DANBOORU, "/wiki_pages.json", {"search[other_names_match]": text, "limit": "10"})
            )
        except _QueryError:
            return []
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict) and row.get("title")]

    def wiki_page(self, title: str) -> dict | None:
        try:
            payload = self.danbooru.get_json(
                _url(DANBOORU, "/wiki_pages.json", {"search[title]": title, "limit": "1"})
            )
        except _QueryError:
            return None
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            return None
        page = payload[0]
        if str(page.get("title") or "") != title:
            return None
        return page

    def tag_alias(self, name: str) -> str | None:
        try:
            payload = self.danbooru.get_json(
                _url(DANBOORU, "/tag_aliases.json", {"search[antecedent_name]": name, "limit": "5"})
            )
        except _QueryError:
            return None
        if not isinstance(payload, list):
            return None
        for row in payload:
            if not isinstance(row, dict):
                continue
            if row.get("status") not in (None, "active"):
                continue
            if str(row.get("antecedent_name") or "") != name:
                continue
            consequent = str(row.get("consequent_name") or "")
            if consequent:
                return consequent
        return None

    def related(self, tag: str, category: str) -> tuple[int, list[tuple[str, float]]]:
        limit = "300" if category == "general" else "15"
        try:
            payload = self.danbooru.get_json(
                _url(DANBOORU, "/related_tag.json", {"query": tag, "category": category, "limit": limit})
            )
        except _QueryError:
            return 0, []
        if not isinstance(payload, dict):
            return 0, []
        try:
            posts = int(payload.get("post_count") or 0)
        except (TypeError, ValueError):
            posts = 0
        pairs: list[tuple[str, float]] = []
        for item in payload.get("related_tags") or []:
            if not isinstance(item, dict):
                continue
            name = str((item.get("tag") or {}).get("name") or "")
            if not name:
                continue
            try:
                freq = float(item.get("frequency") or 0)
            except (TypeError, ValueError):
                freq = 0.0
            pairs.append((name, freq))
        return posts, pairs

    def work_names(self, character: dict) -> list[str]:
        subject_id = character.get("bangumiSubjectId")
        if not isinstance(subject_id, int):
            return []
        if subject_id in self._work_names:
            return self._work_names[subject_id]
        try:
            payload = self.bangumi.get_json(f"{BANGUMI}/v0/subjects/{subject_id}")
        except DanbooruOutage as error:
            print(f"bangumi subject {subject_id} failed: {error}", file=sys.stderr)
            payload = None
        except _QueryError:
            payload = None
        names = bangumi_subject_names(payload if isinstance(payload, dict) else None)
        self._work_names[subject_id] = names
        return names


def _exact_page(page: dict, query: str) -> bool:
    target = norm_name(query)
    if not target:
        return False
    values = [str(page.get("title") or "")]
    values.extend(str(name) for name in (page.get("other_names") or []))
    return any(norm_name(value) == target for value in values)


def _pool(tags: list[dict], slug: str) -> list[dict]:
    exact: list[dict] = []
    prefix: list[dict] = []
    for tag in tags:
        base = tag_base(tag["name"])
        if base == slug:
            exact.append(tag)
        elif base.startswith(slug + "_") or tag["name"].startswith(slug + "_"):
            prefix.append(tag)
    exact.sort(key=lambda tag: -tag["post_count"])
    prefix.sort(key=lambda tag: -tag["post_count"])
    if any(tag["post_count"] >= AUTO_TAG_MIN_POSTS for tag in exact):
        return exact[:3]
    return (exact + prefix)[:3]


def _english_candidates(client: DanbooruClient, query: str, preferred_suffix: str | None) -> list[dict]:
    """Try `{slug}_({this game})` before a wildcard, so a more-posted same name in another work loses."""
    slugs = latin_slugs(query)
    if not slugs:
        return []
    if preferred_suffix:
        direct = client.lookup_tag(f"{slugs[0]}_({preferred_suffix})")
        if direct and direct.get("category") == 4 and direct["post_count"] >= AUTO_TAG_MIN_POSTS:
            return [direct]
    pool = _pool(client.search_tags(slugs[0]), slugs[0])
    if preferred_suffix:
        pool.sort(key=lambda tag: (tag_suffix(tag["name"]) != preferred_suffix, -tag["post_count"]))
    if any(tag_base(tag["name"]) == slugs[0] and tag["post_count"] >= AUTO_TAG_MIN_POSTS for tag in pool):
        return pool
    if len(slugs) == 1:
        return pool
    extra = _pool(client.search_tags(slugs[1]), slugs[1])
    merged: dict[str, dict] = {tag["name"]: tag for tag in pool}
    for tag in extra:
        merged.setdefault(tag["name"], tag)
    ordered = sorted(
        merged.values(),
        key=lambda tag: (tag_suffix(tag["name"]) != preferred_suffix, -tag["post_count"]),
    )
    return ordered[:3]


def _wiki_candidates(client: DanbooruClient, query: str) -> list[dict]:
    found: list[dict] = []
    for page in client.search_wiki(query):
        if not _exact_page(page, query):
            continue
        tag = client.lookup_tag(str(page.get("title")))
        if not tag or tag["category"] != 4:
            continue
        copied = dict(tag)
        copied["page"] = page
        found.append(copied)
        if len(found) >= 3:
            break
    found.sort(key=lambda tag: -tag["post_count"])
    return found


def _canonical(client: DanbooruClient, tag: dict) -> dict:
    if tag["post_count"] > 0:
        return tag
    consequent = client.tag_alias(tag["name"])
    if not consequent or consequent == tag["name"]:
        return tag
    looked = client.lookup_tag(consequent)
    if not looked or looked["category"] != 4 or looked["post_count"] <= 0:
        return tag
    copied = dict(looked)
    if tag.get("page") is not None:
        copied["page"] = tag["page"]
    return copied


def _suffix_fallback(character: dict, query: str, tag: dict, slug_index: dict[str, set[str]]) -> bool:
    """No wiki page: accept only `{slug}_({this game})` when the slug is unique."""
    expected = GAME_COPYRIGHT.get(str(character.get("game") or ""))
    if not expected or tag_suffix(tag["name"]) != expected:
        return False
    slugs = latin_slugs(query)
    if not slugs or tag_base(tag["name"]) != slugs[0]:
        return False
    owners = slug_index.get(slugs[0]) or set()
    return owners == {str(character.get("key") or "")}


def _identity(character: dict, query: str, page: dict | None, tag: dict, name_index, slug_index) -> bool:
    if page is None:
        return _suffix_fallback(character, query, tag, slug_index)
    names = [str(name) for name in (page.get("other_names") or [])]
    return wiki_identity_ok(character, query, names, name_index)


def _work_ok(character: dict, client: DanbooruClient, pairs: list[tuple[str, float]]) -> bool:
    game = str(character.get("game") or "")
    if game in GAME_COPYRIGHT:
        return game_copyright_matches(game, pairs)
    work_names = [str(character.get("work") or "")]
    others: dict[str, list[str]] = {}
    for tag in top_copyright_names(pairs):
        page = client.wiki_page(tag)
        names = page.get("other_names") if isinstance(page, dict) else []
        others[tag] = [str(name) for name in (names or [])]
    if anime_copyright_matches(pairs, work_names, others):
        return True
    extra = client.work_names(character)
    if not extra:
        return False
    return anime_copyright_matches(pairs, work_names + extra, others)


def _evaluate(
    character: dict,
    client: DanbooruClient,
    tag: dict,
    matched_by: str,
    query: str,
    today: str,
    name_index: dict[str, set[str]],
    slug_index: dict[str, set[str]],
) -> dict | None:
    tag = _canonical(client, tag)
    if tag.get("category") != 4:
        return None
    page = tag.get("page")
    if page is None:
        page = client.wiki_page(tag["name"])
    if not _identity(character, query, page if isinstance(page, dict) else None, tag, name_index, slug_index):
        return None
    related_posts, pairs = client.related(tag["name"], "copyright")
    posts = max(int(tag["post_count"]), related_posts)
    if not _work_ok(character, client, pairs):
        return {"_reject": "copyright"}
    if posts < AUTO_TAG_MIN_POSTS:
        return build_auto_tag_record(
            status="too-few-posts",
            danbooru_tag=tag["name"],
            post_count=posts,
            matched_by=matched_by,
            today=today,
        )
    _, general = client.related(tag["name"], "general")
    freq: dict[str, float] = {}
    for name, value in general:
        freq[name] = max(freq.get(name, 0.0), value)
    _status, tags = structure_tags_from_frequencies(freq, posts)
    return build_auto_tag_record(
        status="ok",
        tags=tags,
        danbooru_tag=tag["name"],
        post_count=posts,
        matched_by=matched_by,
        stats=frequency_stats(freq),
        today=today,
    )


def resolve_auto_tags(
    character: dict,
    client: DanbooruClient,
    today: str,
    name_index: dict[str, set[str]],
    slug_index: dict[str, set[str]],
) -> dict:
    rejected_work = False
    weak: dict | None = None
    checked = 0
    seen: set[str] = set()
    for matched_by, query in auto_tag_queries(character):
        if checked >= MAX_CANDIDATES:
            break
        if matched_by.endswith(":en"):
            preferred = GAME_COPYRIGHT.get(str(character.get("game") or ""))
            candidates = _english_candidates(client, query, preferred)
        else:
            candidates = _wiki_candidates(client, query)
        for tag in candidates:
            if checked >= MAX_CANDIDATES:
                break
            if tag["name"] in seen:
                continue
            seen.add(tag["name"])
            checked += 1
            try:
                verdict = _evaluate(character, client, tag, matched_by, query, today, name_index, slug_index)
            except DanbooruOutage:
                raise
            except Exception as error:
                print(f"autoTags {character.get('key')} {tag['name']}: {error}", file=sys.stderr)
                continue
            if verdict is None:
                continue
            if verdict.get("_reject") == "copyright":
                rejected_work = True
                continue
            if verdict.get("status") == "too-few-posts":
                if weak is None or int(verdict.get("postCount") or 0) > int(weak.get("postCount") or 0):
                    weak = verdict
                continue
            return verdict
    if weak is not None:
        return weak
    if rejected_work:
        return build_auto_tag_record(status="work-mismatch", today=today)
    return build_auto_tag_record(status="not-found", today=today)


def refresh_auto_tags(
    characters: list[dict],
    previous: dict | None,
    client: DanbooruClient,
    today: str,
    *,
    limit: int = AUTO_TAG_BATCH_LIMIT,
    budget_seconds: float = AUTO_TAG_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Fill missing or expired autoTags. An outage leaves every previous value in place."""
    restore_previous_auto_tags(characters, previous)
    started = clock()
    try:
        client.probe()
    except DanbooruOutage as error:
        print(f"danbooru probe failed: {error}; keeping previous autoTags", file=sys.stderr)
        return {"status": "skipped", "error": str(error), "attempted": 0, "seconds": round(clock() - started, 1)}
    name_index = character_name_index(characters)
    slug_index = latin_slug_index(characters)
    targets = rank_auto_tag_targets(characters, today)[: max(0, limit)]
    attempted = 0
    stopped = False
    outage = ""
    for row in targets:
        if clock() - started >= budget_seconds:
            stopped = True
            break
        try:
            row["autoTags"] = resolve_auto_tags(row, client, today, name_index, slug_index)
            attempted += 1
            auto = row["autoTags"]
            label = ",".join(auto.get("tags") or []) or auto.get("status")
            print(f"autoTags {row.get('name')} {auto.get('status')} {label} posts={auto.get('postCount')}")
        except DanbooruOutage as error:
            outage = str(error)
            stopped = True
            print(f"danbooru outage: {error}; keeping remaining autoTags", file=sys.stderr)
            break
        except Exception as error:
            attempted += 1
            print(f"autoTags {row.get('key')} failed: {error}", file=sys.stderr)
            row["autoTags"] = build_auto_tag_record(status="error", today=today)
    counts: dict[str, int] = {}
    for row in characters:
        auto = row.get("autoTags")
        status = auto.get("status") if isinstance(auto, dict) else "missing"
        counts[str(status)] = counts.get(str(status), 0) + 1
    summary = {
        "status": "partial" if stopped else "ok",
        "attempted": attempted,
        "deferred": max(0, len(targets) - attempted),
        "seconds": round(clock() - started, 1),
        "counts": counts,
    }
    if outage:
        summary["error"] = outage
    print(
        "autoTags "
        + " ".join(f"{key}={value}" for key, value in summary.items() if key != "counts")
        + " "
        + " ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    )
    return summary
