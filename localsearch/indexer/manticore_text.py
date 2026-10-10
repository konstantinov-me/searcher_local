"""Индексер текстовых чанков в Manticore Search (основной путь L2).

consumes={'text'}; каждый Chunk файла → документ docs с id = hash(file_id, ordinal),
чтобы REPLACE был идемпотентен. Экземпляр создаёт app.py после подключения клиента
(инстанс-регистрация); при недоступности Manticore включается alternate sqlite_fts.
"""
from __future__ import annotations

import hashlib
from typing import Any

from ..core.contracts import Artifacts


class ManticoreTextIndexer:
    name = "manticore_text"
    target = "manticore"
    consumes = {"text"}                  # 'text' => берём Artifacts.chunks
    priority = 10

    def __init__(self, client: Any):
        self.mc = client

    def setup(self) -> None:
        if self.mc.connected:
            self.mc.ensure_schema()

    def index(self, rec: Any, art: Artifacts) -> None:
        if not self.mc.connected or not art.chunks:
            return
        for c in art.chunks:
            doc_id = int.from_bytes(
                hashlib.blake2b(f"{rec.id}:{c.ordinal}".encode(), digest_size=8).digest(),
                "big") >> 1
            self.mc.replace_doc(
                doc_id=doc_id, file_id=rec.id, chunk=c.ordinal,
                title=(c.title or rec.name)[:512], body=c.text,
                path=rec.path, ext=rec.ext, mime=rec.mime,
                size=rec.size, mtime=rec.mtime,
                owner=getattr(rec, "owner_id", 0) or 0)

    def remove(self, file_id: int) -> None:
        if self.mc.connected:
            self.mc.delete_by_file(file_id)
