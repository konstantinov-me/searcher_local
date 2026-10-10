"""L1-серчер: поиск по именам файлов (всегда активен, самый быстрый).

Триграммные кандидаты из SQLite → точная проверка подстроки в имени/пути.
Поддержка глобов: `*.pdf`, префикса `name:` (роутинг сделан в fusion/api).
"""
from __future__ import annotations

import fnmatch
from typing import Any, Iterator

from ..core.contracts import Hit, QuerySpec


class FilenameSearcher:
    name = "filename"
    level = 1
    priority = 1

    def __init__(self, db: Any):
        self.db = db

    def search(self, q: QuerySpec) -> Iterator[Hit]:
        needle = q.raw.strip()
        if needle.lower().startswith("name:"):
            needle = needle[5:].strip()
        if not needle:
            return
        is_glob = any(c in needle for c in "*?[]")
        cand_ids = (self._glob_candidates(needle, q.limit) if is_glob
                    else self.db.search_names_candidates(needle, q.limit * 4))
        n = 0
        lower = needle.lower().lstrip("*?.")
        for fid in cand_ids:
            rec = self.db.get_file(abs(fid))
            if rec is None:
                continue
            hay = (rec.name if fid > 0 else rec.path).lower()
            if is_glob:
                ok = fnmatch.fnmatch(rec.name, needle.lower()) or \
                     fnmatch.fnmatch(hay, needle.lower())
                score = 1.0 if ok else 0.0
            else:
                idx = hay.find(lower)
                if idx == -1:
                    continue
                # бонус за совпадение в начале имени и за короткое имя
                score = 1.0 + (0.5 if idx == 0 else 0.0) - min(idx, 30) / 100.0
            if score > 0:
                n += 1
                yield Hit(file_id=abs(fid), score=score, source=self.name,
                          snippet=rec.path, meta={"matched": "path" if fid < 0 else "name"})
                if n >= q.limit:
                    return

    def _glob_candidates(self, pattern: str, limit: int) -> list[int]:
        like = pattern.replace("*", "%").replace("?", "_").replace("[", "[").lower()
        rows = self.db.conn.execute(
            "SELECT id FROM files WHERE lower(name) LIKE ? OR lower(path) LIKE ? LIMIT ?",
            (like, like, limit)).fetchall()
        return [r["id"] for r in rows]
