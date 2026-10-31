"""L1-серчер: поиск по именам файлов (всегда активен, самый быстрый).

Триграммные кандидаты из SQLite → точная проверка подстроки в имени/пути.
Поддержка глобов: `*.pdf`, префикса `name:` (роутинг сделан в fusion/api).
"""
from __future__ import annotations

import fnmatch
import re
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
        owner = q.filters.get("owner")          # клиент-сервер: None => все файлы
        is_glob = any(c in needle for c in "*?[]")
        words = [w for w in re.findall(r"\w+", needle.lower(), re.UNICODE) if len(w) >= 3]
        cand_ids: set[int] = set()
        if is_glob:
            cand_ids.update(self._glob_candidates(needle, q.limit, owner))
        else:
            # кандидаты по триграммам всего запроса и по каждому слову отдельно —
            # иначе многословный автозапрос ("документ альфа") не найдёт короткое
            # имя "a.txt": ни одно слово не даёт пересечения с общими триграммами
            terms = [needle] + ([w for w in words if w != needle] if q.level == "auto" else [])
            for t in terms:
                cand_ids.update(self.db.search_names_candidates(t, q.limit * 4, owner))
            cand_ids = sorted(cand_ids)
        n = 0
        # для L1 в составе auto-роутинга достаточно слова из запроса:
        # "документ альфа" ищем как подстроку по каждому слову (имена файлов
        # обычно короткие); явные префиксы name:/glob работают как раньше
        words = words if q.level == "auto" and not is_glob else []
        lower = needle.lower().lstrip("*?.")
        for fid in cand_ids:
            rec = self.db.get_file(abs(fid))
            if rec is None:
                continue
            if owner is not None and rec.owner_id != owner:   # defense-in-depth
                continue
            hay = (rec.name if fid > 0 else rec.path).lower()
            if is_glob:
                ok = fnmatch.fnmatch(rec.name, needle.lower()) or \
                     fnmatch.fnmatch(hay, needle.lower())
                score = 1.0 if ok else 0.0
            else:
                idx = hay.find(lower)
                if idx == -1 and words:
                    best_idx, hit_word = -1, ""
                    for w in words:
                        i2 = hay.find(w)
                        if i2 != -1 and (best_idx == -1 or i2 < best_idx):
                            best_idx, hit_word = i2, w
                    idx = best_idx
                    if idx != -1:
                        lower_eff = hit_word
                else:
                    lower_eff = lower
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

    def _glob_candidates(self, pattern: str, limit: int,
                         owner_id: int | None = None) -> list[int]:
        like = pattern.replace("*", "%").replace("?", "_").lower()
        owner_sql = "AND owner_id=?" if owner_id is not None else ""
        params = [like, like] + ([owner_id] if owner_id is not None else []) + [limit]
        rows = self.db.conn.execute(
            f"SELECT id FROM files WHERE (lower(name) LIKE ? OR lower(path) LIKE ?)"
            f" {owner_sql} LIMIT ?", params).fetchall()
        return [r["id"] for r in rows]
