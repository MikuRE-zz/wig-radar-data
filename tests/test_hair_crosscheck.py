"""Sample-file dedupe. A portrait/sample color gap is diagnostic only. No network."""

from __future__ import annotations

import hashlib
import io
import unittest
from unittest.mock import patch

from PIL import Image

from scripts.catalog_lib import (
    build_checked_hair,
    flag_duplicate_samples,
    hair_can_reuse,
    hair_is_settled,
)
from scripts.fetch_catalog import extract_one
from scripts.haircolor import PORTRAIT, POSTER, ArtKindVerdict, ExtractedHair, delta_e, parse_hex


def _row(key: str = "zzz:1621") -> dict:
    return {
        "key": key,
        "name": key,
        "aliases": [],
        "work": "绝区零",
        "game": "zzz",
        "kind": "GAME",
        "status": "EVERGREEN",
        "artUrl": "https://example.test/art.webp",
        "hairSampleUrl": "https://example.test/sample.webp",
        "source": "nanoka-zzz",
    }


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (8, 8), (249, 187, 189, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


class HairCrossCheckTest(unittest.TestCase):
    def test_large_delta_stays_high(self) -> None:
        sample = parse_hex("#F9BBBD")
        display = parse_hex("#6B9F9D")
        self.assertGreater(delta_e(sample, display), 45)
        hair = build_checked_hair(
            sample,
            None,
            "HIGH",
            "https://example.test/sample.webp",
            display,
            display_is_portrait=True,
            sample_sha1="abc",
        )
        self.assertEqual("#F9BBBD", hair["primary"])
        self.assertEqual("HIGH", hair["confidence"])
        self.assertNotIn("reason", hair)
        self.assertEqual("#F9BBBD", hair["sampleColor"])
        self.assertEqual("#6B9F9D", hair["displayColor"])
        self.assertGreater(hair["deltaE"], 45)
        self.assertEqual("abc", hair["sampleSha1"])
        self.assertNotIn("crossCheck", hair)

    def test_non_portrait_display_is_not_compared(self) -> None:
        hair = build_checked_hair(
            parse_hex("#F9BBBD"),
            None,
            "HIGH",
            "https://example.test/sample.webp",
            parse_hex("#6B9F9D"),
            display_is_portrait=False,
        )
        self.assertEqual("#F9BBBD", hair["primary"])
        self.assertEqual("HIGH", hair["confidence"])
        self.assertNotIn("deltaE", hair)
        self.assertNotIn("sampleSha1", hair)

    def test_duplicate_group_is_low_without_a_primary(self) -> None:
        shared = "f8e2e6e310"
        rows = [
            {"key": "zzz:1581", "hair": {"primary": "#F9BBBD", "secondary": "#FFFFFF", "confidence": "HIGH", "sampleSha1": shared, "deltaE": 23.8}},
            {"key": "zzz:1621", "hair": {"primary": "#F9BBBD", "secondary": None, "confidence": "HIGH", "sampleSha1": shared, "deltaE": 52.9}},
            {"key": "zzz:1011", "hair": {"primary": "#DADCDE", "secondary": None, "confidence": "HIGH", "sampleSha1": "unique", "deltaE": 52.4}},
            {"key": "gi:1", "hair": {"primary": "#112233", "secondary": None, "confidence": "HIGH"}},
        ]
        groups = flag_duplicate_samples(rows)
        self.assertEqual([["zzz:1581", "zzz:1621"]], groups)
        for row in rows[:2]:
            self.assertIsNone(row["hair"]["primary"])
            self.assertIsNone(row["hair"]["secondary"])
            self.assertEqual("LOW", row["hair"]["confidence"])
            self.assertEqual("sample-duplicate", row["hair"]["reason"])
            self.assertEqual(shared, row["hair"]["sampleSha1"])
            self.assertTrue(hair_is_settled(row))
        self.assertEqual("#DADCDE", rows[2]["hair"]["primary"])
        self.assertEqual("HIGH", rows[2]["hair"]["confidence"])
        self.assertNotIn("reason", rows[2]["hair"])
        self.assertEqual("#112233", rows[3]["hair"]["primary"])
        self.assertEqual("HIGH", rows[3]["hair"]["confidence"])

    def test_same_key_is_not_a_duplicate_group(self) -> None:
        rows = [
            {"key": "zzz:1", "hair": {"primary": "#112233", "confidence": "HIGH", "sampleSha1": "only"}},
            {"key": "zzz:1", "hair": {"primary": "#112233", "confidence": "HIGH", "sampleSha1": "only"}},
        ]
        self.assertEqual([], flag_duplicate_samples(rows))
        self.assertEqual("#112233", rows[0]["hair"]["primary"])

    def test_extract_one_keeps_high_and_records_the_sample_sha1(self) -> None:
        pink = parse_hex("#F9BBBD")
        green = parse_hex("#6B9F9D")
        raw = _png()
        character = _row()
        with (
            patch("scripts.fetch_catalog.read_image_bytes", return_value=raw),
            patch("scripts.fetch_catalog.extract_hair", return_value=ExtractedHair(pink, None, "HIGH")),
            patch("scripts.fetch_catalog.classify_art", return_value=ArtKindVerdict(PORTRAIT, 1.0, 0.0)),
            patch("scripts.fetch_catalog.inspect_image", return_value=(PORTRAIT, green)),
        ):
            extract_one(None, character)  # type: ignore[arg-type]
        self.assertEqual("#F9BBBD", character["hair"]["primary"])
        self.assertEqual("HIGH", character["hair"]["confidence"])
        self.assertNotIn("reason", character["hair"])
        self.assertEqual(hashlib.sha1(raw).hexdigest(), character["hair"]["sampleSha1"])
        self.assertGreater(character["hair"]["deltaE"], 45)

    def test_poster_display_does_not_reject_the_sample(self) -> None:
        pink = parse_hex("#F9BBBD")
        character = _row()
        with (
            patch("scripts.fetch_catalog.read_image_bytes", return_value=_png()),
            patch("scripts.fetch_catalog.extract_hair", return_value=ExtractedHair(pink, None, "HIGH")),
            patch("scripts.fetch_catalog.classify_art", return_value=ArtKindVerdict(PORTRAIT, 1.0, 0.0)),
            patch("scripts.fetch_catalog.inspect_image", return_value=(POSTER, None)),
        ):
            extract_one(None, character)  # type: ignore[arg-type]
        self.assertEqual("#F9BBBD", character["hair"]["primary"])
        self.assertIn("sampleSha1", character["hair"])

    def test_settled_zzz_swatch_is_reused_without_a_cross_check(self) -> None:
        fresh = _row()
        previous = _row()
        previous["hair"] = {
            "primary": "#F9BBBD",
            "secondary": None,
            "confidence": "HIGH",
            "sampledFrom": fresh["hairSampleUrl"],
            "sampleSha1": "abc",
        }
        previous["artKind"] = "PORTRAIT"
        self.assertTrue(hair_can_reuse(previous, fresh))

    def test_other_games_keep_a_settled_swatch(self) -> None:
        fresh = _row("gi:1")
        fresh["game"] = "genshin"
        fresh["work"] = "原神"
        previous = dict(fresh)
        previous["hair"] = {"primary": "#112233", "confidence": "HIGH"}
        previous["artKind"] = "PORTRAIT"
        self.assertTrue(hair_can_reuse(previous, fresh))

    def test_art_kind_follows_the_display_image(self) -> None:
        pink = parse_hex("#F9BBBD")
        character = _row()
        with (
            patch("scripts.fetch_catalog.read_image_bytes", return_value=_png()),
            patch("scripts.fetch_catalog.extract_hair", return_value=ExtractedHair(pink, None, "HIGH")),
            patch("scripts.fetch_catalog.classify_art", return_value=ArtKindVerdict(POSTER, 0.0, 40.0)),
            patch("scripts.fetch_catalog.inspect_image", return_value=(PORTRAIT, pink)),
        ):
            extract_one(None, character)  # type: ignore[arg-type]
        self.assertEqual("PORTRAIT", character["artKind"])
        self.assertEqual("#F9BBBD", character["hair"]["primary"])
        self.assertEqual("HIGH", character["hair"]["confidence"])
        self.assertEqual(character["hairSampleUrl"], character["hair"]["sampledFrom"])
        self.assertIn("sampleSha1", character["hair"])

    def test_poster_display_still_drops_a_poster_sample(self) -> None:
        character = _row()
        with (
            patch("scripts.fetch_catalog.read_image_bytes", return_value=_png()),
            patch("scripts.fetch_catalog.extract_hair", return_value=ExtractedHair(parse_hex("#112233"), None, "HIGH")),
            patch("scripts.fetch_catalog.classify_art", return_value=ArtKindVerdict(POSTER, 0.0, 40.0)),
            patch("scripts.fetch_catalog.inspect_image", return_value=(POSTER, None)),
        ):
            extract_one(None, character)  # type: ignore[arg-type]
        self.assertEqual("POSTER", character["artKind"])
        self.assertIsNone(character["hair"]["primary"])
        self.assertEqual("LOW", character["hair"]["confidence"])
        self.assertEqual(character["hairSampleUrl"], character["hair"]["sampledFrom"])
        self.assertTrue(hair_is_settled(character))

    def test_settled_low_sample_is_reused_until_the_sample_url_changes(self) -> None:
        previous = _row()
        previous["artKind"] = "PORTRAIT"
        previous["hair"] = {
            "primary": None,
            "secondary": None,
            "confidence": "LOW",
            "sampledFrom": previous["hairSampleUrl"],
            "sampleSha1": "abc",
        }
        self.assertTrue(hair_is_settled(previous))
        self.assertTrue(hair_can_reuse(previous, _row()))
        changed = _row()
        changed["hairSampleUrl"] = "https://example.test/other.webp"
        self.assertFalse(hair_can_reuse(previous, changed))
        poster = _row("ak:1")
        poster["artKind"] = "POSTER"
        poster["hair"] = {"primary": None, "confidence": "LOW", "sampledFrom": None}
        self.assertTrue(hair_is_settled(poster))
        self.assertTrue(hair_can_reuse(poster, _row("ak:1")))


if __name__ == "__main__":
    unittest.main()
