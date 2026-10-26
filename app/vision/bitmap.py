"""Pixel data and a small, strict PNG decoder (standard library only).

`Bitmap` is plain RGB data with no provenance (used for templates and as the pixel store of a
`Frame`). Decoder scope: non-interlaced 8-bit PNG, colour types 0 (grey), 2 (RGB), 6 (RGBA).
Alpha is discarded. Anything else is refused with a clear error rather than guessed at.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import PlatformError

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PIXELS = 64_000_000  # refuse absurd images (decompression bombs)


class VisionError(PlatformError):
    """A refusal of the picture code. One a person can meet names itself in `code` and carries its numbers in
    `params`, so a screen can say it in the person's language; the message is the English wording of the same."""

    def __init__(self, message: str = "", *, code: str = "", **params) -> None:
        super().__init__(message)
        self.code = code
        self.params = params


_FAST: list = []                # [the fast module, or None], filled on first use


def fast_module():
    """`app.vision.fast` when OpenCV and NumPy can be loaded, else None (looked up once): the picture search and
    PNG decoding use it when it is there, and work without it."""
    if not _FAST:
        try:
            from . import fast
        except ImportError:
            _FAST.append(None)
        else:
            _FAST.append(fast)
    return _FAST[0]


@dataclass(frozen=True)
class Bitmap:
    width: int
    height: int
    rgb: bytes  # row-major, 3 bytes per pixel

    def __post_init__(self) -> None:
        if not (isinstance(self.width, int) and isinstance(self.height, int)) or self.width <= 0 or self.height <= 0:
            raise VisionError("A bitmap needs a positive integer width and height.")
        if len(self.rgb) != self.width * self.height * 3:
            raise VisionError("Bitmap data length does not match width x height x 3.")

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise VisionError(f"Pixel ({x}, {y}) is outside the {self.width}x{self.height} bitmap.")
        i = (y * self.width + x) * 3
        return self.rgb[i], self.rgb[i + 1], self.rgb[i + 2]

    @classmethod
    def solid(cls, width: int, height: int, color: tuple[int, int, int]) -> "Bitmap":
        return cls(width, height, bytes(color) * (width * height))

    def with_rect(self, x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> "Bitmap":
        """A copy with a filled rectangle (mainly for building synthetic test images)."""
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > self.width or y + h > self.height:
            raise VisionError("Rectangle is outside the bitmap.")
        data = bytearray(self.rgb)
        row = bytes(color) * w
        for yy in range(y, y + h):
            i = (yy * self.width + x) * 3
            data[i:i + 3 * w] = row
        return Bitmap(self.width, self.height, bytes(data))

    def resized(self, w: int, h: int, *, check=None) -> "Bitmap":
        """The picture at `w` x `h`, each new pixel the average of the old ones it covers (area-weighted): an exact
        copy of each pixel when enlarged by a whole number, a fair average when made smaller. Deterministic.
        `check` is called before each new row (it raises to end a long resize)."""
        if isinstance(w, bool) or isinstance(h, bool) or not isinstance(w, int) or not isinstance(h, int) \
                or w <= 0 or h <= 0:
            raise VisionError("A picture is resized to a positive whole width and height.")
        if (w, h) == (self.width, self.height):
            return self

        def spans(old: int, new: int) -> list[list[tuple[int, float]]]:
            step = old / new
            out = []
            for i in range(new):
                start, end = i * step, (i + 1) * step
                parts, p = [], int(start)
                while p < end and p < old:
                    cover = min(end, p + 1) - max(start, p)
                    if cover > 1e-12:
                        parts.append((p, cover / step))
                    p += 1
                out.append(parts)
            return out
        cols, rows = spans(self.width, w), spans(self.height, h)
        src, stride = self.rgb, self.width * 3
        data = bytearray(w * h * 3)
        k = 0
        for row in rows:
            if check is not None:
                check()
            for col in cols:
                r = g = b = 0.0
                for yy, wy in row:
                    base = yy * stride
                    for xx, wx in col:
                        i, wgt = base + xx * 3, wy * wx
                        r += src[i] * wgt
                        g += src[i + 1] * wgt
                        b += src[i + 2] * wgt
                data[k], data[k + 1], data[k + 2] = (min(255, int(r + 0.5)), min(255, int(g + 0.5)),
                                                     min(255, int(b + 0.5)))
                k += 3
        return Bitmap(w, h, bytes(data))

    def crop(self, x: int, y: int, w: int, h: int) -> "Bitmap":
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > self.width or y + h > self.height:
            raise VisionError("Crop rectangle is outside the bitmap.")
        rows = [self.rgb[((yy * self.width) + x) * 3:((yy * self.width) + x + w) * 3] for yy in range(y, y + h)]
        return Bitmap(w, h, b"".join(rows))


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> list[bytearray]:
    stride = width * bpp
    if len(raw) != (stride + 1) * height:
        raise VisionError("PNG pixel data has an unexpected length.")
    rows: list[bytearray] = []
    prev = bytearray(stride)
    pos = 0
    for _ in range(height):
        ftype = raw[pos]
        cur = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if ftype == 0:
            pass
        elif ftype == 1:
            for i in range(bpp, stride):
                cur[i] = (cur[i] + cur[i - bpp]) & 255
        elif ftype == 2:
            cur = bytearray((a + b) & 255 for a, b in zip(cur, prev))
        elif ftype == 3:
            for i in range(stride):
                left = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + ((left + prev[i]) >> 1)) & 255
        elif ftype == 4:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                cur[i] = (cur[i] + pred) & 255
        else:
            raise VisionError(f"PNG uses an unknown filter type ({ftype}).")
        rows.append(cur)
        prev = cur
    return rows


def decode_png(data: bytes) -> Bitmap:
    if not data.startswith(_SIGNATURE):
        raise VisionError("Not a PNG file (bad signature).")
    pos, ihdr, idat, seen_end, animated = 8, None, [], False, False
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        crc = data[pos + 8 + length:pos + 12 + length]
        if len(body) != length or len(crc) != 4:
            raise VisionError("PNG is truncated.")
        if zlib.crc32(kind + body) & 0xFFFFFFFF != struct.unpack(">I", crc)[0]:
            raise VisionError(f"PNG chunk {kind!r} is corrupt (CRC mismatch).")
        pos += 12 + length
        if kind == b"IHDR":
            ihdr = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"acTL":
            animated = True                            # an APNG: its IDAT picture is the one read here
        elif kind == b"IEND":
            seen_end = True
            break
    if ihdr is None or len(ihdr) != 13 or not idat or not seen_end:
        raise VisionError("PNG is incomplete (missing IHDR, IDAT or IEND).")
    width, height, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", ihdr)
    if width == 0 or height == 0 or width * height > MAX_PIXELS:
        raise VisionError("PNG has an invalid or excessive size.")
    if depth != 8 or ctype not in (0, 2, 6) or interlace != 0 or comp != 0 or filt != 0:
        raise VisionError(f"Unsupported PNG (bit depth {depth}, colour type {ctype}, interlace {interlace}); "
                          "only 8-bit non-interlaced grey/RGB/RGBA is supported.")
    bpp = {0: 1, 2: 3, 6: 4}[ctype]
    expected = (width * bpp + 1) * height              # the only inflated size a valid image of this header can have
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(b"".join(idat), expected + 1)         # never inflate more than the header allows
    except zlib.error as exc:
        raise VisionError(f"PNG pixel data cannot be decompressed: {exc}") from None
    if len(raw) > expected or inflater.unconsumed_tail:
        raise VisionError("PNG pixel data is larger than its declared size.")
    if not inflater.eof:
        raise VisionError("PNG pixel data is incomplete (truncated compressed stream).")
    if len(raw) != expected:
        raise VisionError("PNG pixel data has an unexpected length.")
    fast = None if animated else fast_module()      # OpenCV might read an animation's own frame instead
    if fast is not None:                               # the same values, unfiltered in C: a screen in milliseconds
        rgb = fast.decode_png(data, width, height)
        if rgb is not None:
            return Bitmap(width, height, rgb)
    out = bytearray()
    for row in _unfilter(raw, width, height, bpp):
        if ctype == 2:
            out += row
        elif ctype == 6:
            px = bytearray(width * 3)
            px[0::3], px[1::3], px[2::3] = row[0::4], row[1::4], row[2::4]
            out += px
        else:
            px = bytearray(width * 3)
            px[0::3] = px[1::3] = px[2::3] = row
            out += px
    return Bitmap(width, height, bytes(out))


def encode_png(bitmap: Bitmap) -> bytes:
    """A Bitmap as PNG bytes: 8-bit RGB, non-interlaced, one filter type, which `decode_png` reads back exactly.

    The counterpart of `decode_png`, and deliberately the plainest encoder that produces a valid file rather
    than the smallest one: it exists so a frame (or a crop of one) can be handed to something that reads images
    -- an OCR engine, a saved template -- without the vision layer needing an imaging library. Filter 0 (None)
    keeps it simple and fast; zlib does the compressing."""
    if not isinstance(bitmap, Bitmap):
        raise VisionError("encode_png needs a Bitmap.")
    raw = bytearray()
    stride = bitmap.width * 3
    for y in range(bitmap.height):
        raw.append(0)                                  # filter type 0 (None) for every row
        raw += bitmap.rgb[y * stride:(y + 1) * stride]

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", bitmap.width, bitmap.height, 8, 2, 0, 0, 0)
    return (_SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def load_png(path: str | Path) -> Bitmap:
    """Load a local PNG (for templates). Missing, unreadable or invalid files raise VisionError."""
    p = Path(path)
    if not p.is_file():
        raise VisionError(f"Image file not found: {p.name}")
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise VisionError(f"Image file could not be read: {exc}") from None
    return decode_png(data)
