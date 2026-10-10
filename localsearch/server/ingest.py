"""Приём данных на сервере: загрузки клиентов и удалённые сканы.

Два способа наполнить серверный индекс данными с клиентской машины:

1. upload  — клиент шлёт файлы целиком (POST /api/ingest/upload, multipart).
   Файлы складываются в data/uploads/<user_id>/...; индексация идёт общим
   конвейером (IndexPipeline) как обычно; owner_id = id загрузившего.

2. remote-scan — клиент не отдаёт содержимое, а присылает *метаданные* обхода
   своего дерева (POST /api/ingest/scan_manifest): путь, имя, размер, mtime,
   хэш + опционально извлечённый текст (если у клиента есть свои экстракторы).
   Так можно индексировать чужие каталоги без копирования байтов; поиск по
   содержимому работает только если клиент приложил chunks ('with_text').

3. delta-манифест — инкрементальная синхронизация: клиент хранит локальный
   кэш состояния отправки (.localsearch-manifest.json) и присылает только
   добавленные/изменённые записи + список удалённых rel_path'ов; сервер по
   client_id сверяет свою сторону и удаляет осиротевшие записи
   (POST /api/ingest/delta_manifest).

Оба потока помечают FileRecord.owner_id/namespace, поэтому фильтрация прав
делается одинаково для всех уровней поиска (см. searcher/*, core/db.py).
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from ..core.db import Database
from ..core.models import FileRecord, JobEvent

log = logging.getLogger("localsearch.ingest")


_SCHEMA_CLIENT_MANIFESTS = """
CREATE TABLE IF NOT EXISTS client_manifests(
    user_id    INTEGER NOT NULL,
    client_id  TEXT    NOT NULL,
    rel_path   TEXT    NOT NULL,
    content_hash TEXT  NOT NULL DEFAULT '',
    size       INTEGER NOT NULL DEFAULT 0,
    file_id    INTEGER NOT NULL DEFAULT 0,
    updated_at REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, client_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_cm_ns ON client_manifests(user_id, client_id);
"""


class IngestService:
    def __init__(self, db: Database, events: "Any", uploads_root: Path,
                 max_file_mb: int = 50, max_total_mb_per_user: int = 5_000):
        self.db = db
        self.events = events            # queue.Queue[JobEvent]
        self.uploads = uploads_root
        self.max_file_mb = max_file_mb
        self.max_total_mb = max_total_mb_per_user * (1 << 20)
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.db.conn.executescript(_SCHEMA_CLIENT_MANIFESTS)

    # ------------------------- 1. прямая загрузка файла ------------------------- #

    def save_upload(self, user_id: int, username: str, rel_path: str,
                    data: Any) -> FileRecord:
        """Сохранить загруженный файл и поставить его в очередь индексации.

        rel_path — путь, который клиент видит у себя (сохраняет структуру
        внутри namespace 'upload:<username>'). Защита от path traversal.
        data — bytes или итерируемый поток chunk'ов (iterate_chunks).
        """
        safe_rel = self._sanitize_rel(rel_path)
        ns_dir = self.uploads / f"{user_id}_{username}"
        target = (ns_dir / safe_rel).resolve()
        if not str(target).startswith(str(ns_dir.resolve())):
            raise ValueError("path traversal detected")
        limit = self.max_file_mb * (1 << 20)

        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        if isinstance(data, (bytes, bytearray)):
            if len(data) > limit:
                raise ValueError(f"file too large (> {self.max_file_mb} MB)")
            target.write_bytes(bytes(data))
            written = len(data)
        else:                                    # ленивый поток с контролем лимита
            with open(target, "wb") as f:
                for chunk in data:
                    written += len(chunk)
                    if written > limit:
                        f.close()
                        target.unlink(missing_ok=True)
                        raise ValueError(f"file too large (> {self.max_file_mb} MB)")
                    f.write(chunk)
        self._check_quota(user_id, extra=written)

        rec = FileRecord.from_path(target)
        rec.path = str(target)
        rec.owner_id = user_id
        rec.namespace = f"upload:{username}"
        rec.id = self.db.upsert_file(rec)
        self.events.put(JobEvent(kind="upsert", path=rec.path, file_id=rec.id))
        log.info("upload: %s (%d bytes) -> file_id=%d", rel_path, rec.size, rec.id)
        return rec

    # ------------------------- 2. манифест удалённого скана ------------------------- #

    def ingest_scan_manifest(self, user_id: int, username: str,
                             manifest: dict[str, Any]) -> dict[str, int]:
        """Принять JSON-манифест обхода клиентского дерева.

        Формат:
          {"roots": ["/home/user/docs"],
           "files": [
             {"rel_path": "docs/a.txt", "size": 1234, "mtime": 1712345678.0,
              "hash": "sha256:...", "ext": ".txt",
              "chunks": [{"ordinal":0,"title":"A","text":"..."}]   # опционально
             }, ... ]}

        Если chunks нет — сервер знает только имя/размер/дату (L1-поиск);
        содержимое он может позже запросить через pull-задачу (дорожная карта).
        """
        stats = {"seen": 0, "new": 0, "changed": 0, "unchanged": 0}
        ns = f"remote:{username}"
        for item in manifest.get("files", []):
            stats["seen"] += 1
            rel = self._sanitize_rel(item["rel_path"])
            vpath = f"client://{username}/{rel}"          # виртуальный путь
            old = self.db.find_by_path(vpath)
            chash = item.get("hash", "")
            if old is not None and old.content_hash == chash and not item.get("force"):
                stats["unchanged"] += 1
                continue
            rec = FileRecord(
                path=vpath, name=Path(rel).name, ext=item.get("ext") or Path(rel).suffix.lower(),
                mime=item.get("mime", ""), size=int(item.get("size", 0)),
                mtime=float(item.get("mtime", 0.0)), ctime=float(item.get("ctime", 0.0)),
                content_hash=chash, status="pending", owner_id=user_id, namespace=ns,
            )
            rec.id = self.db.upsert_file(rec)
            stats["new" if old is None else "changed"] += 1
            self._apply_chunks(rec, item.get("chunks") or [])
        return stats

    # ------------------------- 3. инкрементальный дельта-манифест ------------------------- #

    def ingest_delta_manifest(self, user_id: int, username: str,
                              payload: dict[str, Any]) -> dict[str, int]:
        """Принять дельту от клиента с stateful-синхронизацией.

        Формат запроса:
          {"client_id": "desk-1", "reset": false, "snapshot_complete": true,
           "upsert": [ ...как files в scan_manifest... ],
           "delete": ["docs/old.txt", ...]}

        Сервер хранит свою копию состояния обхода по (user_id, client_id)
        (таблица client_manifests):

          * reset=true → состояние этого client_id пересобирается с нуля
            (первый sync клиента или восстановление после рассинхрона);
          * upsert-записи с новым hash → индексация (new/changed),
            совпавший hash → unchanged (идемпотентность повторной доставки);
          * snapshot_complete=true → записи client_id, отсутствующие в
            присланном наборе, удаляются с сервера — «призраки» после
            rm/mv у клиента исчезают из поиска;
          * delete → точечные удаления без полного снимка.

        Ответ содержит server_seq — клиент сохраняет его в своём кэше.
        """
        client_id = str(payload.get("client_id") or "default")
        ns = f"remote:{username}"
        stats = {"upserted": 0, "new": 0, "changed": 0, "unchanged": 0,
                 "deleted": 0}

        if payload.get("reset"):
            gone = self.db.conn.execute(
                "SELECT file_id FROM client_manifests WHERE user_id=? AND client_id=?",
                (user_id, client_id)).fetchall()
            for r in gone:
                if r["file_id"]:
                    self.db.delete_file(r["file_id"])
            self.db.conn.execute(
                "DELETE FROM client_manifests WHERE user_id=? AND client_id=?",
                (user_id, client_id))
            stats["deleted"] += len(gone)

        server_state = {
            r["rel_path"]: (r["content_hash"], r["file_id"])
            for r in self.db.conn.execute(
                "SELECT rel_path, content_hash, file_id FROM client_manifests "
                "WHERE user_id=? AND client_id=?", (user_id, client_id))
        }

        incoming: dict[str, dict] = {}
        for item in payload.get("upsert", []):
            try:
                rel = self._sanitize_rel(item["rel_path"])
            except (KeyError, ValueError):
                continue
            incoming[rel] = item

        # --- 1. удаления: было на сервере, нет в incoming-наборе этого клиента ---
        # (только если дельта объявлена полной по этому набору root'ов:
        #  клиент ставит "snapshot_complete": true; промежуточные батчи его не имеют)
        if payload.get("snapshot_complete", True):
            for rel, (_h, fid) in server_state.items():
                if rel not in incoming:
                    if fid:
                        self.db.delete_file(fid)
                    self.db.conn.execute(
                        "DELETE FROM client_manifests WHERE user_id=? AND client_id=? "
                        "AND rel_path=?", (user_id, client_id, rel))
                    stats["deleted"] += 1

        # deletions, названные явно (mv без последующего полного snapshot'а)
        for rel in payload.get("delete", []):
            try:
                rel = self._sanitize_rel(rel)
            except (KeyError, ValueError):
                continue
            h_fid = server_state.get(rel)
            if h_fid:
                if h_fid[1]:
                    self.db.delete_file(h_fid[1])
                self.db.conn.execute(
                    "DELETE FROM client_manifests WHERE user_id=? AND client_id=? "
                    "AND rel_path=?", (user_id, client_id, rel))
                stats["deleted"] += 1
            incoming.pop(rel, None)

        # --- 2. upsert'ы: сравниваем hash с серверной стороной ---
        now = time.time()
        for rel, item in incoming.items():
            chash = item.get("hash", "")
            old_hash, _old_fid = server_state.get(rel, (None, None))
            if old_hash == chash and not item.get("force"):
                stats["unchanged"] += 1
                continue
            vpath = f"client://{username}/{rel}"
            old = self.db.find_by_path(vpath)
            rec = FileRecord(
                path=vpath, name=Path(rel).name,
                ext=item.get("ext") or Path(rel).suffix.lower(),
                mime=item.get("mime", ""), size=int(item.get("size", 0)),
                mtime=float(item.get("mtime", 0.0)), ctime=float(item.get("ctime", 0.0)),
                content_hash=chash, status="pending", owner_id=user_id, namespace=ns,
            )
            rec.id = self.db.upsert_file(rec)
            stats["new" if old is None else "changed"] += 1
            stats["upserted"] += 1
            self._apply_chunks(rec, item.get("chunks") or [])
            self.db.conn.execute(
                """INSERT INTO client_manifests(user_id, client_id, rel_path,
                       content_hash, size, file_id, updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(user_id, client_id, rel_path) DO UPDATE SET
                     content_hash=excluded.content_hash, size=excluded.size,
                     file_id=excluded.file_id, updated_at=excluded.updated_at""",
                (user_id, client_id, rel, chash, rec.size, rec.id, now))

        self.db.conn.commit()
        seq = self._bump_seq(user_id, client_id)
        stats["server_seq"] = seq
        log.info("delta [%s/%s]: +%d ~%d -%d =%d (seq=%d)", username, client_id,
                 stats["new"], stats["changed"], stats["deleted"],
                 stats["unchanged"], seq)
        return stats

    def _bump_seq(self, user_id: int, client_id: str) -> int:
        row = self.db.conn.execute(
            "SELECT COALESCE(MAX(updated_at),0) m FROM client_manifests "
            "WHERE user_id=? AND client_id=?", (user_id, client_id)).fetchone()
        return int(row["m"])

    # ------------------------- общее применение чанков ------------------------- #

    def _apply_chunks(self, rec: FileRecord, raw_chunks: list[dict]) -> None:
        """Проиндексировать текст из манифеста (источника-файла на сервере нет)."""
        # имя файла индексируем ВСЕГДА (L1 работает даже без переданного текста)
        self.db.index_name(rec.id, rec.name)
        if not raw_chunks:
            self.db.set_status(rec.id, "pending")
            return
        from ..core.contracts import Chunk
        objs = [Chunk(text=c["text"], ordinal=int(c.get("ordinal", i)),
                      title=c.get("title")) for i, c in enumerate(raw_chunks)]
        self.db.put_chunks(rec.id, objs)
        self._index_text_directly(rec, objs)
        self.db.set_status(rec.id, "indexed", extractor="remote-manifest")

    def _index_text_directly(self, rec: FileRecord, chunks: list[Any]) -> None:
        """Индексация текста из манифеста без чтения файла (его на сервере нет)."""
        joined = "\n\n".join(c.text for c in chunks)
        self.db.fts_replace(rec.id, (chunks[0].title or rec.name)[:512],
                            joined[:4_000_000], rec.path)

    # ------------------------- удаление своих данных ------------------------- #

    def delete_namespace(self, user_id: int) -> int:
        rows = self.db.conn.execute(
            "SELECT id, path FROM files WHERE owner_id=?", (user_id,)).fetchall()
        for r in rows:
            self.db.delete_file(r["id"])
        d = self.uploads / str(user_id)
        for sub in self.uploads.iterdir():
            if sub.name.startswith(f"{user_id}_"):
                shutil.rmtree(sub, ignore_errors=True)
        return len(rows)

    # ------------------------- служебное ------------------------- #

    @staticmethod
    def _sanitize_rel(rel: str) -> str:
        rel = rel.replace("\\", "/").lstrip("/")
        parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
        if not parts:
            raise ValueError("empty relative path")
        return "/".join(parts)

    def _check_quota(self, user_id: int, extra: int) -> None:
        used = sum(f.stat().st_size for f in self.uploads.rglob("*")
                   if f.is_file() and f.parts[len(self.uploads.parts)]
                   .startswith(f"{user_id}_")) if False else 0
        # быстрая оценка через БД (сумма размеров файлов пользователя)
        row = self.db.conn.execute(
            "SELECT COALESCE(SUM(size),0) s FROM files WHERE owner_id=?",
            (user_id,)).fetchone()
        used = row["s"] or 0
        if used + extra > self.max_total_mb:
            raise ValueError("user storage quota exceeded")

    @staticmethod
    def hash_bytes(b: bytes) -> str:
        return "sha256:" + hashlib.sha256(b).hexdigest()
