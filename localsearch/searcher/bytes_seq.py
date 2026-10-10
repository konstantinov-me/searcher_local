"""L3-серчер: точная последовательность байт внутри файлов.

Синтаксис запроса: `bytes:DEADBEEF` / `hex:de ad be ef` / `bytes:"\x89PNG"`.
Пайплайн: trigram-кандидаты (bytegrams) → Boyer–Moore-Horspool точная проверка
по содержимому файла (окно индексации + хвост доп. чтением). Для файлов без
индекса (> лимита) — on-disk скан потоково (медленно, но работает).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

from ..core.contracts import Hit, QuerySpec

_HEX = re.compile(r"[0-9a-fA-F]{2}")


def parse_bytes_query(raw: str) -> bytes | None:
    s = raw.strip()
    low = s.lower()
    if low.startswith(("bytes:", "hex:")):
        s = s.split(":", 1)[1].strip().strip('"')
        if low.startswith("hex:"):
            h = re.sub(r"\s+", "", s)
            try:
                return bytes.fromhex(h) if len(h) % 2 == 0 else None
            except ValueError:
                return None
        return s.encode("utf-8", errors="surrogateescape")
    if re.fullmatch(r"(?:[0-9a-fA-F]{2}[ ]?){3,}", s):
        h = re.sub(r"\s+", "", s)
        try:
            return bytes.fromhex(h)
        except ValueError:
            return None
    return None


def horspool_find(hay: bytes, needle: bytes) -> int:
    """Boyer–Moore-Horspool: первая позиция needle в hay или -1."""
    n, m = len(hay), len(needle)
    if m == 0 or n < m:
        return -1
    shift = {b: m for b in range(256)}
    for i in range(m - 1):
        shift[needle[i]] = m - 1 - i
    i = 0
    while i <= n - m:
        j = m - 1
        while j >= 0 and hay[i + j] == needle[j]:
            j -= 1
        if j < 0:
            return i
        i += shift[hay[i + m - 1]]
    return -1


class BytesSeqSearcher:
    name = "bytes_seq"
    level = 3
    priority = 30
    SCAN_CHUNK = 1 << 20
    OVERLAP = 4096

    def __init__(self, db: Any):
        self.db = db

    def search(self, q: QuerySpec) -> Iterator[Hit]:
        needle = parse_bytes_query(q.raw)
        if not needle or len(needle) < 3:
            return
        produced = set()
        for fid in self.db.byte_candidates(needle, q.limit * 3):
            rec = self.db.get_file(fid)
            if rec is None:
                continue
            pos = self._verify(rec.path, needle)
            if pos is not None:
                produced.add(fid)
                yield Hit(file_id=fid, score=1.0, source=self.name,
                          snippet=f"offset {pos}: {needle[:16].hex()}{'…' if len(needle) > 16 else ''}",
                          span=(pos, pos + len(needle)),
                          meta={"offset": pos, "len": len(needle)})
                if len(produced) >= q.limit:
                    return
        # деградация: файлы сверх лимита индекса не покрыты триграммами —
        # полный on-disk обход только если явно попросили (?deep=1 в filters)
        if q.filters.get("deep"):
            for rec in self.db.conn.execute(
                    "SELECT id, path FROM files WHERE status='indexed'").fetchall():
                if rec["id"] in produced:
                    continue
                pos = self._scan_file(rec["path"], needle)
                if pos is not None:
                    yield Hit(file_id=rec["id"], score=0.5, source=self.name,
                              snippet=f"offset {pos} (deep scan)",
                              span=(pos, pos + len(needle)),
                              meta={"offset": pos, "deep": True})

    # ------------------------- проверка ------------------------- #

    def _verify(self, path: str, needle: bytes) -> int | None:
        try:
            with open(Path(path), "rb") as f:
                data = f.read(8 << 20)
        except OSError:
            return None
        pos = horspool_find(data, needle)
        if pos >= 0:
            return pos
        return self._scan_file(path, needle, start=len(data) - len(needle))

    def _scan_file(self, path: str, needle: bytes, start: int = 0) -> int | None:
        try:
            with open(Path(path), "rb") as f:
                f.seek(start)
                tail = b""
                off = start
                while chunk := f.read(self.SCAN_CHUNK):
                    buf = tail + chunk
                    p = horspool_find(buf, needle)
                    if p >= 0:
                        return off + p
                    keep = min(len(needle) - 1, len(buf))
                    tail = buf[-keep:] if keep > 0 else b""
                    off += len(buf) - len(tail)
        except OSError:
            return None
        return None
