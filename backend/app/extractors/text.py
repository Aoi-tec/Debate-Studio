from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from app.extractors.base import ExtractionError, ExtractResult, Limits, split_lines_into_chunks


def decode_bytes(raw: bytes) -> tuple[str, str, str]:
    """Return (text, encoding, quality)."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace"), "utf-8-sig", "ok"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace"), "utf-16", "ok"
    for enc in ("utf-8", "cp932", "euc_jp"):
        try:
            return raw.decode(enc), enc, "ok"
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1", "low"


def extract_text(path: Path, kind: str, limits: Limits) -> ExtractResult:
    raw = path.read_bytes()
    text, enc, quality = decode_bytes(raw)
    if len(text) > limits.max_text_chars:
        raise ExtractionError("TOO_LARGE", "テキストが上限を超えています")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    res = ExtractResult(stats={"encoding": enc, "units_total": 1})
    if quality == "low":
        res.unreadable.append({"location": {}, "reason": "文字コードを判定できず latin-1 として読み込み（文字化けの可能性）"})
    if kind == "csv":
        try:
            rows = list(csv.reader(io.StringIO(text)))
        except csv.Error as e:
            res.unreadable.append({"location": {}, "reason": f"CSV解析エラー: {e}"})
            rows = [[line] for line in text.split("\n")]
        header = " | ".join(rows[0]) if rows else ""
        body = [" | ".join(r) for r in rows[1:]]
        res.chunks = split_lines_into_chunks(
            body, lambda s, e: {"line_start": s + 1, "line_end": e + 1}, quality, header=f"[列] {header}")
        if not body and header:
            res.chunks = split_lines_into_chunks([header], lambda s, e: {"line_start": 1, "line_end": 1}, quality)
        res.stats.update(rows=len(rows))
        return res
    if kind == "json":
        try:
            obj = json.loads(text)
            text = json.dumps(obj, ensure_ascii=False, indent=1)
            res.stats["json_valid"] = True
        except ValueError:
            res.stats["json_valid"] = False
            res.unreadable.append({"location": {}, "reason": "JSONとして解析できないためテキストとして扱った"})
    lines = text.split("\n")
    res.chunks = split_lines_into_chunks(lines, lambda s, e: {"line_start": s, "line_end": e}, quality)
    res.stats["lines"] = len(lines)
    return res
