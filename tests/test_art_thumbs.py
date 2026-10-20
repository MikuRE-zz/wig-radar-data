"""Display thumbnail paths, incremental rebuild, and encode size. No network."""

from __future__ import annotations

import io
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from PIL import Image

from scripts.art_thumbs import (
    USER_AGENT,
    PoliteFetcher,
    art_public_url,
    art_relpath,
    encode_thumbnail,
    fit_longest_edge,
    needs_rebuild,
    next_encode,
    refresh_display_art,
    retry_wait_seconds,
    wants_display_thumb,
)


def _png(width: int, height: int) -> bytes:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = image.load()
    for y in range(height):
        for x in range(width // 2, width):
            pixels[x, y] = (40, 80, 160, 255)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class ArtThumbPureTest(unittest.TestCase):
    def test_relpath_replaces_colons_and_public_url_uses_the_catalog_host(self) -> None:
        self.assertEqual("art/hsr/hsr_1015.webp", art_relpath("hsr", "hsr:1015"))
        self.assertEqual("art/endfield/bgm_169197.webp", art_relpath("endfield", "bgm:169197"))
        self.assertEqual(
            "https://raw.githubusercontent.com/MikuRE-zz/wig-radar-data/main/art/hsr/hsr_1015.webp",
            art_public_url("hsr", "hsr:1015"),
        )

    def test_only_nanoka_star_rail_and_wiki_endfield_are_thumbnailed(self) -> None:
        self.assertTrue(wants_display_thumb("hsr", "https://static.nanoka.cc/assets/hsr/avatardrawcard/1015.webp"))
        self.assertTrue(wants_display_thumb("endfield", "https://endfield.wiki.gg/images/Yvonne_Splash_Art.png"))
        self.assertFalse(wants_display_thumb("endfield", "https://lain.bgm.tv/pic/crt/l/poster.jpg"))
        self.assertFalse(wants_display_thumb("genshin", "https://static.nanoka.cc/assets/gi/x.webp"))

    def test_needs_rebuild_skips_an_unchanged_source_with_a_file(self) -> None:
        source = "https://endfield.wiki.gg/images/Yvonne_Splash_Art.png"
        self.assertFalse(needs_rebuild(source, source, True))
        self.assertTrue(needs_rebuild(source, source, False))
        self.assertTrue(needs_rebuild(source, source + "?v=2", True))
        self.assertTrue(needs_rebuild(source, None, True))
        self.assertFalse(needs_rebuild(None, source, False))

    def test_fit_longest_edge_keeps_aspect_and_does_not_upscale(self) -> None:
        self.assertEqual((900, 450), fit_longest_edge(1800, 900))
        self.assertEqual((450, 900), fit_longest_edge(800, 1600))
        self.assertEqual((400, 300), fit_longest_edge(400, 300))
        self.assertEqual((900, 900), fit_longest_edge(900, 900))
        self.assertEqual((900, 100), fit_longest_edge(901, 100))

    def test_next_encode_drops_quality_then_the_long_edge(self) -> None:
        self.assertIsNone(next_encode(900, 80, 1000))
        self.assertEqual((900, 70), next_encode(900, 80, 200_000))
        self.assertEqual((900, 50), next_encode(900, 60, 200_000))
        self.assertEqual((765, 70), next_encode(900, 50, 200_000))
        self.assertIsNone(next_encode(480, 50, 200_000))

    def test_retry_after_wins_and_otherwise_backoff_doubles(self) -> None:
        self.assertEqual(12, retry_wait_seconds("12", 1))
        self.assertEqual(2, retry_wait_seconds(None, 1))
        self.assertEqual(8, retry_wait_seconds("", 3))
        self.assertEqual(60, retry_wait_seconds(None, 8))
        now = datetime(2026, 10, 20, 4, 0, tzinfo=timezone.utc)
        header = "Tue, 20 Oct 2026 04:00:30 GMT"
        self.assertEqual(30, retry_wait_seconds(header, 1, now=now))

    def test_encode_keeps_alpha_and_the_long_edge(self) -> None:
        raw = encode_thumbnail(Image.open(io.BytesIO(_png(1000, 400))))
        data, width, height, quality = raw
        self.assertLessEqual(len(data), 150 * 1024)
        self.assertEqual((900, 360), (width, height))
        self.assertEqual(80, quality)
        image = Image.open(io.BytesIO(data))
        self.assertEqual("RGBA", image.mode)
        self.assertEqual(0, image.getpixel((4, 180))[3])
        self.assertEqual(255, image.getpixel((890, 180))[3])


class _Resp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class PoliteFetcherTest(unittest.TestCase):
    def test_429_waits_retry_after_and_the_next_host_request_waits_a_second(self) -> None:
        clock = {"t": 0.0}
        slept: list[float] = []

        def monotonic() -> float:
            return clock["t"]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock["t"] += seconds

        calls = {"n": 0}

        def opener(request: urllib.request.Request, timeout: float = 0) -> _Resp:
            calls["n"] += 1
            self.assertEqual(USER_AGENT, request.get_header("User-agent"))
            if calls["n"] == 1:
                headers = EmailMessage()
                headers["Retry-After"] = "5"
                raise urllib.error.HTTPError(request.full_url, 429, "slow", headers, io.BytesIO(b""))
            return _Resp(b"\x89PNG" + b"x" * 40)

        fetcher = PoliteFetcher(opener=opener, sleep=sleep, monotonic=monotonic)
        url = "https://endfield.wiki.gg/images/Yvonne_Splash_Art.png"
        self.assertGreater(len(fetcher.get(url)), 32)
        fetcher.get(url)
        self.assertEqual([5.0, 1.0], slept)


class RefreshDisplayArtTest(unittest.TestCase):
    def _row(self, key: str = "bgm:1") -> dict:
        return {
            "key": key,
            "name": "伊冯",
            "game": "endfield",
            "artUrl": "https://endfield.wiki.gg/images/Yvonne_Splash_Art.png",
            "hairSampleUrl": "https://endfield.wiki.gg/images/Yvonne_icon.png",
            "artKind": "PORTRAIT",
        }

    def test_existing_file_is_not_downloaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = self._row()
            path = root / "art" / "endfield" / "bgm_1.webp"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"x" * 64)
            previous = {"characters": [dict(row)]}
            calls: list[str] = []
            stats = refresh_display_art(
                [row],
                previous,
                root,
                limit=5,
                budget_seconds=30,
                load_bytes=lambda url: calls.append(url) or _png(8, 8),
            )
        self.assertEqual([], calls)
        self.assertEqual(1, stats["linked"])
        self.assertEqual(0, stats["built"])
        self.assertEqual(art_public_url("endfield", "bgm:1"), row["artUrl"])
        self.assertEqual("https://endfield.wiki.gg/images/Yvonne_Splash_Art.png", row["artSourceUrl"])
        self.assertEqual(["artUrl", "artSourceUrl", "hairSampleUrl"], list(row)[3:6])

    def test_failure_keeps_the_original_url_and_the_limit_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._row("bgm:1")
            second = self._row("bgm:2")
            second["artUrl"] = "https://endfield.wiki.gg/images/Other_Splash_Art.png"
            calls: list[str] = []

            def load(url: str) -> bytes:
                calls.append(url)
                raise RuntimeError("HTTP 429")

            stats = refresh_display_art(
                [first, second],
                {"characters": []},
                root,
                limit=1,
                budget_seconds=30,
                load_bytes=load,
            )
        self.assertEqual(["https://endfield.wiki.gg/images/Yvonne_Splash_Art.png"], calls)
        self.assertEqual("https://endfield.wiki.gg/images/Yvonne_Splash_Art.png", first["artUrl"])
        self.assertNotIn("artSourceUrl", first)
        self.assertEqual("https://endfield.wiki.gg/images/Other_Splash_Art.png", second["artUrl"])
        self.assertEqual(1, stats["failed"])
        self.assertEqual(1, stats["pending"])

    def test_a_new_source_replaces_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = self._row()
            path = root / "art" / "endfield" / "bgm_1.webp"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"x" * 64)
            old = "https://endfield.wiki.gg/images/Yvonne_Splash_Art.png"
            new = "https://endfield.wiki.gg/images/Yvonne_Splash_Art_2.png"
            row["artUrl"] = new
            previous = {"characters": [{"key": "bgm:1", "artUrl": old, "artSourceUrl": old}]}
            stats = refresh_display_art(
                [row],
                previous,
                root,
                limit=5,
                budget_seconds=30,
                load_bytes=lambda url: _png(32, 16),
            )
            self.assertEqual(1, stats["built"])
            self.assertEqual(new, row["artSourceUrl"])
            self.assertEqual(art_public_url("endfield", "bgm:1"), row["artUrl"])
            with Image.open(path) as saved:
                self.assertEqual("WEBP", saved.format)
                self.assertEqual((32, 16), saved.size)


if __name__ == "__main__":
    unittest.main()
