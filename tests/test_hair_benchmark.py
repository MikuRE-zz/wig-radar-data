"""Spec 5.1 benchmark. CIE76 ΔE against a hand-sampled midtone must be ≤ 15."""

from __future__ import annotations

import unittest
from pathlib import Path

from PIL import Image

from scripts.haircolor import delta_e, extract_hair, hex_color, parse_hex, pixels_from_rgba

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

CASES = [
    ("suoming.png", "#A7192F"),
    ("luokexi.png", "#6B9F9D"),
    ("xinyuehu.png", "#EBEAEC"),
    ("lucy.png", "#F8ECF4"),
    ("vodyanitsa.png", "#2B6983"),
    ("maomao.png", "#21383F"),
]


class HairExtractorBenchmarkTest(unittest.TestCase):
    def test_six_characters(self) -> None:
        deltas = []
        for filename, expected_hex in CASES:
            path = FIXTURES / filename
            self.assertTrue(path.exists(), f"missing fixture {filename}")
            pixels, width, height = pixels_from_rgba(Image.open(path))
            extracted = extract_hair(pixels, width, height)
            self.assertIsNotNone(extracted, f"{filename} produced no hair color")
            assert extracted is not None
            got = extracted.primary
            expected = parse_hex(expected_hex)
            delta = delta_e(got, expected)
            deltas.append((filename, hex_color(got), expected_hex, delta, extracted.confidence))
            self.assertEqual("HIGH", extracted.confidence, filename)
            self.assertLessEqual(
                delta,
                15.0,
                f"{filename} got {hex_color(got)} ΔE={delta:.2f} expected {expected_hex}",
            )
        for filename, got, expected, delta, confidence in deltas:
            print(f"{filename} {got} vs {expected} ΔE={delta:.2f} {confidence}")


if __name__ == "__main__":
    unittest.main()
