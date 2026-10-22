"""Images are stored but not interpreted (OCR/VLM is an optional, disabled feature)."""
from __future__ import annotations

import struct
from pathlib import Path

from app.extractors.base import ExtractionError, ExtractResult, Limits


def image_size(head: bytes) -> tuple[int, int] | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
        w, h = struct.unpack(">II", head[16:24])
        return w, h
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        chunk = head[12:16]
        if chunk == b"VP8X" and len(head) >= 30:
            w = int.from_bytes(head[24:27], "little") + 1
            h = int.from_bytes(head[27:30], "little") + 1
            return w, h
        if chunk == b"VP8 " and len(head) >= 30:
            w, h = struct.unpack("<HH", head[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if chunk == b"VP8L" and len(head) >= 25:
            b = head[21:25]
            w = 1 + (((b[1] & 0x3F) << 8) | b[0])
            h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
            return w, h
    if head.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                i += 1
                continue
            marker = head[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", head[i + 5:i + 9])
                return w, h
            seg = struct.unpack(">H", head[i + 2:i + 4])[0]
            i += 2 + seg
    return None


def extract_image(path: Path, limits: Limits) -> ExtractResult:
    head = path.read_bytes()[:2_000_000]
    size = image_size(head)
    if size is None:
        raise ExtractionError("BAD_IMAGE", "画像サイズを判定できません")
    w, h = size
    if w * h > limits.max_image_pixels:
        raise ExtractionError("TOO_MANY_PIXELS", f"画素数{w}x{h}が上限を超えています")
    res = ExtractResult(stats={"width": w, "height": h, "units_total": 1})
    res.unreadable.append({"location": {}, "reason": "画像の内容は未読（OCR/VLMが無効のため）"})
    return res
