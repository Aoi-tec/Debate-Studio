"""Extractor contract: bytes on disk -> located text chunks + unreadable regions.

Extractors never execute macros or embedded code and never treat document text as
instructions. Everything they return is untrusted data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.tokens import estimate_tokens

CHUNK_TOKENS = 400


@dataclass
class Limits:
    max_pdf_pages: int = 500
    max_xlsx_cells: int = 200_000
    max_image_pixels: int = 40_000_000
    max_text_chars: int = 5_000_000


@dataclass
class Chunk:
    text: str
    location: dict[str, Any]
    quality: str = "ok"  # ok | low | partial

    def to_dict(self) -> dict:
        return {"text": self.text, "location": self.location, "quality": self.quality,
                "tokens": estimate_tokens(self.text)}


@dataclass
class ExtractResult:
    chunks: list[Chunk] = field(default_factory=list)
    unreadable: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if not self.chunks and self.unreadable:
            return "PARTIAL" if self.stats.get("units_total") else "FAILED"
        if self.unreadable:
            return "PARTIAL"
        return "READY"

    def to_dict(self) -> dict:
        return {"chunks": [c.to_dict() for c in self.chunks], "unreadable": self.unreadable,
                "stats": self.stats, "status": self.status}


class ExtractionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def split_lines_into_chunks(lines: list[str], make_loc, quality: str = "ok", header: str | None = None,
                            max_tokens: int = CHUNK_TOKENS) -> list[Chunk]:
    """Group consecutive lines into ~max_tokens chunks. ``make_loc(start, end)`` gets 1-based line numbers."""
    out: list[Chunk] = []
    buf: list[str] = []
    start = 1
    tok = 0
    for i, line in enumerate(lines, start=1):
        t = estimate_tokens(line)
        if buf and tok + t > max_tokens:
            body = "\n".join(buf)
            out.append(Chunk((header + "\n" + body) if header else body, make_loc(start, i - 1), quality))
            buf, tok, start = [], 0, i
        if t > max_tokens * 2:  # extremely long line: hard split
            step = max_tokens * 2
            for j in range(0, len(line), step):
                out.append(Chunk(line[j:j + step], make_loc(i, i), quality))
            start = i + 1
            continue
        buf.append(line)
        tok += t
    if buf and any(s.strip() for s in buf):
        body = "\n".join(buf)
        out.append(Chunk((header + "\n" + body) if header else body, make_loc(start, len(lines)), quality))
    return [c for c in out if c.text.strip()]
