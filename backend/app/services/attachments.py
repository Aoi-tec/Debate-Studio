"""AttachmentService: upload validation, storage, chair-side extraction jobs.

OWASP-style checks: extension allow-list + magic bytes + container structure
(OOXML: required parts, no macros, no DOCTYPE, bounded decompression, no path
traversal). Originals live outside any web root under random names.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing as mp
import os
import secrets
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from app.core.fsutil import display_name, replace_with_retry, safe_chmod
from app.core.ids import new_id, now_iso
from app.core.settings import AppSettings
from app.core.tokens import estimate_tokens
from app.extractors import extraction_worker, run_extraction
from app.extractors.base import Limits
from app.services.events import EventBus
from app.storage.db import Database, dec

ALLOWED = {
    "txt": "text/plain", "md": "text/markdown", "csv": "text/csv", "json": "application/json",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
}
OOXML_MAIN = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}


class UploadRejected(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def _ext(name: str) -> str:
    base = display_name(name, "")
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def validate_ooxml(path: Path, kind: str, max_uncompressed: int, max_ratio: int) -> None:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise UploadRejected(415, "BAD_CONTAINER", "Officeファイルとして不正です（ZIP構造が壊れています）") from e
    with zf:
        infos = zf.infolist()
        if len(infos) > 10_000:
            raise UploadRejected(415, "TOO_MANY_ENTRIES", "コンテナ内のエントリ数が多すぎます")
        total = 0
        names = set()
        for i in infos:
            n = i.filename
            if n.startswith("/") or ".." in n.split("/") or "\\" in n:
                raise UploadRejected(415, "PATH_TRAVERSAL", "不正なパスを含むファイルです")
            if i.flag_bits & 0x1:
                raise UploadRejected(415, "ENCRYPTED", "暗号化されたOfficeファイルは受け付けません")
            total += i.file_size
            if i.compress_size and i.file_size / max(i.compress_size, 1) > max_ratio and i.file_size > 1_000_000:
                raise UploadRejected(415, "ZIP_BOMB", "圧縮率が異常なため拒否しました")
            names.add(n)
        if total > max_uncompressed:
            raise UploadRejected(413, "UNCOMPRESSED_TOO_LARGE", "展開後サイズが上限を超えています")
        if "[Content_Types].xml" not in names or OOXML_MAIN[kind] not in names:
            raise UploadRejected(415, "TYPE_MISMATCH", f"拡張子 .{kind} と実際の形式が一致しません")
        if any(n.lower().endswith("vbaproject.bin") for n in names):
            raise UploadRejected(415, "MACRO", "マクロを含むファイルは受け付けません")
        ct = zf.read("[Content_Types].xml")[:200_000].decode("utf-8", "replace").lower()
        if "macroenabled" in ct or "vbaproject" in ct:
            raise UploadRejected(415, "MACRO", "マクロ有効形式は受け付けません")
        for part in ("[Content_Types].xml", OOXML_MAIN[kind]):
            head = zf.read(part)[:4096].decode("utf-8", "replace").upper()
            if "<!DOCTYPE" in head or "<!ENTITY" in head:
                raise UploadRejected(415, "XML_DTD", "DTD/外部実体を含むXMLは受け付けません")


def sniff(path: Path, ext: str) -> str:
    """Return canonical kind or raise UploadRejected."""
    with open(path, "rb") as f:
        head = f.read(8192)
    if ext in ("txt", "md", "csv", "json"):
        if b"\x00" in head and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise UploadRejected(415, "TYPE_MISMATCH", "テキスト形式ではありません（バイナリを検出）")
        if head.startswith((b"%PDF", b"PK\x03\x04", b"\x89PNG", b"\x7fELF", b"MZ")):
            raise UploadRejected(415, "TYPE_MISMATCH", "拡張子と実際の形式が一致しません")
        return ext
    if ext == "pdf":
        if not head.startswith(b"%PDF-"):
            raise UploadRejected(415, "TYPE_MISMATCH", "PDFではありません")
        return "pdf"
    if ext in OOXML_MAIN:
        if not head.startswith(b"PK\x03\x04"):
            raise UploadRejected(415, "TYPE_MISMATCH", f".{ext} ではありません")
        return ext
    if ext == "png":
        if not head.startswith(b"\x89PNG\r\n\x1a\n"):
            raise UploadRejected(415, "TYPE_MISMATCH", "PNGではありません")
        return "png"
    if ext in ("jpg", "jpeg"):
        if not head.startswith(b"\xff\xd8\xff"):
            raise UploadRejected(415, "TYPE_MISMATCH", "JPEGではありません")
        return "jpg"
    if ext == "webp":
        if not (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
            raise UploadRejected(415, "TYPE_MISMATCH", "WebPではありません")
        return "webp"
    raise UploadRejected(415, "UNSUPPORTED", f".{ext} は受け付けません")


def location_label(name: str, loc: dict) -> str:
    if "page" in loc:
        return f"{name} / p.{loc['page']}"
    if "sheet" in loc:
        return f"{name} / {loc['sheet']}!{loc.get('cell_range', '')}"
    if "slide" in loc:
        return f"{name} / スライド{loc['slide']}"
    if "paragraph_start" in loc:
        return f"{name} / 段落{loc['paragraph_start']}-{loc['paragraph_end']}"
    if "tables" in loc:
        return f"{name} / 表{','.join(map(str, loc['tables']))}"
    if "line_start" in loc:
        return f"{name} / 行{loc['line_start']}-{loc['line_end']}"
    return name


class AttachmentService:
    def __init__(self, settings: AppSettings, db: Database, bus: EventBus):
        self.s = settings
        self.db = db
        self.bus = bus
        self.in_process = False  # tests may run extraction inline
        self.worker_target = extraction_worker  # must be importable in a spawned child
        self._sem = asyncio.Semaphore(2)
        self._jobs: dict[str, asyncio.Task] = {}
        self.listeners: list[Callable[[str, str], Any]] = []  # (file_id, status) callbacks

    # ------------------------------------------------------------ upload
    async def save_upload(self, filename: str, stream, declared_type: str | None = None) -> dict:
        ext = _ext(filename)
        if ext not in ALLOWED:
            raise UploadRejected(415, "UNSUPPORTED", f".{ext or '(拡張子なし)'} は受け付けません（許可: {', '.join(sorted(set(ALLOWED)))}）")
        file_id = new_id("file")
        stored = secrets.token_hex(16)
        dest = self.s.uploads_dir / stored
        h = hashlib.sha256()
        size = 0
        fd, tmp = tempfile.mkstemp(dir=str(self.s.uploads_dir), prefix=".up.")
        try:
            with os.fdopen(fd, "wb") as f:
                while True:
                    chunk = await stream.read(1024 * 256)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.s.max_file_bytes:
                        raise UploadRejected(413, "TOO_LARGE", f"ファイルサイズが上限 {self.s.max_file_bytes // (1024 * 1024)} MiB を超えています")
                    h.update(chunk)
                    f.write(chunk)
            if size == 0:
                raise UploadRejected(415, "EMPTY", "空のファイルです")
            kind = sniff(Path(tmp), ext)
            if kind in OOXML_MAIN:
                validate_ooxml(Path(tmp), kind, self.s.max_uncompressed_bytes, self.s.max_zip_ratio)
            safe_chmod(tmp, 0o600)
            replace_with_retry(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        display = display_name(filename)[:200]
        self.db.insert("uploaded_files", {
            "file_id": file_id, "original_name": display, "stored_name": stored, "sha256": h.hexdigest(),
            "mime": ALLOWED[ext], "kind": kind, "size": size, "created_at": now_iso(), "status": "PENDING",
            "extract_version": 0, "stats_json": {},
        })
        self.db.audit("file.upload", {"file_id": file_id, "name": display, "size": size})
        self.schedule(file_id)
        return self.public(file_id)

    # ------------------------------------------------------------ processing
    def schedule(self, file_id: str) -> None:
        t = asyncio.get_event_loop().create_task(self._process(file_id))
        self._jobs[file_id] = t

    async def wait(self, file_ids: list[str], timeout: float | None = None) -> None:
        tasks = [self._jobs[f] for f in file_ids if f in self._jobs and not self._jobs[f].done()]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout or (self.s.extraction_timeout_s + 30))

    def _emit(self, file_id: str, type_: str, extra: dict | None = None) -> None:
        sessions = self.db.all("SELECT session_id FROM session_attachments WHERE file_id=?", (file_id,))
        payload = {"file": self.public(file_id), **(extra or {})}
        for s in sessions:
            self.bus.publish(s["session_id"], type_, payload)
        self.bus.publish("_files", type_, payload)

    async def _process(self, file_id: str) -> None:
        async with self._sem:
            f = self.db.one("SELECT * FROM uploaded_files WHERE file_id=?", (file_id,))
            if not f or f["deleted_at"]:
                return
            job_id = new_id("job")
            self.db.insert("file_processing_jobs", {"job_id": job_id, "file_id": file_id, "status": "PROCESSING",
                                                    "progress": 0, "started_at": now_iso()})
            self.db.update("uploaded_files", {"file_id": file_id}, {"status": "PROCESSING", "error": None})
            self._emit(file_id, "file.processing")
            path = self.s.uploads_dir / f["stored_name"]
            limits = asdict(Limits(max_pdf_pages=self.s.max_pdf_pages, max_xlsx_cells=self.s.max_xlsx_cells,
                                   max_image_pixels=self.s.max_image_pixels))
            result = await self._run(str(path), f["kind"], limits)
            if result.get("ok") and not result["chunks"] and f["kind"] not in ("png", "jpg", "webp"):
                result = {"ok": False, "error_code": "NO_TEXT", "error": "読み取れるテキストがありません",
                          "unreadable": result.get("unreadable", [])}
            version = int(f["extract_version"]) + 1
            if not result.get("ok"):
                self.db.update("uploaded_files", {"file_id": file_id}, {
                    "status": "FAILED", "error": f"{result.get('error_code')}: {result.get('error')}",
                    "stats_json": {"unreadable": result.get("unreadable", [])}})
                self.db.update("file_processing_jobs", {"job_id": job_id},
                               {"status": "FAILED", "ended_at": now_iso(), "error_code": result.get("error_code")})
                self._emit(file_id, "file.failed")
                self._notify(file_id, "FAILED")
                return
            rows = []
            for i, c in enumerate(result["chunks"]):
                rows.append((new_id("chk"), file_id, version, i, c["text"], c["tokens"],
                             json.dumps(c["location"], ensure_ascii=False), c["quality"]))
            with self.db.tx():
                self.db.conn.executemany(
                    "INSERT INTO file_chunks(chunk_id,file_id,extract_version,seq,text,tokens,location_json,quality) VALUES (?,?,?,?,?,?,?,?)",
                    rows)
                if self.db.fts_mode != "none":
                    self.db.conn.execute("DELETE FROM file_chunk_fts WHERE file_id=?", (file_id,))
                    self.db.conn.executemany("INSERT INTO file_chunk_fts(text, chunk_id, file_id) VALUES (?,?,?)",
                                             [(r[4], r[0], file_id) for r in rows])
                status = result["status"]
                stats = dict(result["stats"])
                stats["unreadable"] = result["unreadable"]
                stats["chunks"] = len(rows)
                stats["tokens"] = sum(r[5] for r in rows)
                self.db.conn.execute(
                    "UPDATE uploaded_files SET status=?, extract_version=?, stats_json=?, error=NULL WHERE file_id=?",
                    (status, version, json.dumps(stats, ensure_ascii=False), file_id))
                self.db.conn.execute("UPDATE file_processing_jobs SET status=?, progress=1, ended_at=? WHERE job_id=?",
                                     ("DONE", now_iso(), job_id))
            self._emit(file_id, "file.ready" if status == "READY" else "file.partial")
            self._notify(file_id, status)

    def _notify(self, file_id: str, status: str) -> None:
        for cb in list(self.listeners):
            try:
                cb(file_id, status)
            except Exception:  # noqa: BLE001
                pass

    async def _run(self, path: str, kind: str, limits: dict) -> dict:
        if self.in_process:
            return await asyncio.to_thread(run_extraction, path, kind, limits)
        ctx = mp.get_context("spawn")
        fd, out = tempfile.mkstemp(dir=str(self.s.uploads_dir), prefix=".ex.")
        os.close(fd)
        proc = ctx.Process(target=self.worker_target, args=(path, kind, limits, out), daemon=True)
        proc.start()
        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + self.s.extraction_timeout_s
            while proc.is_alive():
                if loop.time() > deadline:
                    proc.terminate()
                    proc.join(5)
                    return {"ok": False, "error_code": "TIMEOUT", "error": "解析が制限時間を超えました"}
                await asyncio.sleep(0.1)
            proc.join(1)
            try:
                return json.loads(Path(out).read_text(encoding="utf-8") or "{}") or {
                    "ok": False, "error_code": "EXTRACTOR_CRASH", "error": "解析プロセスが異常終了しました"}
            except (OSError, ValueError):
                return {"ok": False, "error_code": "EXTRACTOR_CRASH", "error": "解析プロセスが異常終了しました"}
        finally:
            try:
                os.unlink(out)
            except OSError:
                pass

    # ------------------------------------------------------------ queries
    def public(self, file_id: str) -> dict:
        f = self.db.one("SELECT * FROM uploaded_files WHERE file_id=?", (file_id,))
        if not f:
            return {}
        stats = dec(f["stats_json"], {})
        return {
            "file_id": f["file_id"], "name": f["original_name"], "kind": f["kind"], "size": f["size"],
            "status": "DELETED" if f["deleted_at"] else f["status"], "error": f["error"],
            "extract_version": f["extract_version"], "created_at": f["created_at"],
            "pages": stats.get("pages"), "sheets": stats.get("sheets"), "slides": stats.get("slides"),
            "chunks": stats.get("chunks"), "tokens": stats.get("tokens"),
            "unreadable": stats.get("unreadable", []), "sha256": f["sha256"],
        }

    def list(self) -> list[dict]:
        rows = self.db.all("SELECT file_id FROM uploaded_files WHERE deleted_at IS NULL ORDER BY created_at DESC")
        return [self.public(r["file_id"]) for r in rows]

    def preview(self, file_id: str, limit: int = 200) -> dict:
        f = self.db.one("SELECT * FROM uploaded_files WHERE file_id=? AND deleted_at IS NULL", (file_id,))
        if not f:
            return {}
        chunks = self.db.all(
            "SELECT seq, text, location_json, quality FROM file_chunks WHERE file_id=? AND extract_version=? ORDER BY seq LIMIT ?",
            (file_id, f["extract_version"], limit))
        return {
            "file": self.public(file_id),
            "chunks": [{"seq": c["seq"], "text": c["text"], "location": dec(c["location_json"], {}),
                        "label": location_label(f["original_name"], dec(c["location_json"], {})),
                        "quality": c["quality"]} for c in chunks],
        }

    def path_for_download(self, file_id: str) -> tuple[Path, str, str] | None:
        f = self.db.one("SELECT * FROM uploaded_files WHERE file_id=? AND deleted_at IS NULL", (file_id,))
        if not f:
            return None
        return self.s.uploads_dir / f["stored_name"], f["original_name"], f["mime"] or "application/octet-stream"

    def delete(self, file_id: str) -> None:
        f = self.db.one("SELECT * FROM uploaded_files WHERE file_id=? AND deleted_at IS NULL", (file_id,))
        if not f:
            raise KeyError(file_id)
        with self.db.tx():
            self.db.conn.execute("DELETE FROM file_chunks WHERE file_id=?", (file_id,))
            if self.db.fts_mode != "none":
                self.db.conn.execute("DELETE FROM file_chunk_fts WHERE file_id=?", (file_id,))
            self.db.conn.execute("UPDATE chair_evidence SET status='invalidated', quote=NULL WHERE file_id=?", (file_id,))
            self.db.conn.execute("UPDATE uploaded_files SET deleted_at=?, status='DELETED' WHERE file_id=?",
                                 (now_iso(), file_id))
        try:
            (self.s.uploads_dir / f["stored_name"]).unlink()
        except OSError:
            pass
        self.db.audit("file.delete", {"file_id": file_id})
        self._notify(file_id, "DELETED")

    def session_bytes(self, session_id: str) -> tuple[int, int]:
        r = self.db.one(
            "SELECT COUNT(*) n, COALESCE(SUM(u.size),0) b FROM session_attachments a JOIN uploaded_files u ON u.file_id=a.file_id WHERE a.session_id=? AND u.deleted_at IS NULL",
            (session_id,))
        return int(r["n"]), int(r["b"])

    def chunk_tokens(self, file_id: str) -> int:
        f = self.db.one("SELECT extract_version FROM uploaded_files WHERE file_id=?", (file_id,))
        if not f:
            return 0
        r = self.db.one("SELECT COALESCE(SUM(tokens),0) t FROM file_chunks WHERE file_id=? AND extract_version=?",
                        (file_id, f["extract_version"]))
        return int(r["t"])


__all__ = ["AttachmentService", "UploadRejected", "location_label", "estimate_tokens"]
