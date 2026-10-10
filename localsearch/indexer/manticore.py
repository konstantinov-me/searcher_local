"""Manticore Search — слой подключения и DDL.

Manticore 6.x понимает SQL через MySQL-протокол (порт 9306). Индекс `docs`
создаётся приложением при первом коннекте (CREATE TABLE синтаксис RT-индекса):

  * title/body — text с морфологией en,ru (Snowball) → L2: слова, фразы "a b c",
    операторы AND/OR/NOT, BM25-ранжирование;
  * ignore_chars + infix_step => встроенный НЕЧЁТКИЙ поиск:
      - `SELECT ... WHERE MATCH('@@(програмиств)')` — burst edit distance;
      - `infix('подключен')` — not-analyzed substring-поиск по всему тексту;
    т.е. весь fuzzy текстового слоя тоже на Manticore (решение из обсуждения);
  * атрибуты file_id/size/mtime — фильтрация без парсинга документов.

Клиент — pymysql (чистый Python, нет нативных зависимостей mysqlclient).
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("localsearch.manticore")

DDL_DOCS = """
CREATE TABLE IF NOT EXISTS docs (
    id          bigint,
    file_id     integer,
    chunk       integer,
    title       text,
    body        text,
    path        string,
    ext         string,
    mime        string,
    size        bigint,
    mtime       timestamp,
    owner       integer,                      -- клиент-сервер: id загрузившего пользователя
    @index(title,body) morphology('en,ru') stopwords('stopwords.txt')
              expand_keywords='1' min_infix_len='3' max_edit_distance='2',
    @index(path,ext,mime) expand_keywords='0'
)
"""

DDL_STOPWORDS = ""   # файл stopwords.txt кладётся в том контейнера (см. deploy/)


class ManticoreClient:
    def __init__(self, host: str, port: int, index: str = "docs"):
        self.host, self.port, self.index = host, port, index
        self._conn: Any = None

    # ------------------------- подключение ------------------------- #

    def connect(self) -> bool:
        try:
            import pymysql
        except ImportError:
            log.error("pymysql не установлен (pip install localsearch[manticore])")
            return False
        try:
            self._conn = pymysql.connect(
                host=self.host, port=self.port, autocommit=True,
                charset="utf8mb4", connect_timeout=5, read_timeout=60)
            log.info("manticore: подключено %s:%d", self.host, self.port)
            return True
        except Exception as e:
            log.warning("manticore недоступен (%s) — нужен docker compose up", e)
            return False

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def ensure_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(f"SHOW TABLES LIKE '{self.index}'")
        if cur.fetchone() is None:
            log.info("manticore: создаю индекс %s", self.index)
            cur.execute(DDL_DOCS)

    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        cur = self._conn.cursor()
        cur.execute(sql, params)
        try:
            return cur.fetchall()
        except Exception:
            return []

    # ------------------------- запись (REPLACE = атомарное обновление) ------------------------- #

    def replace_doc(self, doc_id: int, file_id: int, chunk: int, title: str,
                    body: str, path: str, ext: str, mime: str,
                    size: int, mtime: float, owner: int = 0) -> None:
        # pymysql paramstyle='format' (%s); Manticore принимает стандартный SQL
        self.execute(
            f"REPLACE INTO {self.index} "
            "(id,file_id,chunk,title,body,path,ext,mime,size,mtime,owner) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (doc_id, file_id, chunk, title, body[:256_000], path, ext, mime, size,
             int(mtime), owner))

    def delete_by_file(self, file_id: int) -> None:
        self.execute(f"DELETE FROM {self.index} WHERE file_id=%s", (file_id,))

    # ------------------------- запросы L2 ------------------------- #

    @staticmethod
    def escape(q: str) -> str:
        for ch in "\\()*|&!<>-~\"":
            q = q.replace(ch, f"\\{ch}")
        return q

    def search(self, query: str, mode: str = "fulltext", limit: int = 50,
                filters_sql: str = "") -> list[dict[str, Any]]:
        """mode: 'fulltext'|'phrase'|'fuzzy'|'infix'. Возвращает rows c weight и snippet()."""
        idx = self.index
        if mode == "phrase":
            match = f'"{query}"'
        elif mode == "fuzzy":
            match = f"@@({self.escape(query)})"          # burst edit-distance
        elif mode == "infix":
            expr = f"infix('{self.escape(query)}')"
            sql = (f"SELECT id, file_id, chunk, path, weight()"
                   f" FROM {idx} WHERE {expr} {filters_sql}"
                   f" ORDER BY weight() DESC LIMIT {limit}")
            return self._rows_to_dicts(sql)
        else:
            match = self.escape(query)
        sql = (f"SELECT id, file_id, chunk, path, weight(),"
               f" snippet({idx}, 'body', '<b>', '</b>', '…', 5, 100) AS snip,"
               f" matched_tokens() AS tokens"
               f" FROM {idx} WHERE MATCH(%s) {filters_sql}"
               f" ORDER BY weight() DESC LIMIT {limit}")
        cur = self._conn.cursor()
        cur.execute(sql, (match,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def _rows_to_dicts(self, sql: str) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def ping(self) -> bool:
        try:
            self.execute("SHOW STATUS LIKE 'uptime'")
            return True
        except Exception:
            return False
