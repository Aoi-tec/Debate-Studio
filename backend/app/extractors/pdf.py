from __future__ import annotations

from pathlib import Path

from app.extractors.base import Chunk, ExtractionError, ExtractResult, Limits, split_lines_into_chunks


def extract_pdf(path: Path, limits: Limits) -> ExtractResult:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    raise ExtractionError("ENCRYPTED", "パスワード保護されたPDFは解析できません")
            except Exception as e:  # noqa: BLE001
                raise ExtractionError("ENCRYPTED", "パスワード保護されたPDFは解析できません") from e
        n = len(reader.pages)
    except ExtractionError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ExtractionError("PARSE_ERROR", f"PDFを解析できません: {type(e).__name__}") from e
    if n > limits.max_pdf_pages:
        raise ExtractionError("TOO_MANY_PAGES", f"ページ数{n}が上限{limits.max_pdf_pages}を超えています")
    res = ExtractResult(stats={"pages": n, "units_total": n})
    ok = 0
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception as e:  # noqa: BLE001
            res.unreadable.append({"location": {"page": i}, "reason": f"抽出エラー: {type(e).__name__}"})
            continue
        if not text:
            res.unreadable.append({"location": {"page": i},
                                   "reason": "テキスト層なし（画像のみのページの可能性。OCRは未有効）"})
            continue
        ok += 1
        lines = text.split("\n")
        chunks = split_lines_into_chunks(lines, lambda s, e, p=i: {"page": p, "line_start": s, "line_end": e})
        res.chunks.extend(chunks)
    res.stats["pages_with_text"] = ok
    res.stats["success_rate"] = ok / n if n else 0.0
    if not res.chunks and n:
        res.chunks = []
    return res


__all__ = ["extract_pdf", "Chunk"]
