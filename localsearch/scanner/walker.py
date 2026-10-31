"""Рекурсивный обход дерева директорий (многопоточный, без лимита глубины).

os.scandir + явный стек вместо рекурсии; параллелим по корневым директориям
(ThreadPoolExecutor — обход I/O-bound). Для каждого файла — быстрый stat,
фильтр exclude-глобов, upsert в SQLite как 'pending' и событие в очередь.
Инкрементальность: если mtime/size совпадают с записью 'indexed' — пропускаем.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import queue as q
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable

from ..core.db import Database
from ..core.models import FileRecord, JobEvent

log = logging.getLogger("localsearch.walker")


class Walker:
    def __init__(self, db: Database, events: "q.Queue[JobEvent]",
                 exclude: Iterable[str] = (), follow_symlinks: bool = False):
        self.db = db
        self.events = events
        self.exclude = list(exclude)
        self.follow = follow_symlinks
        self._seen_dirs: set[str] = set()   # защита от симлинк-циклов

    # ------------------------- публичный API ------------------------- #

    def scan(self, roots: list[Path], max_workers: int = 8) -> int:
        """Обход всех корней; возвращает число новых/изменённых файлов."""
        n = 0
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="walk") as ex:
            futures = [ex.submit(self._scan_root, r) for r in roots if r.is_dir()]
            for fut in futures:
                n += fut.result()
        log.info("обход завершён: %d файлов поставлено в индексацию", n)
        return n

    # ------------------------- внутреннее ------------------------- #

    def _excluded(self, p: str) -> bool:
        for pat in self.exclude:
            if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p, f"*/{pat.lstrip('/')}"):
                return True
        return False

    def _scan_root(self, root: Path) -> int:
        """Явный стек: обход в ширину/глубину без рекурсии — любая глубина дерева."""
        count = 0
        stack: list[Path] = [root]
        while stack:
            d = stack.pop()
            key = str(d.resolve(strict=False))
            if key in self._seen_dirs:
                continue
            self._seen_dirs.add(key)
            try:
                with os.scandir(d) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=self.follow):
                                if not self._excluded(entry.path):
                                    stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=True):
                                if self._handle_file(entry):
                                    count += 1
                        except OSError as e:
                            log.debug("пропуск %s: %s", entry.path, e)
            except PermissionError:
                log.warning("нет доступа к %s", d)
        return count

    def _handle_file(self, entry: os.DirEntry) -> bool:
        st = entry.stat(follow_symlinks=False)
        existing = self.db.find_by_path(entry.path)
        if existing and existing.status == "indexed" \
                and not existing.is_stale(st.st_mtime, st.st_size):
            return False                       # ничего не изменилось — пропускаем
        rec = FileRecord.from_path(Path(entry.path))
        file_id = self.db.upsert_file(rec)
        self.db.set_status(file_id, "pending")
        self.events.put(JobEvent(kind="upsert", path=entry.path, file_id=file_id))
        return True
