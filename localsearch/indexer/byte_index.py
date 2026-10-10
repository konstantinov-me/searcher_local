"""L3-индексер байтовых последовательностей: trigram-индекс содержимого в SQLite.

Для файлов ≤ byte_index_max_mb индексируются триграммы первых N МБ; поиск:
кандидаты по gram-пересечению → точная проверка Boyer–Moore при серчинге.
Большие файлы пропускаются (деградация: on-disk скан в searcher/bytes_seq).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.contracts import Artifacts

INDEX_WINDOW = 8 << 20                    # 8 МБ головы файла в индекс


class ByteIndexer:
    name = "byte_index"
    target = "sqlite"
    consumes = {"bytes"}                  # special: читаем файл сами (head window)
    priority = 30

    def __init__(self, db: Any, max_file_mb: int = 50):
        self.db = db
        self.max_bytes = max_file_mb << 20

    def setup(self) -> None:
        pass

    def index(self, rec: Any, art: Artifacts) -> None:
        if not art.searchable_bytes or rec.size > self.max_bytes:
            return
        try:
            with open(Path(rec.path), "rb") as f:
                head = f.read(INDEX_WINDOW)
        except OSError:
            return
        self.db.index_bytes(rec.id, head)

    def remove(self, file_id: int) -> None:
        self.db.index_bytes(file_id, b"")        # очищает строки файла
