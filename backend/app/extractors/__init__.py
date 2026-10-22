"""Chair-only document extractors. Entry point: ``run_extraction``."""
from __future__ import annotations

import json
from pathlib import Path

from app.extractors.base import ExtractionError, ExtractResult, Limits


def run_extraction(path: str, kind: str, limits: dict) -> dict:
    lim = Limits(**limits)
    p = Path(path)
    try:
        if kind in ("txt", "md", "csv", "json"):
            from app.extractors.text import extract_text
            res = extract_text(p, kind, lim)
        elif kind == "pdf":
            from app.extractors.pdf import extract_pdf
            res = extract_pdf(p, lim)
        elif kind == "docx":
            from app.extractors.office import extract_docx
            res = extract_docx(p, lim)
        elif kind == "xlsx":
            from app.extractors.office import extract_xlsx
            res = extract_xlsx(p, lim)
        elif kind == "pptx":
            from app.extractors.office import extract_pptx
            res = extract_pptx(p, lim)
        elif kind in ("png", "jpg", "webp"):
            from app.extractors.image import extract_image
            res = extract_image(p, lim)
        else:
            raise ExtractionError("UNSUPPORTED", f"未対応の形式: {kind}")
        return {"ok": True, **res.to_dict()}
    except ExtractionError as e:
        return {"ok": False, "error_code": e.code, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error_code": "EXTRACTOR_CRASH", "error": f"{type(e).__name__}"}


def extraction_worker(path: str, kind: str, limits: dict, out_path: str) -> None:
    """Subprocess entry point: write result JSON to ``out_path``."""
    try:
        import resource  # POSIX only; best-effort memory cap (not enforced on macOS)
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024 ** 3, 2 * 1024 ** 3))
    except Exception:  # noqa: BLE001
        pass
    result = run_extraction(path, kind, limits)
    Path(out_path).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


__all__ = ["run_extraction", "extraction_worker", "ExtractResult", "ExtractionError", "Limits"]
