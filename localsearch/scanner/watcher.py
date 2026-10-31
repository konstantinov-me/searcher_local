"""Наблюдатель за изменениями файлов (watchdog) с дебаунсом и батчингом событий.

watchdog.obsEmitter пишет события в ту же очередь JobEvent; дебаунс 2 сек на путь
(редакторы пишут файл пачкой событий). Если watchdog не установлен — тихий no-op
(тогда актуальность обеспечивает периодический полный обход walk-сканера).
"""
from __future__ import annotations

import logging
import queue as q
import threading
import time
from pathlib import Path
from typing import Any

from ..core.db import Database
from ..core.models import JobEvent

log = logging.getLogger("localsearch.watcher")


class Watcher:
    DEBOUNCE_S = 2.0

    def __init__(self, db: Database, events: "q.Queue[JobEvent]"):
        self.db = db
        self.events = events
        self._pending: dict[str, float] = {}      # path → deadline
        self._lock = threading.Lock()
        self._observer: Any = None
        self._flusher: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, roots: list[Path]) -> bool:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            log.info("watchdog не установлен — только периодические сканы")
            return False

        outer = self

        class Handler(FileSystemEventHandler):
            def on_created(self, e: Any) -> None:
                if not e.is_directory:
                    outer.touch(e.src_path)

            def on_modified(self, e: Any) -> None:
                if not e.is_directory:
                    outer.touch(e.src_path)

            def on_moved(self, e: Any) -> None:
                outer.touch(e.dest_path)

            def on_deleted(self, e: Any) -> None:
                outer.on_delete(e.src_path)

        self._observer = Observer()
        h = Handler()
        for r in roots:
            try:
                self._observer.schedule(h, str(r), recursive=True)
            except OSError as e:
                log.warning("watch %s: %s", r, e)
        self._observer.start()
        self._flusher = threading.Thread(target=self._flush_loop, daemon=True)
        self._flusher.start()
        log.info("наблюдение за %d корнями", len(roots))
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)

    # ------------------------- внутреннее ------------------------- #

    def touch(self, path_str: str) -> None:
        with self._lock:
            self._pending[path_str] = time.time() + self.DEBOUNCE_S

    def on_delete(self, path_str: str) -> None:
        rec = self.db.find_by_path(path_str)
        self.events.put(JobEvent(kind="delete", path=path_str,
                                 file_id=rec.id if rec else None))

    def _flush_loop(self) -> None:
        while not self._stop.wait(0.5):
            now = time.time()
            due: list[str] = []
            with self._lock:
                for p, deadline in list(self._pending.items()):
                    if now >= deadline:
                        due.append(p)
                        del self._pending[p]
            for p in due:
                path = Path(p)
                if not path.exists():
                    continue
                rec = self.db.find_by_path(p)
                from ..core.models import FileRecord
                if rec is None:
                    rec = FileRecord.from_path(path)
                fid = self.db.upsert_file(rec)
                self.db.set_status(fid, "pending")
                self.events.put(JobEvent(kind="upsert", path=p, file_id=fid))
