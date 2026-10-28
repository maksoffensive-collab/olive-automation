"""Minimal PNG sanity check (standard library only). Not an image decoder: it only proves the
bytes are a well-formed PNG container and reads the size from the IHDR header."""
from __future__ import annotations

import struct
import zlib

from .client import AdbError

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"


def png_size(data: bytes) -> tuple[int, int]:
    """Return (width, height) or raise AdbError if `data` is not a complete, well-formed PNG."""
    if len(data) < 8 + 25 + len(_IEND) or not data.startswith(_SIGNATURE):
        raise AdbError("The screenshot data is not a PNG (bad signature or too short).").saying(
            ru="Данные снимка экрана — не PNG (неверная подпись или слишком мало данных).")
    length, kind = struct.unpack(">I4s", data[8:16])
    if kind != b"IHDR" or length != 13:
        raise AdbError("The screenshot PNG has no valid IHDR header.").saying(
            ru="У снимка экрана нет правильного заголовка PNG.")
    ihdr = data[16:29]
    (crc,) = struct.unpack(">I", data[29:33])
    if zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF != crc:
        raise AdbError("The screenshot PNG header is corrupt (CRC mismatch).").saying(
            ru="Заголовок PNG снимка экрана повреждён (не сходится контрольная сумма).")
    if not data.endswith(_IEND):
        raise AdbError("The screenshot PNG is truncated or corrupted (no IEND trailer); line-ending damage is a common cause.").saying(
            ru="Снимок экрана PNG обрезан или повреждён (нет конца файла).")
    width, height = struct.unpack(">II", ihdr[:8])
    if width == 0 or height == 0:
        raise AdbError("The screenshot PNG reports a zero size.").saying(ru="Снимок экрана PNG нулевого размера.")
    return width, height
