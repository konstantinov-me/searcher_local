"""L1-индексер имён файлов: триграммы имени+пути в SQLite (таблица names_trgm).

Триграммный индекс даёт substring/fuzzy-поиск по именам без Manticore:
запрос 'отчёт2024' → кандидаты через пересечение триграмм → точная проверка LIKE.
"""
from __future__ import annotations

from typing import Any

from ..core.contracts import Artifacts


class NameIndexer:
    name = "name_index"
    target = "sqlite"
    consumes = {"name"}                   # special field: берём rec.name + rec.path
    priority = 5

    def __init__(self, db: Any):
        self.db = db

    def setup(self) -> None:
        pass

    def index(self, rec: Any, art: Artifacts) -> None:
        self.db.index_name(rec.id, rec.name)
        self.db.index_name(-rec.id, rec.path)   # путь — отдельным «виртуальным» id

    def remove(self, file_id: int) -> None:
        self.db.index_name(file_id, "")          # DELETE внутри
        self.db.index_name(-file_id, "")
