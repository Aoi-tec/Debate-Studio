"""DocumentRetriever — chair-only full-text search over extracted chunks.

SQLite FTS5 (trigram tokenizer when available) for terms of 3+ chars, LIKE for
2-char Japanese terms, then Python-side scoring. Participants never reach this.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.storage.db import Database, dec

_STOP = {"いつ", "どこ", "なに", "何か", "こと", "もの", "ため", "よう", "など", "これ", "それ", "あれ", "必要",
         "資料", "確認", "について", "ですか", "ください", "the", "and", "for", "what", "when", "which"}
_TOKEN = re.compile(r"[一-龯々〆ヵヶ]+|[ァ-ヴー]+|[A-Za-z0-9][A-Za-z0-9_\-\.]*")


def extract_terms(text: str, max_terms: int = 24) -> list[str]:
    terms: list[str] = []
    for m in _TOKEN.finditer(text or ""):
        t = m.group(0).strip(".-_")
        if len(t) < 2 or t.lower() in _STOP:
            continue
        terms.append(t)
        if re.match(r"^[一-龯々〆ヵヶ]{4,}$", t):  # split long kanji compounds into 2/3-grams
            for n in (3, 2):
                for i in range(0, len(t) - n + 1):
                    sub = t[i:i + n]
                    if sub not in _STOP:
                        terms.append(sub)
    seen: list[str] = []
    for t in terms:
        if t not in seen:
            seen.append(t)
    return seen[:max_terms]


@dataclass
class Hit:
    chunk_id: str
    file_id: str
    file_version: int
    text: str
    tokens: int
    location: dict
    quality: str
    score: float


class DocumentRetriever:
    def __init__(self, db: Database):
        self.db = db

    def _current(self, file_ids: list[str]) -> dict[str, int]:
        if not file_ids:
            return {}
        q = ",".join("?" for _ in file_ids)
        rows = self.db.all(
            f"SELECT file_id, extract_version FROM uploaded_files WHERE file_id IN ({q}) AND deleted_at IS NULL AND status IN ('READY','PARTIAL')",
            file_ids)
        return {r["file_id"]: r["extract_version"] for r in rows}

    def all_chunks(self, file_ids: list[str]) -> list[Hit]:
        cur = self._current(file_ids)
        out: list[Hit] = []
        for fid, ver in cur.items():
            for r in self.db.all("SELECT * FROM file_chunks WHERE file_id=? AND extract_version=? ORDER BY seq", (fid, ver)):
                out.append(Hit(r["chunk_id"], fid, ver, r["text"], r["tokens"], dec(r["location_json"], {}), r["quality"], 0.0))
        return out

    def search(self, file_ids: list[str], query: str, *, limit: int = 8, token_budget: int = 3000,
               extra_terms: list[str] | None = None) -> tuple[list[Hit], list[str]]:
        cur = self._current(file_ids)
        if not cur:
            return [], []
        terms = extract_terms(query)
        for t in extra_terms or []:
            for x in extract_terms(t):
                if x not in terms:
                    terms.append(x)
        if not terms:
            return [], []
        fq = ",".join("?" for _ in cur)
        cand: dict[str, dict] = {}
        long_terms = [t for t in terms if len(t) >= 3]
        if long_terms and self.db.fts_mode != "none":
            match = " OR ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
            try:
                rows = self.db.all(
                    f"SELECT chunk_id FROM file_chunk_fts WHERE file_chunk_fts MATCH ? AND file_id IN ({fq}) LIMIT 400",
                    [match, *cur.keys()])
                for r in rows:
                    cand[r["chunk_id"]] = {}
            except Exception:  # noqa: BLE001 - malformed query: fall back to LIKE
                pass
        like_terms = [t for t in terms if len(t) < 3 or self.db.fts_mode == "none"]
        for t in like_terms[:10]:
            rows = self.db.all(
                f"SELECT chunk_id FROM file_chunks WHERE file_id IN ({fq}) AND text LIKE ? LIMIT 200",
                [*cur.keys(), f"%{t}%"])
            for r in rows:
                cand[r["chunk_id"]] = {}
        hits: list[Hit] = []
        for cid in cand:
            r = self.db.one("SELECT * FROM file_chunks WHERE chunk_id=?", (cid,))
            if not r or cur.get(r["file_id"]) != r["extract_version"]:
                continue
            text = r["text"]
            score = 0.0
            for t in terms:
                c = text.count(t)
                if c:
                    score += (1.0 + min(c, 5) * 0.2) * (len(t) ** 1.2)
            if score <= 0:
                continue
            hits.append(Hit(cid, r["file_id"], r["extract_version"], text, r["tokens"], dec(r["location_json"], {}),
                            r["quality"], score))
        hits.sort(key=lambda h: -h.score)
        out: list[Hit] = []
        used = 0
        for h in hits:
            if len(out) >= limit:
                break
            if used + h.tokens > token_budget and out:
                continue
            out.append(h)
            used += h.tokens
        return out, terms
