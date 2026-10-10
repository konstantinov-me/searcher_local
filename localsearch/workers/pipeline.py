"""Конвейер индексации: потребитель очереди событий (probe → extract → index).

Модель исполнения:
  * лёгкие форматы (text, EXIF) — в пуле потоков основного процесса;
  * CPU-тяжёлые (pdf/office/archive/AI) — ProcessPoolExecutor; экстракторы
    обязаны быть picklable (классы на уровне модуля — это и так так);
  * backpressure: очередь ограничена queue_size, сканер блокируется put();
  * ретраи: exception → status='error' + причина; повтор при следующем проходе.

Прогресс публикуется в Event bus → WebSocket /ws/progress.
"""
from __future__ import annotations

import logging
import queue as q
import threading
import time
import traceback
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from ..core.contracts import Artifacts, ExtractCtx, Support
from ..core.db import Database
from ..core.models import FileRecord, JobEvent
from ..extractor.base import content_hash
from ..scanner.probe import probe_file

log = logging.getLogger("localsearch.pipeline")


class PipelineCtx(ExtractCtx):
    """Реализация контекста экстрактора: ленивое чтение + enqueue детей."""

    def __init__(self, path: Path, events: "q.Queue[JobEvent]", db: Database | None = None):
        self.path = path
        self.events = events
        self._db = db
        self.logs: list[str] = []

    def open(self):
        return open(self.path, "rb")

    def read_bytes(self, limit: int | None = None) -> bytes:
        with self.open() as f:
            return f.read(limit) if limit else f.read()

    def read_text(self, max_chars: int) -> str:
        from ..extractor.text import decode_bytes
        b = self.read_bytes(max_chars * 4)
        return decode_bytes(b)[:max_chars]

    def enqueue_child(self, tmp_path: Path, parent_id: int, member_name: str) -> None:
        rec = FileRecord.from_path(tmp_path)
        rec.parent_id, rec.member_name = parent_id, member_name
        if self._db is not None:
            cid = self._db.upsert_file(rec)
            self.events.put(JobEvent(kind="upsert", path=str(tmp_path), file_id=cid))

    def log(self, msg: str) -> None:
        self.logs.append(msg)
        log.debug("[%s] %s", self.path.name, msg)


# ------------------------- работа в процессе-воркере ------------------------- #

def _extract_in_worker(args: tuple) -> dict[str, Any]:
    """Точка входа ProcessPool: probe → выбор экстрактора → Artifacts (picklable)."""
    path_str, extractors_dump = args
    path = Path(path_str)
    probe = probe_file(path)
    chosen = None
    for cls_ref in extractors_dump:
        inst = cls_ref()
        sup = inst.supports(probe)
        if sup is Support.YES:
            chosen = inst
            break
        if sup is Support.FALLBACK and chosen is None:
            chosen = inst
    if chosen is None:
        return {"extractor": None, "artifacts": Artifacts(), "hash": "", "logs": []}
    ctx = PipelineCtx(path, events=q.Queue())
    try:
        art = chosen.extract(_MinimalRec(path, probe), ctx)
    except Exception:
        return {"extractor": chosen.name, "error": traceback.format_exc(limit=3),
                "hash": "", "logs": ctx.logs}
    return {"extractor": chosen.name, "artifacts": art,
            "hash": content_hash(path), "logs": ctx.logs}


class _MinimalRec:
    """Урезанная запись для worker'а (полная есть только в основном процессе)."""
    def __init__(self, path: Path, probe):
        self.id = 0
        self.path, self.name, self.ext = str(path), path.name, probe.ext
        self.mime, self.size, self.mtime = probe.mime, probe.size, 0.0


# ------------------------- основной потребитель ------------------------- #

class IndexPipeline:
    def __init__(self, settings: Any, db: Database, events: "q.Queue[JobEvent]",
                 extractors: list[Any], indexers: list[Any],
                 progress_cb: Callable[[dict], None] | None = None):
        self.st = settings
        self.db = db
        self.events = events
        self.extractors = [e for e in extractors if getattr(e, "name", "") != "archive"]
        self.archive_ext = next((e for e in extractors if getattr(e, "name", "") == "archive"), None)
        self.indexers = indexers
        self.progress_cb = progress_cb
        self.stop_flag = threading.Event()
        self.counters = {"indexed": 0, "errors": 0, "skipped": 0}

    # ------------------------- запуск ------------------------- #

    def run_forever(self) -> None:
        """Поток-потребитель: берёт события, диспетчерит в пул обработки."""
        pool = ThreadPoolExecutor(max_workers=self.st.workers.threads,
                                  thread_name_prefix="index")
        while not self.stop_flag.is_set():
            try:
                ev = self.events.get(timeout=1.0)
            except q.Empty:
                continue
            if ev.kind == "delete":
                self._handle_delete(ev)
            else:
                pool.submit(self.process_event, ev)
        pool.shutdown(wait=True)

    def drain_pending(self) -> None:
        """Синхронная обработка всех pending (для CLI `index` без вотчера)."""
        for rec in list(self.db.iter_pending()):
            self.process_event(JobEvent(kind="upsert", path=rec.path, file_id=rec.id))

    # ------------------------- одна задача ------------------------- #

    def process_event(self, ev: JobEvent) -> None:
        path = Path(ev.path)
        if not path.exists():
            if ev.file_id:
                self._remove_all(ev.file_id)
            return
        rec = self.db.get_file(ev.file_id) if ev.file_id else None
        if rec is None:
            rec = FileRecord.from_path(path)
            rec.id = self.db.upsert_file(rec)
        self.db.set_status(rec.id, "extracting")
        try:
            art, extractor_name, chash = self._extract(rec, path)
            # дедуп: тот же хэш содержимого уже проиндексирован под другим путём?
            dup = self.db.conn.execute(
                "SELECT id FROM files WHERE content_hash=? AND id!=? AND status='indexed' LIMIT 1",
                (chash, rec.id)).fetchone()
            if dup:
                self.db.set_status(rec.id, "indexed", content_hash=chash,
                                   extractor=f"dup:{extractor_name}")
                self.counters["skipped"] += 1
                return
            for idx in self.indexers:
                if art.chunks or "text" not in idx.consumes:
                    idx.index(rec, art)
            self.db.put_chunks(rec.id, art.chunks)
            self.db.set_status(rec.id, "indexed", extractor=extractor_name,
                               content_hash=chash)
            self.counters["indexed"] += 1
            if self.progress_cb:
                self.progress_cb({"file": str(path), **self.counters})
        except Exception as e:
            log.error("ошибка индексации %s: %s", path, e)
            self.db.set_status(rec.id, "error", error=str(e)[:500])
            self.counters["errors"] += 1

    def _extract(self, rec: FileRecord, path: Path) -> tuple[Artifacts, str, str]:
        probe = probe_file(path, self.st.limits.head_probe_kb)
        if rec.size > self.st.limits.max_file_mb << 20:
            return Artifacts(searchable_bytes=False), "oversized", ""
        chosen = None
        for ext in self.extractors:
            sup = ext.supports(probe)
            if sup is Support.YES:
                chosen = ext
                break
            if sup is Support.FALLBACK and chosen is None:
                chosen = ext
        if chosen is None:
            return Artifacts(), "none", ""
        ctx = PipelineCtx(path, self.events, self.db)
        art = chosen.extract(rec, ctx)
        return art, chosen.name, content_hash(path)

    def _handle_delete(self, ev: JobEvent) -> None:
        if ev.file_id:
            self._remove_all(ev.file_id)
            self.db.delete_file(ev.file_id)

    def _remove_all(self, file_id: int) -> None:
        for idx in self.indexers:
            try:
                idx.remove(file_id)
            except Exception as e:
                log.warning("indexer %s remove(%d) failed: %s", idx.name, file_id, e)
