"""Hair color extraction ported from the wig-radar Kotlin extractor.

The numeric rules match HairExtractor / ArtKindClassifier / ColorMath so the
six-character benchmark (CIE76 ΔE ≤ 15) can run without Android.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def alpha(color: int) -> int:
    return (color >> 24) & 0xFF


def red(color: int) -> int:
    return (color >> 16) & 0xFF


def green(color: int) -> int:
    return (color >> 8) & 0xFF


def blue(color: int) -> int:
    return color & 0xFF


def argb(r: int, g: int, b: int) -> int:
    return (
        (0xFF << 24)
        | (max(0, min(255, r)) << 16)
        | (max(0, min(255, g)) << 8)
        | max(0, min(255, b))
    )


def hex_color(color: int) -> str:
    return f"#{color & 0xFFFFFF:06X}"


def parse_hex(value: str) -> int:
    text = value.strip().lstrip("#")
    return int(text, 16) & 0xFFFFFF


def hsv_of(r: int, g: int, b: int) -> tuple[float, float, float]:
    rf = r / 255.0
    gf = g / 255.0
    bf = b / 255.0
    max_c = max(rf, gf, bf)
    min_c = min(rf, gf, bf)
    d = max_c - min_c
    if d == 0.0:
        h = 0.0
    elif max_c == rf:
        h = (60.0 * ((gf - bf) / d) + 360.0) % 360.0
    elif max_c == gf:
        h = 60.0 * ((bf - rf) / d) + 120.0
    else:
        h = 60.0 * ((rf - gf) / d) + 240.0
    s = 0.0 if max_c == 0.0 else d / max_c
    return h, s, max_c


def _srgb_to_linear(c: int) -> float:
    s = c / 255.0
    if s <= 0.04045:
        return s / 12.92
    return ((s + 0.055) / 1.055) ** 2.4


def _lab_f(t: float) -> float:
    if t > 0.008856:
        return t ** (1.0 / 3.0)
    return 7.787 * t + 16.0 / 116.0


def rgb_to_lab(r: int, g: int, b: int) -> tuple[float, float, float]:
    rl = _srgb_to_linear(r)
    gl = _srgb_to_linear(g)
    bl = _srgb_to_linear(b)
    x = rl * 0.4124564 + gl * 0.3575761 + bl * 0.1804375
    y = rl * 0.2126729 + gl * 0.7151522 + bl * 0.0721750
    z = rl * 0.0193339 + gl * 0.1191920 + bl * 0.9503041
    fx = _lab_f(x / 0.95047)
    fy = _lab_f(y / 1.00000)
    fz = _lab_f(z / 1.08883)
    return 116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)


def delta_e(c1: int, c2: int) -> float:
    a = rgb_to_lab(red(c1), green(c1), blue(c1))
    b = rgb_to_lab(red(c2), green(c2), blue(c2))
    dl = a[0] - b[0]
    da = a[1] - b[1]
    db = a[2] - b[2]
    return math.sqrt(dl * dl + da * da + db * db)


def _median_int(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _median_float(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


@dataclass
class ExtractedHair:
    primary: int
    secondary: int | None
    confidence: str


@dataclass
class _Sample:
    hue: float
    value: float
    r: int
    g: int
    b: int


_BG_DELTA_E = 6.0
_EDGE_TRANSPARENT_SKIP = 0.45
_TALL_ASPECT = 1.55
_HIGH_SUPPORT = 24


def extract_hair(pixels: list[int], width: int, height: int) -> ExtractedHair | None:
    if width <= 0 or height <= 0 or len(pixels) < width * height:
        return None
    background = _background_mask(pixels, width, height)
    min_x, min_y = width, height
    max_x, max_y = -1, -1
    for i, flagged in enumerate(background):
        if flagged or alpha(pixels[i]) < 16:
            continue
        y, x = divmod(i, width)
        if x < min_x:
            min_x = x
        if y < min_y:
            min_y = y
        if x > max_x:
            max_x = x
        if y > max_y:
            max_y = y
    if max_x < 0:
        return None
    subject_w = max_x - min_x + 1
    subject_h = max_y - min_y + 1
    tall = subject_h / subject_w >= _TALL_ASPECT
    fraction = 0.17 if tall else 0.62
    inset = 0.12 if tall else 0.08
    y0 = min_y
    y1 = min_y + max(1, int(subject_h * fraction))
    x0 = min_x + int(subject_w * inset)
    x1 = max_x - int(subject_w * inset)
    if x1 <= x0 or y1 <= y0:
        return None
    stride = max(1, min(x1 - x0, y1 - y0) // 100)

    chromatic: list[_Sample] = []
    pale: list[tuple[int, int, int]] = []
    y = y0
    while y < y1:
        row = y * width
        x = x0
        while x < x1:
            index = row + x
            if not background[index]:
                color = pixels[index]
                if alpha(color) >= 16:
                    r, g, b = red(color), green(color), blue(color)
                    hue, sat, value = hsv_of(r, g, b)
                    skin = 8.0 <= hue <= 42.0 and sat <= 0.50 and value >= 0.52
                    if not skin:
                        if sat >= 0.15 and 0.08 <= value <= 0.93:
                            chromatic.append(_Sample(hue, value, r, g, b))
                        if value >= 0.74 and sat < 0.28:
                            pale.append((r, g, b))
            x += stride
        y += stride

    if len(pale) >= 40 and len(pale) > len(chromatic) * 0.85:
        confidence = "HIGH" if len(pale) >= _HIGH_SUPPORT else "LOW"
        return ExtractedHair(
            primary=argb(
                _median_int([p[0] for p in pale]),
                _median_int([p[1] for p in pale]),
                _median_int([p[2] for p in pale]),
            ),
            secondary=None,
            confidence=confidence,
        )
    if len(chromatic) < 20:
        return None
    buckets: dict[int, list[_Sample]] = {}
    for sample in chromatic:
        key = int(sample.hue / 30.0)
        buckets.setdefault(key, []).append(sample)
    family = max(buckets.values(), key=len)
    family.sort(key=lambda sample: sample.value)
    center = int((len(family) - 1) * 0.40)
    half = max(1, int(len(family) * 0.08))
    start = max(0, center - half)
    end = min(len(family), center + half + 1)
    band = family[start:end]
    if not band:
        return None
    confidence = "HIGH" if len(family) >= _HIGH_SUPPORT else "LOW"
    return ExtractedHair(
        primary=argb(
            _median_int([sample.r for sample in band]),
            _median_int([sample.g for sample in band]),
            _median_int([sample.b for sample in band]),
        ),
        secondary=None,
        confidence=confidence,
    )


def _background_mask(pixels: list[int], width: int, height: int) -> list[bool]:
    band = max(2, int(min(width, height) * 0.015))
    seen: set[int] = set()
    labs: list[tuple[float, float, float]] = []
    edge_count = 0
    edge_transparent = 0

    def take(x: int, y: int) -> None:
        nonlocal edge_count, edge_transparent
        index = y * width + x
        if index in seen:
            return
        seen.add(index)
        color = pixels[index]
        edge_count += 1
        if alpha(color) < 16:
            edge_transparent += 1
        else:
            labs.append(rgb_to_lab(red(color), green(color), blue(color)))

    for y in range(height):
        for x in range(band):
            take(x, y)
        for x in range(max(0, width - band), width):
            take(x, y)
    for x in range(width):
        for y in range(band):
            take(x, y)
        for y in range(max(0, height - band), height):
            take(x, y)

    ref = None
    if labs and edge_transparent / max(1, edge_count) < _EDGE_TRANSPARENT_SKIP:
        ref = (
            _median_float([lab[0] for lab in labs]),
            _median_float([lab[1] for lab in labs]),
            _median_float([lab[2] for lab in labs]),
        )
    background = [False] * (width * height)
    stack = []
    for x in range(width):
        stack.append(x)
        stack.append((height - 1) * width + x)
    for y in range(1, height - 1):
        stack.append(y * width)
        stack.append(y * width + width - 1)
    while stack:
        index = stack.pop()
        if background[index]:
            continue
        if not _is_background(pixels[index], ref):
            continue
        background[index] = True
        y, x = divmod(index, width)
        if x > 0:
            stack.append(index - 1)
        if x + 1 < width:
            stack.append(index + 1)
        if y > 0:
            stack.append(index - width)
        if y + 1 < height:
            stack.append(index + width)
    return background


def _is_background(color: int, ref: tuple[float, float, float] | None) -> bool:
    if alpha(color) < 16:
        return True
    if ref is None:
        return False
    lab = rgb_to_lab(red(color), green(color), blue(color))
    dl = lab[0] - ref[0]
    da = lab[1] - ref[1]
    db = lab[2] - ref[2]
    return math.sqrt(dl * dl + da * da + db * db) <= _BG_DELTA_E


PORTRAIT = "PORTRAIT"
POSTER = "POSTER"
UNKNOWN = "UNKNOWN"

_EDGE_BAND_FRACTION = 0.04
_EDGE_ALPHA_PORTRAIT = 0.35
_EDGE_MEAN_DELTA_E_MAX = 15.0


@dataclass
class ArtKindVerdict:
    kind: str
    edge_alpha_ratio: float
    edge_mean_delta_e: float


def classify_art(pixels: list[int], width: int, height: int) -> ArtKindVerdict:
    if width <= 0 or height <= 0:
        return ArtKindVerdict(UNKNOWN, 0.0, 0.0)
    band = max(2, int(min(width, height) * _EDGE_BAND_FRACTION))
    stride = max(1, min(width, height) // 240)
    top = _region_mean(pixels, width, height, 0, 0, width, band, stride)
    bottom = _region_mean(pixels, width, height, 0, height - band, width, height, stride)
    left = _region_mean(pixels, width, height, 0, 0, band, height, stride)
    right = _region_mean(pixels, width, height, width - band, 0, width, height, stride)
    edges = (top, bottom, left, right)
    total = max(1, sum(edge[0] for edge in edges))
    transparent = sum(edge[1] for edge in edges)
    edge_alpha = transparent / total
    opaque = [edge[2] for edge in edges if edge[2] is not None]
    edge_mean_delta = _max_pair_delta(opaque)
    if edge_alpha >= _EDGE_ALPHA_PORTRAIT:
        kind = PORTRAIT
    elif len(opaque) >= 2 and edge_mean_delta <= _EDGE_MEAN_DELTA_E_MAX:
        kind = PORTRAIT
    else:
        kind = POSTER
    return ArtKindVerdict(kind, edge_alpha, edge_mean_delta)


def _region_mean(
    pixels: list[int],
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    stride: int,
) -> tuple[int, int, int | None]:
    total = 0
    transparent = 0
    r = g = b = 0.0
    opaque = 0
    xs = max(0, min(width, x0))
    xe = max(xs, min(width, x1))
    ys = max(0, min(height, y0))
    ye = max(ys, min(height, y1))
    y = ys
    while y < ye:
        row = y * width
        x = xs
        while x < xe:
            color = pixels[row + x]
            total += 1
            if alpha(color) < 16:
                transparent += 1
            else:
                r += red(color)
                g += green(color)
                b += blue(color)
                opaque += 1
            x += stride
        y += stride
    mean = None
    if opaque > 0:
        n = float(opaque)
        mean = argb(int(r / n), int(g / n), int(b / n))
    return total, transparent, mean


def _max_pair_delta(colors: list[int]) -> float:
    best = 0.0
    for i, left in enumerate(colors):
        for right in colors[i + 1 :]:
            best = max(best, delta_e(left, right))
    return best


def pixels_from_rgba(image) -> tuple[list[int], int, int]:
    """Decode a Pillow image into the ARGB ints the extractor expects."""
    rgba = image.convert("RGBA")
    width, height = rgba.size
    raw = rgba.tobytes()
    count = width * height
    pixels = [0] * count
    for i in range(count):
        offset = i * 4
        r = raw[offset]
        g = raw[offset + 1]
        b = raw[offset + 2]
        a = raw[offset + 3]
        pixels[i] = (a << 24) | (r << 16) | (g << 8) | b
    return pixels, width, height
