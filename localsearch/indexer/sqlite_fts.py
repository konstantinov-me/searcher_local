"""Alternate-индексер: SQLite FTS5 вместо Manticore ('нулевые зависимости').

Тот же контракт consumes={'text'}; включается config `indexer_backend: sqlite_fts`.
FTS5 умеет фразы ("a b c") и морфологию (unicode_ru / porter), но НЕ умеет edit-distance
fuzzy — для него sqlite_fts-серчер добавляет trigram-таблицу имён как косвенный fallback.
"""
from __future__ import annotations

from typing import Any

from ..core.contracts import Artifacts


class SqliteFtsIndexer:
    name = "sqlite_fts"
    target = "sqlite"
    consumes = {"text"}
    priority = 20

    def __init__(self, db: Any):
        self.db = db                      # core.db.Database

    def setup(self) -> None:
        pass                              # таблица docs_fts создана в схеме Database

    def index(self, rec: Any, art: Artifacts) -> None:
        if not art.chunks:
            return
        joined_body = "\n\n".join(c.text for c in art.chunks)
        title = art.chunks[0].title or rec.name
        self.db.fts_replace(rec.id, title[:512], joined_body[:4_000_000], rec.path)

    def remove(self, file_id: int) -> None:
        self.db.fts_replace(file_id, "", "", "")   # DELETE внутри fts_replace + пустой insert
