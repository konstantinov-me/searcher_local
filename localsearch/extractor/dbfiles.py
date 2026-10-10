"""Экстрактор файлов баз данных: SQLite (.db/.sqlite), DBF, (опц.) MS Access .mdb.

Стратегия «табличный дамп в текст»:
  * схема (имена таблиц/колонок) — отдельный чанк (ищем 'CREATE TABLE orders');
  * строки → "table.col = value" построчно с лимитом строк на таблицу;
  * только read-only URI (`mode=ro`) — ядро никогда не пишет в чужие БД;
  * redact-список колонок (password, token...) исключает чувствительные данные.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterator

from ..core.contracts import Artifacts, Chunk, ExtractCtx, FileProbe, Support
from ..core.registry import registry
from .base import BaseExtractor, chunk_text

REDACT = {"password", "passwd", "secret", "token", "api_key", "credit_card"}
MAX_ROWS_PER_TABLE = 20_000
MAX_CELL_CHARS = 512


class SqliteExtractor(BaseExtractor):
    name = "dbfiles"
    priority = 45
    formats = ("application/x-sqlite3", "application/x-dbf", "application/x-msaccess")

    def supports(self, probe: FileProbe) -> Support:
        if probe.magic.startswith("53514c697465"):          # 'SQLite format 3'
            return Support.YES
        if probe.ext in (".sqlite", ".db", ".sqlite3", ".dbf"):
            return Support.YES
        return Support.NO

    def extract(self, rec: Any, ctx: ExtractCtx) -> Artifacts:
        ext = (rec.ext if rec else "") or ""
        if ext == ".dbf":
            return self._extract_dbf(ctx)
        return self._extract_sqlite(ctx)

    # ------------------------- SQLite ------------------------- #

    def _extract_sqlite(self, ctx: ExtractCtx) -> Artifacts:
        path = str(getattr(ctx, "path", ""))
        uri = f"file:{path}?mode=ro"
        art = Artifacts(metadata={"format": "sqlite"})
        try:
            con = sqlite3.connect(uri, uri=True, timeout=5)
        except sqlite3.Error as e:
            ctx.log(f"sqlite open failed: {e}")
            return art
        try:
            cur = con.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            tables = cur.fetchall()
            schema_txt = "\n".join(sql or f"TABLE {name}" for name, sql in tables)
            art.chunks.append(Chunk(text=f"-- схема\n{schema_txt}", ordinal=0,
                                    title="schema"))
            lines_per_table: dict[str, int] = {}
            body_parts: list[str] = []
            ordinal = 1
            for name, _sql in tables:
                cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
                redacted = {c for c in cols if c.lower() in REDACT}
                for i, row in enumerate(self._rows(con, name)):
                    cells = [f"{name}.{c} = {self._fmt(v)}"
                             for c, v in zip(cols, row) if c not in redacted and v is not None]
                    if cells:
                        body_parts.append("; ".join(cells))
                    if len(body_parts) >= 5_000:
                        art.chunks.extend(
                            Chunk(text=t, ordinal=ordinal + j)
                            for j, t in enumerate(chunk_text("\n".join(body_parts))))
                        ordinal += 10
                        body_parts = []
                    if i >= MAX_ROWS_PER_TABLE:
                        break
                lines_per_table[name] = min(i + 1, MAX_ROWS_PER_TABLE) if tables else 0
            if body_parts:
                art.chunks.extend(Chunk(text=t, ordinal=ordinal + j)
                                  for j, t in enumerate(chunk_text("\n".join(body_parts))))
            art.metadata["tables"] = lines_per_table
            art.searchable_bytes = True
        finally:
            con.close()
        return art

    @staticmethod
    def _rows(con: sqlite3.Connection, table: str) -> Iterator[tuple]:
        try:
            yield from con.execute(f'SELECT * FROM "{table}" LIMIT ?', (MAX_ROWS_PER_TABLE,))
        except sqlite3.Error:
            return

    @staticmethod
    def _fmt(v: Any) -> str:
        s = str(v)
        return s[:MAX_CELL_CHARS] + "…" if len(s) > MAX_CELL_CHARS else s

    # ------------------------- DBF ------------------------- #

    @staticmethod
    def _extract_dbf(ctx: ExtractCtx) -> Artifacts:
        """DBF через dbfread если установлен; иначе — пустой артефакт."""
        try:
            from dbfread import DBF
            table = DBF(str(getattr(ctx, "path", "")))
            parts = []
            for i, r in enumerate(table):
                if i > MAX_ROWS_PER_TABLE:
                    break
                parts.append("; ".join(f"{table.table_name}.{k} = {v}"
                                       for k, v in r.items() if v))
            return Artifacts(chunks=chunk_text("\n".join(parts)), metadata={"format": "dbf"})
        except ImportError as e:
            ctx.log(f"dbf: зависимость не установлена ({e})")
            return Artifacts(metadata={"format": "dbf", "skipped": str(e)})


registry.register("extractor", SqliteExtractor(), "builtin")
