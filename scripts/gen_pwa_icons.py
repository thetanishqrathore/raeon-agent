#!/usr/bin/env python3
"""Generate the Helix PWA icon set into web/public/icons/ (plan item E5).

Design: dark-teal (#041c1c — the web DS LENS_0 canvas) rounded square with a
cream (#ffe6cb — DS midground accent) four-point sparkle plus a small
companion sparkle. Drawn at 4x supersample and LANCZOS-downscaled.

Outputs (all PNG):
  helix-192.png / helix-512.png          purpose "any"    (rounded, alpha corners)
  helix-maskable-192.png / -512.png      purpose "maskable" (full-bleed bg,
                                          glyph inside the ~80% safe zone)
  helix-apple-touch-180.png              iOS home screen (full-bleed, opaque)

Deps: Pillow only (a core hermes-agent dependency). ImageMagick is not
installed on the VM, hence this committed script (re-run any time:
  ./venv/bin/python scripts/gen_pwa_icons.py ).
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

TEAL = (4, 28, 28, 255)      # #041c1c
CREAM = (255, 230, 203, 255)  # #ffe6cb
OUT_DIR = Path(__file__).resolve().parent.parent / "web" / "public" / "icons"
SS = 4  # supersample factor


def _sparkle_points(cx: float, cy: float, outer: float, inner_ratio: float = 0.20):
    """8-vertex four-point sparkle: N/E/S/W tips, concave diagonals."""
    pts = []
    inner = outer * inner_ratio
    for i in range(8):
        ang = math.radians(90 - i * 45)  # start at N, clockwise
        r = outer if i % 2 == 0 else inner
        pts.append((cx + r * math.cos(ang), cy - r * math.sin(ang)))
    return pts


def _draw_glyph(draw: ImageDraw.ImageDraw, size: int, scale: float) -> None:
    """Main sparkle slightly low-left + small companion sparkle upper-right."""
    c = size / 2
    main_r = size * scale
    main_cx, main_cy = c - size * 0.03, c + size * 0.03
    draw.polygon(_sparkle_points(main_cx, main_cy, main_r), fill=CREAM)
    small_r = main_r * 0.34
    small_cx = main_cx + main_r * 0.78
    small_cy = main_cy - main_r * 0.82
    draw.polygon(_sparkle_points(small_cx, small_cy, small_r), fill=CREAM)


def _render(size: int, *, rounded: bool, glyph_scale: float, opaque: bool) -> Image.Image:
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0) if not opaque else TEAL)
    draw = ImageDraw.Draw(img)
    if rounded:
        radius = int(s * 0.22)
        draw.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=TEAL)
    else:
        draw.rectangle([0, 0, s, s], fill=TEAL)
    _draw_glyph(draw, s, glyph_scale)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jobs = [
        # (filename, size, rounded, glyph_scale, opaque)
        ("helix-192.png", 192, True, 0.34, False),
        ("helix-512.png", 512, True, 0.34, False),
        # Maskable: full-bleed; glyph stays inside the 80%-diameter safe zone
        # (scale 0.27 => glyph diameter ~54% of canvas, comfortably safe).
        ("helix-maskable-192.png", 192, False, 0.27, True),
        ("helix-maskable-512.png", 512, False, 0.27, True),
        # iOS ignores transparency and applies its own corner mask.
        ("helix-apple-touch-180.png", 180, False, 0.30, True),
    ]
    for name, size, rounded, scale, opaque in jobs:
        img = _render(size, rounded=rounded, glyph_scale=scale, opaque=opaque)
        path = OUT_DIR / name
        img.save(path, format="PNG", optimize=True)
        print(f"wrote {path.relative_to(OUT_DIR.parent.parent.parent)} ({size}x{size})")


if __name__ == "__main__":
    main()
