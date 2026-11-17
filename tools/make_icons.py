"""Draws the app icon (a green pulse line on black) as PNGs and an SVG, using only the standard library.
    python tools/make_icons.py
"""
import math
import struct
import zlib
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
# the pulse: x, y in 0..1
PTS = [(0.14, 0.56), (0.34, 0.56), (0.42, 0.56), (0.50, 0.28), (0.60, 0.74), (0.68, 0.56), (0.86, 0.56)]
GREEN = (34, 197, 94)


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy or 1)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def render(size):
    half, aa = size * 0.028, 1.2
    pts = [(x * size, y * size) for x, y in PTS]
    dot = pts[-1]
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            d = min(seg_dist(x + 0.5, y + 0.5, *pts[i], *pts[i + 1]) for i in range(len(pts) - 1))
            d = min(d, math.hypot(x + 0.5 - dot[0], y + 0.5 - dot[1]) - size * 0.022)
            a = max(0.0, min(1.0, (half + aa / 2 - d) / aa))
            row += bytes(int(c * a) for c in GREEN)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def svg():
    pts = " ".join(f"{x * 100:.1f},{y * 100:.1f}" for x, y in PTS)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" fill="#000"/>'
            f'<polyline points="{pts}" fill="none" stroke="#22c55e" stroke-width="5.6" stroke-linecap="round" stroke-linejoin="round"/>'
            f'<circle cx="{PTS[-1][0] * 100:.1f}" cy="{PTS[-1][1] * 100:.1f}" r="4.4" fill="#22c55e"/></svg>')


if __name__ == "__main__":
    WEB.mkdir(exist_ok=True)
    for name, size in (("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)):
        (WEB / name).write_bytes(render(size))
        print("wrote", name)
    (WEB / "icon.svg").write_text(svg())
