"""Minimal PNG encoder (8-bit RGB, no filtering) for captured windows. Standard library only.

The data is stored, not compressed (zlib level 0): a capture is decoded in this process at once and never written
anywhere, and compressing a large screen took 0.1-0.4 s of every look (3440 x 1440) for nothing."""
from __future__ import annotations

import struct
import zlib

_MAX_SIDE = 16384


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_rgb_png_from_bgra(bgra: bytes, width: int, height: int) -> bytes:
    if not (0 < width <= _MAX_SIDE and 0 < height <= _MAX_SIDE):
        raise ValueError("Image dimensions out of range.")
    if len(bgra) != width * height * 4:
        raise ValueError("Pixel data length does not match the dimensions.")
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    stride = width * 3
    raw = b"".join(b"\x00" + bytes(rgb[y * stride:(y + 1) * stride]) for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw, 0))
            + _chunk(b"IEND", b""))
