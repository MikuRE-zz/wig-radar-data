#!/usr/bin/env python3
"""Fill catalog autoTags from Danbooru without refetching the rest of the catalog.

Progress is written after every character. Re-run the same command to continue.
A Danbooru outage keeps what is already saved and exits.

    python scripts/backfill_auto_tags.py --catalog catalog.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.catalog_lib import (  # noqa: E402
    auto_tags_fresh,
    beijing_today,
    character_name_index,
    latin_slug_index,
    rank_auto_tag_targets,
)
from scripts.danbooru_tags import DanbooruClient, DanbooruOutage, resolve_auto_tags  # noqa: E402

PROGRESS_VERSION = "danbooru-v1"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _progress_results(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    payload = _load(path)
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, dict):
        return {}
    return {str(key): value for key, value in results.items() if isinstance(value, dict)}


def _save_progress(path: Path, results: dict[str, dict]) -> None:
    _write(path, {"version": PROGRESS_VERSION, "updatedAt": beijing_today(), "results": results})


def _apply(characters: list[dict], results: dict[str, dict], today: str) -> int:
    applied = 0
    for row in characters:
        saved = results.get(str(row.get("key") or ""))
        if not isinstance(saved, dict):
            continue
        if auto_tags_fresh(row.get("autoTags"), today):
            continue
        row["autoTags"] = saved
        applied += 1
    return applied


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Backfill catalog autoTags from Danbooru")
    parser.add_argument("--catalog", type=Path, default=ROOT / "catalog.json")
    parser.add_argument("--progress", type=Path, default=ROOT / ".cache" / "auto-tags-progress.json")
    parser.add_argument("--limit", type=int, default=0, help="Max characters this process; 0 means all pending")
    parser.add_argument("--key", action="append", default=[], help="Only these catalog keys, in the given order")
    parser.add_argument("--dry-run", action="store_true", help="Print tags and do not write the catalog or progress")
    args = parser.parse_args()
    today = beijing_today()
    document = _load(args.catalog)
    characters = [row for row in document.get("characters") or [] if isinstance(row, dict)]
    results = {} if args.dry_run else _progress_results(args.progress)
    if results:
        applied = _apply(characters, results, today)
        print(f"resumed {applied} autoTags from {args.progress}")
    by_key = {str(row.get("key")): row for row in characters}
    if args.key:
        targets = [by_key[key] for key in args.key if key in by_key]
        missing = [key for key in args.key if key not in by_key]
        if missing:
            print("unknown keys: " + ", ".join(missing), file=sys.stderr)
    else:
        targets = rank_auto_tag_targets(characters, today)
    if args.limit > 0:
        targets = targets[: args.limit]
    print(f"autoTags pending {len(targets)}")
    if not targets:
        if results and not args.dry_run:
            _write(args.catalog, document)
        return 0
    client = DanbooruClient()
    try:
        client.probe()
    except DanbooruOutage as error:
        print(f"danbooru probe failed: {error}", file=sys.stderr)
        return 1
    name_index = character_name_index(characters)
    slug_index = latin_slug_index(characters)
    started = time.monotonic()
    done = 0
    try:
        for row in targets:
            record = None
            for attempt in range(4):
                try:
                    record = resolve_auto_tags(row, client, today, name_index, slug_index)
                    break
                except DanbooruOutage as error:
                    if attempt == 3:
                        raise
                    wait = 20 * (attempt + 1)
                    print(f"danbooru outage: {error}; retry {attempt + 1} in {wait}s", file=sys.stderr, flush=True)
                    time.sleep(wait)
            if record is None:
                raise DanbooruOutage("no result")
            row["autoTags"] = record
            done += 1
            if not args.dry_run:
                results[str(row["key"])] = record
                _save_progress(args.progress, results)
                if done % 10 == 0:
                    _write(args.catalog, document)
            label = ",".join(record.get("tags") or []) or "-"
            elapsed = time.monotonic() - started
            print(
                f"{done}/{len(targets)} {row.get('source')} {row.get('name')} "
                f"{record.get('status')} [{label}] {record.get('danbooruTag') or '-'} "
                f"posts={record.get('postCount')} via {record.get('matchedBy')} {elapsed:.0f}s",
                flush=True,
            )
    except DanbooruOutage as error:
        print(f"danbooru outage: {error}", file=sys.stderr)
        if not args.dry_run:
            _save_progress(args.progress, results)
            _write(args.catalog, document)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        if not args.dry_run:
            _save_progress(args.progress, results)
            _write(args.catalog, document)
        return 1
    if not args.dry_run:
        _save_progress(args.progress, results)
        _write(args.catalog, document)
    print(f"autoTags wrote {done} in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"fatal: {error}", file=sys.stderr)
        raise
