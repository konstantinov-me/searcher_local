"""L2-серчер: слова, фразы и НЕЧЁТКИЙ текст — целиком на Manticore Search.

Роутинг режима по синтаксису запроса (пользователь сам выбирает сложность):
  "точная фраза"        → phrase mode (MATCH('"a b c"'))
  ~с опечаткой           → fuzzy mode (@@(word) burst edit-distance)
  иначе                 → fulltext (морфология en,ru + BM25 + операторы AND/OR/NOT)
Если бэкенд sqlite_fts — тот же контракт поверх FTS5 (без @@-fuzzy).
"""
from __future__ import annotations

from typing import Any, Iterator

from ..core.contracts import Hit, QuerySpec


class PhrasesSearcher:
    name = "phrases"
    level = 2
    priority = 10

    def __init__(self, backend: Any):
        self.be = backend                 # ManticoreClient | SqliteFtsBackend

    def search(self, q: QuerySpec) -> Iterator[Hit]:
        raw = q.raw.strip()
        if not raw:
            return
        mode = "fulltext"
        needle = raw
        if raw.startswith('"') and raw.endswith('"') and len(raw) > 2:
            mode, needle = "phrase", raw[1:-1]
        elif raw.startswith("~"):          # явный fuzzy-маркер пользователя
            mode, needle = "fuzzy", raw[1:]
        filters_sql = self._filters(q)
        rows = self.be.search(needle, mode=mode, limit=q.limit, filters_sql=filters_sql)
        for r in rows:
            yield Hit(
                file_id=int(r["file_id"]),
                score=float(r.get("weight", r.get("rank", 0))),
                source=self.name,
                snippet=str(r.get("snip", r.get("body", ""))[:400]),
                meta={"chunk": r.get("chunk"), "path": r.get("path")})

    @staticmethod
    def _filters(q: QuerySpec) -> str:
        """SQL-условия поверх атрибутов индекса из фильтров UI."""
        parts: list[str] = []
        f = q.filters
        if f.get("ext"):
            vals = ",".join(f"'{e}'" for e in f["ext"])
            parts.append(f"ext IN ({vals})")
        if f.get("path_prefix"):
            parts.append(f"path LIKE '{f['path_prefix'].replace(chr(39), chr(39)*2)}%'")
        if f.get("mtime_after"):
            parts.append(f"mtime >= {int(f['mtime_after'])}")
        if f.get("size_max"):
            parts.append(f"size <= {int(f['size_max'])}")
        return ("AND " + " AND ".join(parts)) if parts else ""


class SqliteFtsBackend:
    """Минимальный бэкенд с тем же API .search(), что ManticoreClient (для sqlite_fts)."""

    def __init__(self, db: Any):
        self.db = db

    def search(self, query: str, mode: str = "fulltext", limit: int = 50,
               filters_sql: str = "") -> list[dict[str, Any]]:
        if mode == "phrase":
            match = f'"{query.replace(chr(34), "")}"'
        elif mode == "fuzzy":
            # FTS5 не умеет edit distance — эмулируем trigram-кандидатами по body нельзя;
            # деградация: обычный OR-запрос по буквенным биграммам слова
            bg = {query[i:i + 2].lower() for i in range(len(query) - 1)} or {query.lower()}
            match = " OR ".join(f"{b}* " for b in list(bg)[:8])
        else:
            match = query
        sql = (f"SELECT file_id, title, snippet(docs_fts,1,'<b>','</b>','…',8) AS snip,"
               f" bm25(docs_fts) AS weight FROM docs_fts WHERE docs_fts MATCH ? LIMIT ?")
        out = []
        for row in self.db.conn.execute(sql, (match, limit)).fetchall():
            d = dict(row)
            d["weight"] = -d["weight"]      # bm25 в FTS5 отрицательный: меньше = лучше
            out.append(d)
        return out
