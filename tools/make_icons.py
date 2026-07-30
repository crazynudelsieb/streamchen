"""Render the streamchen mark (app/static/icon.svg) to the PWA's PNG icons.

An installable app needs raster icons — Android wants 192 and 512, iOS will
not take an SVG for the home screen at all — and the project has no build step
and no image library, so the mark is redrawn here with the standard library:
supersampled shapes, zlib, and PNG's own container format.

Run it when the mark changes:

    python tools/make_icons.py

The output is committed, so nothing at runtime depends on this file.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static"

BG = (0x11, 0x18, 0x27)
FG = (0x16, 0xA3, 0x4A)
WHITE = (0xFF, 0xFF, 0xFF)

SS = 4  # supersampling factor per axis


def rounded_rect(x, y, w, h, r, px, py):
    """Point inside a rectangle with rounded corners."""
    if px < x or px > x + w or py < y or py > y + h:
        return False
    cx = min(max(px, x + r), x + w - r)
    cy = min(max(py, y + r), y + h - r)
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r or (
        x + r <= px <= x + w - r or y + r <= py <= y + h - r
    )


def capsule(x1, y1, x2, y2, width, px, py):
    """Point inside a line segment stroked with round caps."""
    vx, vy = x2 - x1, y2 - y1
    wx, wy = px - x1, py - y1
    length2 = vx * vx + vy * vy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / length2))
    dx, dy = wx - t * vx, wy - t * vy
    return dx * dx + dy * dy <= (width / 2) ** 2


def arc_band(cx, cy, radius, width, px, py, y_max):
    """Top half of a stroked circle."""
    if py > y_max:
        return False
    d = math.hypot(px - cx, py - cy)
    return abs(d - radius) <= width / 2


def sample(px, py):
    """Colour at a point in the 64x64 design space, or None for transparent."""
    if not rounded_rect(0, 0, 64, 64, 14, px, py):
        return None

    # Headphone band: M14 32 v-2 a18 18 0 0 1 36 0 v2, stroke 4.
    if arc_band(32, 30, 18, 4, px, py, y_max=30):
        return FG
    if capsule(14, 30, 14, 32, 4, px, py) or capsule(50, 30, 50, 32, 4, px, py):
        return FG
    # Ear cups: M16 34 v10 and M48 34 v10, stroke 4, round caps.
    if capsule(16, 34, 16, 44, 4, px, py) or capsule(48, 34, 48, 44, 4, px, py):
        return FG

    # Equaliser bars.
    for x, y, w, h in ((24, 28, 4, 12), (30, 22, 4, 24), (36, 30, 4, 8)):
        if capsule(x + w / 2, y + w / 2, x + w / 2, y + h - w / 2, w, px, py):
            return FG

    return BG


def render(size: int) -> bytes:
    """RGBA rows, supersampled."""
    rows = []
    scale = 64 / size
    for row in range(size):
        pixels = bytearray()
        for col in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    px = (col + (sx + 0.5) / SS) * scale
                    py = (row + (sy + 0.5) / SS) * scale
                    colour = sample(px, py)
                    if colour is not None:
                        r += colour[0]
                        g += colour[1]
                        b += colour[2]
                        a += 255
            n = SS * SS
            if a == 0:
                pixels += bytes((0, 0, 0, 0))
            else:
                covered = a // 255
                pixels += bytes((r // covered, g // covered, b // covered, a // n))
        rows.append(bytes(pixels))
    return b"".join(b"\x00" + row for row in rows)


def png(size: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def maskable(size: int) -> bytes:
    """Same mark, drawn smaller so a circular mask cannot clip it."""
    rows = []
    scale = 64 / size
    inset = 0.72  # safe zone: 80% of the canvas, a little more for comfort
    for row in range(size):
        pixels = bytearray()
        for col in range(size):
            r = g = b = 0
            for sy in range(SS):
                for sx in range(SS):
                    px = ((col + (sx + 0.5) / SS) * scale - 32) / inset + 32
                    py = ((row + (sy + 0.5) / SS) * scale - 32) / inset + 32
                    colour = sample(px, py) if 0 <= px <= 64 and 0 <= py <= 64 else None
                    colour = colour or BG
                    r += colour[0]
                    g += colour[1]
                    b += colour[2]
            n = SS * SS
            pixels += bytes((r // n, g // n, b // n, 255))
        rows.append(bytes(pixels))
    return b"".join(b"\x00" + row for row in rows)


if __name__ == "__main__":
    for name, size, fn in (
        ("icon-192.png", 192, render),
        ("icon-512.png", 512, render),
        ("apple-touch-icon.png", 180, maskable),
        ("icon-maskable-512.png", 512, maskable),
        ("favicon-32.png", 32, render),
    ):
        path = OUT / name
        path.write_bytes(png(size, fn(size)))
        print(f"{name}: {path.stat().st_size} bytes")
