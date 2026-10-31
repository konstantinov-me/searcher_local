"""SQLite — источник истины о файлах + резервные индексы (имена, байт-trigram'ы, FTS5).

Схема:
  files       — реестр файлов и статусы обработки
  chunks      — извлечённый текст (нужен для L4-чанков, сниппетов и fallback-FTS5)
  docs_fts    — FTS5-таблица (alternate-индексер без Manticore)
  names_trgm  — триграммы имён файлов (L1)
  bytegrams   — триграммы байтов содержимого (L3)
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator

from .models import FileRecord

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    ext TEXT, mime TEXT,
    size INTEGER, mtime REAL, ctime REAL,
    content_hash TEXT,
    status TEXT DEFAULT 'pending',          -- pending|extracting|indexed|error|skipped
    error TEXT, extractor TEXT,
    parent_id INTEGER, member_name TEXT,
    owner_id INTEGER DEFAULT 0,             -- клиент-сервер: загрузивший пользователь
    namespace TEXT DEFAULT 'local',         -- local | upload:<user> | remote:<user>
    metadata TEXT                            -- JSON: EXIF, ID3, схема БД...
);
CREATE INDEX IF NOT EXISTS ix_files_status ON files(status);
CREATE INDEX IF NOT EXISTS ix_files_hash  ON files(content_hash);
CREATE INDEX IF NOT EXISTS ix_files_name  ON files(name);
CREATE INDEX IF NOT EXISTS ix_files_owner ON files(owner_id, namespace);

CREATE TABLE IF NOT EXISTS chunks(
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    title TEXT, body TEXT NOT NULL, meta TEXT,
    PRIMARY KEY(file_id, ordinal)
);

CREATE TABLE IF NOT EXISTS names_trgm(
    file_id INTEGER NOT NULL,               -- id файла или -id (виртуальный id для пути)
    trigram TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_trgm ON names_trgm(trigram);

CREATE TABLE IF NOT EXISTS bytegrams(
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    gram BLOB NOT NULL                       -- 3 байта содержимого
);
CREATE INDEX IF NOT EXISTS ix_bgram ON bytegrams(gram);

CREATE TABLE IF NOT EXISTS jobs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT, path TEXT, file_id INTEGER, ts REAL, done INTEGER DEFAULT 0
);
"""

# FTS5-токенизатор подбирается под возможности конкретной сборки SQLite:
#   unicode_ru trigram — морфология RU+EN (Snowball) + поддержка LIKE/подстрок;
#   porter             — если unicode_ru собран без Snowball;
#   trigram            — самый деградированный вариант (без stemming).
_fts_cache: dict[str, str] = {}


def fts_tokenizer(conn: sqlite3.Connection) -> str:
    """Определить рабочий tokenize для docs_fts в этом окружении."""
    key = id(conn)
    if key not in _fts_cache:
        for tok in ("unicode_ru trigram", "porter", "trigram"):
            try:
                conn.execute(f"CREATE VIRTUAL TABLE _tok_probe USING fts5(a, tokenize='{tok}')")
                conn.execute("DROP TABLE _tok_probe")
                _fts_cache[key] = tok
                break
            except sqlite3.OperationalError:
                continue
        else:
            _fts_cache[key] = "unicode61"
    return _fts_cache[key]


def create_docs_fts(conn: sqlite3.Connection) -> None:
    """Создать docs_fts с подобранным токенизатором (idempotent)."""
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            title, body, path UNINDEXED, file_id UNINDEXED,
            tokenize='%s'
        )""" % fts_tokenizer(conn))


def trigrams(s: str) -> set[str]:
    """Триграммы строки в нижнем регистре (для not-analyzed-подобного fuzzy L1)."""
    s = s.lower()
    return {s[i:i + 3] for i in range(len(s) - 2)} if len(s) >= 3 else {s} if s else set()


def byte_trigrams(b: bytes) -> set[bytes]:
    return {b[i:i + 3] for i in range(len(b) - 2)}


class Database:
    """Тонкая потокобезопасная обёртка над sqlite3 (check_same_thread=False + lock)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self._lock, self.conn:
            self.conn.executescript(_SCHEMA)
            create_docs_fts(self.conn)      # tokenize подбирается под сборку SQLite
            self._migrate()
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _migrate(self) -> None:
        """Инкрементальные миграции для существующих БД (v1 → v2: owner_id/namespace)."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(files)")}
        if "owner_id" not in cols:
            self.conn.execute("ALTER TABLE files ADD COLUMN owner_id INTEGER DEFAULT 0")
        if "namespace" not in cols:
            self.conn.execute("ALTER TABLE files ADD COLUMN namespace TEXT DEFAULT 'local'")
        # names_trgm хранит и «виртуальные» id (-file_id) для путей, поэтому FK
        # недопустим; пересоздаём таблицу из старых схем с REFERENCES.
        fk = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='names_trgm'"
        ).fetchone()
        if fk and "REFERENCES" in (fk[0] or ""):
            with self.conn:
                self.conn.executescript(
                    """BEGIN;
                       ALTER TABLE names_trgm RENAME TO names_trgm_old;
                       CREATE TABLE names_trgm(
                           file_id INTEGER NOT NULL, trigram TEXT NOT NULL);
                       INSERT INTO names_trgm SELECT file_id, trigram FROM names_trgm_old;
                       DROP TABLE names_trgm_old;
                       CREATE INDEX IF NOT EXISTS ix_trgm ON names_trgm(trigram);
                       COMMIT;""")

    # ------------------------- CRUD файлов ------------------------- #

    def upsert_file(self, rec: FileRecord) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO files(path,name,ext,mime,size,mtime,ctime,content_hash,
                                       status,error,extractor,parent_id,member_name,
                                       owner_id,namespace,metadata)
                   VALUES(:path,:name,:ext,:mime,:size,:mtime,:ctime,:content_hash,
                          :status,:error,:extractor,:parent_id,:member_name,
                          :owner_id,:namespace,:metadata)
                   ON CONFLICT(path) DO UPDATE SET
                     size=:size, mtime=:mtime, content_hash=:content_hash,
                     status=:status, error=:error, extractor=:extractor,
                     mime=:mime, metadata=:metadata,
                     owner_id=:owner_id, namespace=:namespace
                   RETURNING id""",
                {**dataclasses.asdict(rec), "metadata": json.dumps(rec.metadata, ensure_ascii=False)},
            )
            return cur.fetchone()[0]

    def get_file(self, file_id: int) -> FileRecord | None:
        row = self.conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return self._row_to_rec(row) if row else None

    def find_by_path(self, path: str) -> FileRecord | None:
        row = self.conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        return self._row_to_rec(row) if row else None

    def set_status(self, file_id: int, status: str, error: str | None = None,
                   extractor: str | None = None, content_hash: str | None = None) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE files SET status=?, error=COALESCE(?,error),
                       extractor=COALESCE(?,extractor), content_hash=COALESCE(?,content_hash)
                   WHERE id=?""",
                (status, error, extractor, content_hash, file_id))

    def delete_file(self, file_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM files WHERE id=?", (file_id,))
            self.conn.execute("DELETE FROM docs_fts WHERE file_id=?", (str(file_id),))

    def iter_pending(self, limit: int = 1000) -> Iterator[FileRecord]:
        rows = self.conn.execute(
            "SELECT * FROM files WHERE status IN ('pending','error') ORDER BY id LIMIT ?",
            (limit,)).fetchall()
        yield from map(self._row_to_rec, rows)

    def stats(self, owner_id: int | None = None) -> dict[str, int]:
        """owner_id=None — общая статистика; иначе — только файлы пользователя."""
        if owner_id is None:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) c FROM files GROUP BY status").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) c FROM files WHERE owner_id=? GROUP BY status",
                (owner_id,)).fetchall()
        return {r["status"]: r["c"] for r in rows}

    def allowed_file_ids(self, owner_id: int | None) -> set[int] | None:
        """Множество разрешённых file_id для post-filter'а (None => без ограничений).

        Для больших БД используется как лёгкий guard после того, как индексы
        уже вернули кандидатов; exactness важен для L1/L3 (индексы-кандидаты).
        """
        if owner_id is None:
            return None
        rows = self.conn.execute("SELECT id FROM files WHERE owner_id=?",
                                 (owner_id,)).fetchall()
        return {r[0] for r in rows}

    @staticmethod
    def _row_to_rec(row: sqlite3.Row) -> FileRecord:
        d = dict(row)
        d["metadata"] = json.loads(d.get("metadata") or "{}")
        return FileRecord(**{k: d.get(k) for k in FileRecord.__slots__})

    # ------------------------- Чанки текста ------------------------- #

    def put_chunks(self, file_id: int, chunks: list[Any]) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
            self.conn.executemany(
                "INSERT INTO chunks(file_id,ordinal,title,body,meta) VALUES(?,?,?,?,?)",
                [(file_id, c.ordinal, c.title, c.text,
                  json.dumps(c.meta, ensure_ascii=False)) for c in chunks])

    # ------------------------- L1: имена ------------------------- #

    def index_name(self, file_id: int, name: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM names_trgm WHERE file_id=?", (file_id,))
            self.conn.executemany(
                "INSERT INTO names_trgm(file_id,trigram) VALUES(?,?)",
                [(file_id, t) for t in trigrams(name)])

    def search_names_candidates(self, query: str, limit: int,
                                owner_id: int | None = None) -> list[int]:
        """Кандидаты по пересечению триграмм; точная проверка — на стороне серчера.

        owner_id (клиент-сервер): JOIN с files => в выдачу попадают только
        файлы указанного пользователя (None => без фильтра).
        """
        ts = trigrams(query)
        if not ts:
            return []
        ph = ",".join("?" * len(ts))
        owner_sql = "AND f.owner_id=?" if owner_id is not None else ""
        params: list[Any] = [*ts]
        if owner_id is not None:
            params.append(owner_id)
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT nt.file_id, COUNT(DISTINCT nt.trigram) c FROM names_trgm nt
                JOIN files f ON f.id=nt.file_id
                WHERE nt.trigram IN ({ph}) {owner_sql}
                GROUP BY nt.file_id ORDER BY c DESC LIMIT ?""",
            params).fetchall()
        return [r["file_id"] for r in rows]

    # ------------------------- L3: байтовые триграммы ------------------------- #

    def index_bytes(self, file_id: int, head: bytes, max_grams: int = 200_000) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM bytegrams WHERE file_id=?", (file_id,))
            grams = list(byte_trigrams(head))[:max_grams]
            self.conn.executemany("INSERT INTO bytegrams(file_id,gram) VALUES(?,?)",
                                  [(file_id, g) for g in grams])

    def byte_candidates(self, needle: bytes, limit: int,
                        owner_id: int | None = None) -> list[int]:
        if len(needle) < 3:
            return []
        grams = list(byte_trigrams(needle[:30]))     # первые 30 байт запроса достаточно
        ph = ",".join("?" * len(grams))
        owner_sql = "AND f.owner_id=?" if owner_id is not None else ""
        params: list[Any] = [*grams]
        if owner_id is not None:
            params.append(owner_id)
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT bg.file_id, COUNT(*) c FROM bytegrams bg
                JOIN files f ON f.id=bg.file_id
                WHERE bg.gram IN ({ph}) {owner_sql}
                GROUP BY bg.file_id ORDER BY c DESC LIMIT ?""",
            params).fetchall()
        return [r["file_id"] for r in rows]

    # ------------------------- Fallback FTS5 (alternate Manticore) ------------------------- #

    def fts_replace(self, file_id: int, title: str, body: str, path: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM docs_fts WHERE file_id=?", (str(file_id),))
            self.conn.execute(
                "INSERT INTO docs_fts(title,body,path,file_id) VALUES(?,?,?,?)",
                (title, body, path, str(file_id)))

    def fts_search(self, match: str, limit: int,
                   owner_id: int | None = None) -> list[dict[str, Any]]:
        """FTS5-поиск с опциональным фильтром по владельцу (клиент-сервер)."""
        owner_sql = "JOIN files f ON f.id=CAST(docs_fts.file_id AS INTEGER) AND f.owner_id=?" \
            if owner_id is not None else ""
        sql = (f"SELECT docs_fts.file_id, title,"
               f" snippet(docs_fts,1,'<b>','</b>','…',8) AS snip,"
               f" bm25(docs_fts) AS weight FROM docs_fts {owner_sql}"
               f" WHERE docs_fts MATCH ? LIMIT ?")
        params: list[Any] = [owner_id] if owner_id is not None else []
        params += [match, limit]
        out = []
        for row in self.conn.execute(sql, params).fetchall():
            d = dict(row)
            d["weight"] = -d["weight"]      # bm25 в FTS5 отрицательный: меньше = лучше
            out.append(d)
        return out
