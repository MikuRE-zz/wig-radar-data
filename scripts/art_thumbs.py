#!/usr/bin/env python3
"""Display thumbnails for Endfield and Star Rail art.

The phone loads a screen of cards at once. Endfield splash PNGs are several
megabytes and wiki.gg answers 429, so those cards stay blank. Star Rail draw
cards are smaller but still too heavy. This module keeps a WebP whose long
edge is 900px on this repo and points artUrl at that file.

wiki.gg: one request at a time, at most one per second. 429 waits Retry-After,
otherwise exponential backoff. Nanoka uses the same serial pace.
A failed thumbnail leaves artUrl on the original image.
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_BASE = "https://raw.githubusercontent.com/MikuRE-zz/wig-radar-data/main"
USER_AGENT = "WigRadar/0.1.0 (mikure; GitHubActions; cos wig selection tool)"

THUMB_GAMES = ("hsr", "endfield")
HSR_HOST = "static.nanoka.cc"
ENDFIELD_HOST = "endfield.wiki.gg"

LONGEST_EDGE = 900
TARGET_BYTES = 150 * 1024
START_QUALITY = 80
QUALITY_FLOOR = 50
EDGE_FLOOR = 480
# New characters only. Linking an existing file does not spend these.
ART_DAILY_LIMIT = 16
ART_BUDGET_SECONDS = 240


def art_relpath(game: str, key: str) -> str:
    safe = str(key).replace(":", "_")
    return f"art/{game}/{safe}.webp"


def art_public_url(game: str, key: str) -> str:
    return f"{PUBLIC_BASE}/{art_relpath(game, key)}"


def art_file(root: Path, game: str, key: str) -> Path:
    safe = str(key).replace(":", "_")
    return root / "art" / game / f"{safe}.webp"


def is_display_url(url: str) -> bool:
    return url.startswith(f"{PUBLIC_BASE}/art/")


def wants_display_thumb(game: str | None, url: str | None) -> bool:
    """Only the two hosts that are too large for a phone grid."""
    if game not in THUMB_GAMES or not url:
        return False
    host = urllib.parse.urlparse(url).netloc.lower()
    if game == "hsr":
        return host == HSR_HOST
    return host == ENDFIELD_HOST


def needs_rebuild(source_url: str | None, stored_source_url: str | None, file_exists: bool) -> bool:
    """Redownload when the source URL changed or the file is missing.

    An unchanged source with the file already on disk is not a new download.
    """
    if not source_url:
        return False
    if file_exists and stored_source_url == source_url:
        return False
    return True


def fit_longest_edge(width: int, height: int, longest: int = LONGEST_EDGE) -> tuple[int, int]:
    """Scale so the longer side is `longest`. Do not upscale. Keep the aspect."""
    if width <= 0 or height <= 0:
        return max(int(width), 1), max(int(height), 1)
    edge = max(width, height)
    if edge <= longest:
        return width, height
    if width >= height:
        return longest, max(1, round(height * longest / width))
    return max(1, round(width * longest / height)), longest


def next_encode(
    longest: int,
    quality: int,
    nbytes: int,
    *,
    limit: int = TARGET_BYTES,
) -> tuple[int, int] | None:
    """Next (longest, quality) when the encode is over the byte budget.

    Drop quality first, then the long edge. None means stop and keep this encode.
    """
    if nbytes <= limit:
        return None
    if quality > QUALITY_FLOOR:
        return longest, quality - 10
    shrunk = int(longest * 0.85)
    if shrunk >= EDGE_FLOOR and shrunk < longest:
        return shrunk, 70
    return None


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    clock = now or datetime.now(timezone.utc)
    return (when - clock).total_seconds()


def retry_wait_seconds(retry_after: str | None, attempt: int, *, now: datetime | None = None) -> float:
    """429: Retry-After when it parses, otherwise 2s, 4s, 8s… capped at 60s."""
    parsed = parse_retry_after(retry_after, now=now)
    if parsed is not None:
        return max(0.0, parsed)
    return min(60.0, 2.0 ** max(1, int(attempt)))


def encode_thumbnail(image: Image.Image) -> tuple[bytes, int, int, int]:
    """WebP bytes, width, height, quality. Alpha is kept."""
    rgba = image.convert("RGBA")
    longest = LONGEST_EDGE
    quality = START_QUALITY
    scaled: Image.Image | None = None
    scaled_size: tuple[int, int] | None = None
    best: tuple[bytes, int, int, int] | None = None
    while True:
        width, height = fit_longest_edge(rgba.width, rgba.height, longest)
        if scaled is None or scaled_size != (width, height):
            if (width, height) == (rgba.width, rgba.height):
                scaled = rgba
            else:
                scaled = rgba.resize((width, height), Image.Resampling.LANCZOS)
            scaled_size = (width, height)
        buffer = io.BytesIO()
        scaled.save(buffer, format="WEBP", quality=quality, method=4)
        data = buffer.getvalue()
        best = (data, width, height, quality)
        nxt = next_encode(longest, quality, len(data))
        if nxt is None:
            return best
        longest, quality = nxt


def cached_image_path(url: str) -> Path:
    # Same key as fetch_catalog.read_image_bytes, so a hair pass is not a second download.
    digest = hashlib.sha1(url.encode()).hexdigest()
    return ROOT / ".cache" / "img" / digest


class PoliteFetcher:
    """Serial image GET. One in flight; each host waits at least `min_interval`."""

    def __init__(
        self,
        min_interval: float = 1.0,
        *,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ):
        self.min_interval = min_interval
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._next: dict[str, float] = {}

    def _pace(self, host: str) -> None:
        delay = self._next.get(host, 0.0) - self._monotonic()
        if delay > 0:
            self._sleep(delay)
        self._next[host] = self._monotonic() + self.min_interval

    def get(self, url: str) -> bytes:
        host = urllib.parse.urlparse(url).netloc.lower()
        last: Exception | None = None
        for attempt in range(1, 6):
            self._pace(host)
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "image/webp,image/*,*/*"},
            )
            try:
                with self._opener(request, timeout=90) as response:
                    data = response.read()
            except urllib.error.HTTPError as error:
                if error.code == 429 or error.code in {408, 500, 502, 503, 504}:
                    header = error.headers.get("Retry-After") if error.code == 429 and error.headers else None
                    wait = retry_wait_seconds(header, attempt)
                    self._next[host] = max(self._next.get(host, 0.0), self._monotonic() + wait)
                    last = error
                    continue
                raise RuntimeError(f"HTTP {error.code} {url}") from error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                wait = retry_wait_seconds(None, attempt)
                self._next[host] = max(self._next.get(host, 0.0), self._monotonic() + wait)
                last = error
                continue
            if len(data) < 32 or data[:1] == b"<" or data[:15].lstrip().lower().startswith(b"<!doctype"):
                raise RuntimeError(f"not an image {url}")
            return data
        raise RuntimeError(f"gave up {url}: {last}")


def _upstream_url(row: dict) -> str | None:
    source = row.get("artSourceUrl")
    if isinstance(source, str) and source.strip():
        return source.strip()
    art = row.get("artUrl")
    if isinstance(art, str) and art.strip() and not is_display_url(art):
        return art.strip()
    return None


def _remembered_source(row: dict) -> str | None:
    source = row.get("artSourceUrl")
    if isinstance(source, str) and source.strip():
        return source.strip()
    art = row.get("artUrl")
    if isinstance(art, str) and art.strip() and not is_display_url(art):
        return art.strip()
    return None


def _assign_display(row: dict, display_url: str, source_url: str) -> None:
    ordered: dict[str, Any] = {}
    placed = False
    for key, value in row.items():
        if key == "artSourceUrl":
            continue
        if key == "artUrl":
            ordered["artUrl"] = display_url
            ordered["artSourceUrl"] = source_url
            placed = True
            continue
        ordered[key] = value
    if not placed:
        ordered["artUrl"] = display_url
        ordered["artSourceUrl"] = source_url
    row.clear()
    row.update(ordered)


def _file_ready(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 32


def refresh_display_art(
    characters: list[dict],
    previous: dict | None,
    root: Path,
    *,
    limit: int = ART_DAILY_LIMIT,
    budget_seconds: float = ART_BUDGET_SECONDS,
    load_bytes: Callable[[str], bytes] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Point eligible rows at a local WebP. Download only when the source changed.

    `limit` and `budget_seconds` apply to new encodes. 0 means no cap.
    Linking a file that is already on disk does not spend them.
    """
    clock = monotonic or time.monotonic
    remembered: dict[str, str | None] = {}
    if previous and isinstance(previous.get("characters"), list):
        for old in previous["characters"]:
            if isinstance(old, dict) and old.get("key"):
                remembered[str(old["key"])] = _remembered_source(old)

    fetcher = PoliteFetcher()

    def read(url: str) -> bytes:
        if load_bytes is not None:
            return load_bytes(url)
        cached = cached_image_path(url)
        if cached.is_file() and cached.stat().st_size > 32:
            return cached.read_bytes()
        data = fetcher.get(url)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
        return data

    linked = 0
    pending: list[tuple[dict, str, str, str, Path]] = []
    for row in characters:
        if not isinstance(row, dict) or not row.get("key"):
            continue
        game = str(row.get("game") or "")
        source = _upstream_url(row)
        if not wants_display_thumb(game, source):
            continue
        key = str(row["key"])
        path = art_file(root, game, key)
        stored = remembered.get(key)
        if not needs_rebuild(source, stored, _file_ready(path)):
            _assign_display(row, art_public_url(game, key), source or "")
            linked += 1
            continue
        pending.append((row, game, key, source or "", path))

    built = 0
    failed = 0
    sizes: list[int] = []
    started = clock()
    cap = None if limit <= 0 else limit
    deadline = None if budget_seconds <= 0 else budget_seconds
    for row, game, key, source, path in pending:
        if cap is not None and built + failed >= cap:
            break
        if deadline is not None and (clock() - started) >= deadline:
            break
        try:
            raw = read(source)
            image = Image.open(io.BytesIO(raw))
            image.load()
            try:
                data, width, height, quality = encode_thumbnail(image)
            finally:
                image.close()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            _assign_display(row, art_public_url(game, key), source)
            built += 1
            sizes.append(len(data))
            print(f"art {game} {key} {len(data)} bytes {width}x{height} q{quality}", flush=True)
        except Exception as error:
            failed += 1
            print(f"art {game} {key} kept original: {error}", file=sys.stderr, flush=True)
    attempted = built + failed
    return {
        "linked": linked,
        "built": built,
        "failed": failed,
        "pending": len(pending) - attempted,
        "sizes": sizes,
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Build display thumbnails into art/ and rewrite catalog artUrl")
    parser.add_argument("--catalog", type=Path, default=ROOT / "catalog.json")
    parser.add_argument("--limit", type=int, default=0, help="Max new thumbnails; 0 means no cap")
    parser.add_argument("--budget", type=float, default=0, help="Seconds for new thumbnails; 0 means no cap")
    args = parser.parse_args(argv)
    document = json.loads(args.catalog.read_text(encoding="utf-8"))
    if not isinstance(document.get("characters"), list):
        raise SystemExit(f"{args.catalog} has no characters")
    stats = refresh_display_art(
        document["characters"],
        document,
        args.catalog.parent,
        limit=args.limit,
        budget_seconds=args.budget,
    )
    temporary = args.catalog.with_suffix(args.catalog.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.catalog)
    print(
        f"art thumbs linked {stats['linked']} built {stats['built']} "
        f"failed {stats['failed']} pending {stats['pending']}"
    )
    return 0 if stats["failed"] == 0 and stats["pending"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
