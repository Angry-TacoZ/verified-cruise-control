#!/usr/bin/env python
"""Regenerate synthetic listing photos (committed public fixtures).

Draws obviously-synthetic car-ish test cards with Pillow — no real
dealership or stock imagery, so these are safe to distribute publicly.
Run from the repo root:  .venv/Scripts/python.exe fixtures/generate.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "synthetic-listings" / "photos"

CARDS = [
    ("front.jpg", "JPEG", (64, 120, 200), "FRONT 3/4 VIEW"),
    ("interior.png", "PNG", (70, 160, 110), "DASHBOARD / CONTROLS"),
    ("wheel.webp", "WEBP", (150, 110, 70), "WHEEL / SENSOR AREA"),
]


def draw_card(path: Path, fmt: str, bg: tuple[int, int, int], caption: str) -> None:
    img = Image.new("RGB", (800, 600), bg)
    draw = ImageDraw.Draw(img)
    # Backdrop grid.
    for x in range(0, 800, 80):
        draw.line([(x, 0), (x, 600)], fill=(255, 255, 255, 60))
    for y in range(0, 600, 80):
        draw.line([(0, y), (800, y)], fill=(255, 255, 255, 60))
    # Car-ish silhouette.
    draw.rounded_rectangle([120, 300, 680, 470], radius=30, fill=(25, 30, 40))
    draw.rounded_rectangle([220, 220, 580, 320], radius=40, fill=(35, 45, 60))
    draw.ellipse([200, 420, 300, 520], fill=(15, 15, 20), outline="white", width=4)
    draw.ellipse([500, 420, 600, 520], fill=(15, 15, 20), outline="white", width=4)
    # Synthetic banner.
    draw.rectangle([0, 0, 800, 64], fill=(180, 30, 30))
    draw.text((400, 32), "SYNTHETIC FIXTURE — NOT A REAL VEHICLE", fill="white", anchor="mm")
    draw.text((400, 560), caption, fill="white", anchor="mm")
    draw.text((400, 96), path.name, fill="white", anchor="mm")
    img.save(path, format=fmt)
    print(f"wrote {path} ({path.stat().st_size} bytes)")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fmt, bg, caption in CARDS:
        draw_card(OUT / name, fmt, bg, caption)


if __name__ == "__main__":
    main()
