"""Portrait vs sample cross-check. No network."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from PIL import Image

from scripts.catalog_lib import (
    HAIR_CROSSCHECK,
    HAIR_MISMATCH_DELTA_E,
    build_checked_hair,
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


class HairCrossCheckTest(unittest.TestCase):
    def test_large_delta_is_low_and_has_no_primary(self) -> None:
        sample = parse_hex("#F9BBBD")
        display = parse_hex("#6B9F9D")
        distance = delta_e(sample, display)
        self.assertGreater(distance, HAIR_MISMATCH_DELTA_E)
        hair = build_checked_hair(
            sample,
            None,
            "HIGH",
            "https://example.test/sample.webp",
            display,
            display_is_portrait=True,
        )
        self.assertIsNone(hair["primary"])
        self.assertIsNone(hair["secondary"])
        self.assertEqual("LOW", hair["confidence"])
        self.assertEqual("sample-mismatch", hair["reason"])
        self.assertEqual("#F9BBBD", hair["sampleColor"])
        self.assertEqual("#6B9F9D", hair["displayColor"])
        self.assertEqual(HAIR_CROSSCHECK, hair["crossCheck"])
        self.assertTrue(hair_is_settled({"artKind": "PORTRAIT", "hair": hair}))

    def test_small_delta_keeps_the_sample_primary(self) -> None:
        sample = parse_hex("#F6C2BF")
        display = parse_hex("#F4BEBB")
        self.assertLessEqual(delta_e(sample, display), HAIR_MISMATCH_DELTA_E)
        hair = build_checked_hair(
            sample,
            parse_hex("#FFFFFF"),
            "HIGH",
            "https://example.test/sample.webp",
            display,
            display_is_portrait=True,
        )
        self.assertEqual("#F6C2BF", hair["primary"])
        self.assertEqual("#FFFFFF", hair["secondary"])
        self.assertEqual("HIGH", hair["confidence"])
        self.assertNotIn("reason", hair)
        self.assertEqual(HAIR_CROSSCHECK, hair["crossCheck"])

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

    def test_extract_one_uses_the_cross_check(self) -> None:
        pink = parse_hex("#F9BBBD")
        green = parse_hex("#6B9F9D")
        character = _row()
        image = Image.new("RGBA", (32, 48), (249, 187, 189, 255))
        with (
            patch("scripts.fetch_catalog.load_image", return_value=image),
            patch("scripts.fetch_catalog.extract_hair", return_value=ExtractedHair(pink, None, "HIGH")),
            patch("scripts.fetch_catalog.classify_art", return_value=ArtKindVerdict(PORTRAIT, 1.0, 0.0)),
            patch("scripts.fetch_catalog.inspect_image", return_value=(PORTRAIT, green)),
        ):
            extract_one(None, character)  # type: ignore[arg-type]
        self.assertIsNone(character["hair"]["primary"])
        self.assertEqual("LOW", character["hair"]["confidence"])
        self.assertEqual("sample-mismatch", character["hair"]["reason"])

        close = _row()
        with (
            patch("scripts.fetch_catalog.load_image", return_value=image),
            patch("scripts.fetch_catalog.extract_hair", return_value=ExtractedHair(pink, None, "HIGH")),
            patch("scripts.fetch_catalog.classify_art", return_value=ArtKindVerdict(PORTRAIT, 1.0, 0.0)),
            patch("scripts.fetch_catalog.inspect_image", return_value=(PORTRAIT, pink)),
        ):
            extract_one(None, close)  # type: ignore[arg-type]
        self.assertEqual("#F9BBBD", close["hair"]["primary"])
        self.assertEqual("HIGH", close["hair"]["confidence"])
        self.assertNotIn("reason", close["hair"])

    def test_poster_display_does_not_reject_the_sample(self) -> None:
        pink = parse_hex("#F9BBBD")
        character = _row()
        image = Image.new("RGBA", (32, 48), (249, 187, 189, 255))
        with (
            patch("scripts.fetch_catalog.load_image", return_value=image),
            patch("scripts.fetch_catalog.extract_hair", return_value=ExtractedHair(pink, None, "HIGH")),
            patch("scripts.fetch_catalog.classify_art", return_value=ArtKindVerdict(PORTRAIT, 1.0, 0.0)),
            patch("scripts.fetch_catalog.inspect_image", return_value=(POSTER, None)),
        ):
            extract_one(None, character)  # type: ignore[arg-type]
        self.assertEqual("#F9BBBD", character["hair"]["primary"])

    def test_zzz_without_cross_check_is_not_reused(self) -> None:
        fresh = _row()
        previous = _row()
        previous["hair"] = {"primary": "#F9BBBD", "secondary": None, "confidence": "HIGH", "sampledFrom": fresh["hairSampleUrl"]}
        previous["artKind"] = "PORTRAIT"
        self.assertFalse(hair_can_reuse(previous, fresh))
        previous["hair"]["crossCheck"] = HAIR_CROSSCHECK
        self.assertTrue(hair_can_reuse(previous, fresh))

    def test_other_games_keep_a_settled_swatch(self) -> None:
        fresh = _row("gi:1")
        fresh["game"] = "genshin"
        fresh["work"] = "原神"
        previous = dict(fresh)
        previous["hair"] = {"primary": "#112233", "confidence": "HIGH"}
        previous["artKind"] = "PORTRAIT"
        self.assertTrue(hair_can_reuse(previous, fresh))


if __name__ == "__main__":
    unittest.main()
