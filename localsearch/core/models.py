"""Модели доменных данных (плоские dataclass'ы — легко сериализуются в пулы процессов)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class FileRecord:
    """Один файл в системе. Источник истины — SQLite (core/db.py)."""
    id: int = 0
    path: str = ""
    name: str = ""                      # basename — для L1-поиска по имени
    ext: str = ""
    mime: str = ""
    size: int = 0
    mtime: float = 0.0
    ctime: float = 0.0
    content_hash: str = ""              # blake3/sha256 — дедупликация и инкрементальность
    status: str = "pending"             # pending|extracting|indexed|error|skipped
    error: str | None = None
    extractor: str | None = None        # id сработавшего экстрактора
    parent_id: int | None = None        # для файлов внутри архивов
    member_name: str | None = None      # путь участника внутри архива
    owner_id: int = 0                   # клиент-сервер: кто загрузил (0 = локальный режим)
    namespace: str = "local"            # 'local' | 'upload:<user>' | 'remote:<user>'
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_path(cls, p: Path) -> "FileRecord":
        st = p.stat()
        return cls(
            path=str(p), name=p.name, ext=p.suffix.lower(),
            size=st.st_size, mtime=st.st_mtime, ctime=st.st_ctime,
        )

    def is_stale(self, current_mtime: float, current_size: int) -> bool:
        """Файл изменился => нужна переиндексация (быстрая проверка без хэша)."""
        return abs(self.mtime - current_mtime) > 1e-6 or self.size != current_size


@dataclass(slots=True)
class JobEvent:
    """Событие из сканера/вотчера в очередь индексации."""
    kind: str                   # 'upsert' | 'delete'
    path: str
    file_id: int | None = None
    ts: float = field(default_factory=time.time)
