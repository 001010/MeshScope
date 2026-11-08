"""Render the MeshScope icon (a small mesh graph) to .ico and .png with no third-party deps."""

import math
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BG = (17, 24, 32)
EDGE = (52, 211, 153)
NODE = (110, 231, 183)
HUB = (96, 165, 250)

NODES = [(0.50, 0.50), (0.24, 0.27), (0.76, 0.24), (0.80, 0.70), (0.30, 0.78), (0.14, 0.55)]
EDGES = [(0, 1), (0, 2), (0, 3), (0, 4), (1, 2), (2, 3), (3, 4), (4, 5), (5, 1)]


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def render(size: int, ss: int = 4) -> bytes:
    """Return RGBA bytes, supersampled for anti-aliasing."""
    out = bytearray()
    radius_bg = 0.22
    lw = max(0.035, 1.6 / size)
    nr = max(0.075, 2.2 / size)
    for y in range(size):
        for x in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(ss):
                for sx in range(ss):
                    u = (x + (sx + 0.5) / ss) / size
                    v = (y + (sy + 0.5) / ss) / size
                    # rounded square mask
                    qx, qy = max(abs(u - 0.5) - (0.5 - radius_bg), 0), max(abs(v - 0.5) - (0.5 - radius_bg), 0)
                    if math.hypot(qx, qy) > radius_bg:
                        continue
                    col = BG
                    for a, b in EDGES:
                        if seg_dist(u, v, *NODES[a], *NODES[b]) < lw:
                            col = EDGE
                            break
                    for i, (nx, ny) in enumerate(NODES):
                        r = nr * (1.35 if i == 0 else 1.0)
                        if math.hypot(u - nx, v - ny) < r:
                            col = HUB if i == 0 else NODE
                    acc[0] += col[0]; acc[1] += col[1]; acc[2] += col[2]; acc[3] += 255
            n = ss * ss
            a = acc[3] / n
            if a:
                out += bytes([int(acc[0] / (acc[3] / 255)), int(acc[1] / (acc[3] / 255)), int(acc[2] / (acc[3] / 255)), int(a)])
            else:
                out += b"\0\0\0\0"
    return bytes(out)


def png(size: int, rgba: bytes) -> bytes:
    raw = b"".join(b"\0" + rgba[y * size * 4:(y + 1) * size * 4] for y in range(size))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ico(images):
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    return header + entries + blobs


def main():
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    imgs = []
    for s in (16, 24, 32, 48, 64, 128, 256):
        imgs.append((s, png(s, render(s, 4 if s <= 64 else 2))))
        print("rendered", s, file=sys.stderr)
    (assets / "meshscope.ico").write_bytes(ico(imgs))
    (ROOT / "meshscope" / "static" / "icon.png").write_bytes(dict(imgs)[64])
    print("wrote", assets / "meshscope.ico")


if __name__ == "__main__":
    main()
