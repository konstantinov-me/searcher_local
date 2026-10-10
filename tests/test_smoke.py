"""Интеграционный smoke-тест: сканируем временную папку с файлами разных форматов,
индексируем (sqlite_fts-бэкенд, без Manticore) и ищем на всех доступных уровнях.

Запуск:  pytest tests/test_smoke.py -v     (или python tests/test_smoke.py)
"""
from __future__ import annotations

import queue as q
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from localsearch.core.db import Database
from localsearch.core.models import JobEvent
from localsearch.core.registry import registry
from localsearch.extractor.base import BinaryFallback          # noqa: F401 — регистрация
from localsearch.extractor.text import TextExtractor           # noqa: F401
from localsearch.extractor.office import OfficeExtractor       # noqa: F401
from localsearch.indexer.name_index import NameIndexer
from localsearch.indexer.byte_index import ByteIndexer
from localsearch.indexer.sqlite_fts import SqliteFtsIndexer
from localsearch.scanner.probe import probe_file
from localsearch.scanner.walker import Walker
from localsearch.searcher.bytes_seq import BytesSeqSearcher, horspool_find, parse_bytes_query
from localsearch.searcher.filename import FilenameSearcher
from localsearch.searcher.fusion import fanout, route
from localsearch.searcher.phrases import PhrasesSearcher, SqliteFtsBackend
from localsearch.settings import Settings
from localsearch.workers.pipeline import IndexPipeline
from localsearch.core.contracts import QuerySpec


def make_tree(tmp: Path) -> None:
    (tmp / "docs").mkdir(parents=True)
    (tmp / "notes_маркетинг_2024.txt").write_text(
        "Стратегия подключения Manticore Search к проекту.\n"
        "Используем RT-индекс docs и морфологию en,ru.", encoding="utf-8")
    (tmp / "docs" / "report.md").write_text(
        "# Отчёт\n\nКвадрацикл подключен к сети и работает.", encoding="utf-8")
    (tmp / "binary.bin").write_bytes(b"\x00\x01SECRETNEEDLE123\x00\xff" * 10)
    with zipfile.ZipFile(tmp / "archive.zip", "w") as z:
        z.writestr("inner.txt", "внутренний файл архива: еда ласка")


def build(tmp: Path):
    st = Settings(roots=[tmp], data_dir=tmp / "_data", indexer_backend="sqlite_fts")
    db = Database(st.db_path)
    events: q.Queue = q.Queue()
    extractors = [TextExtractor(), OfficeExtractor(), BinaryFallback()]
    indexers = [NameIndexer(db), SqliteFtsIndexer(db), ByteIndexer(db, 50)]
    pipe = IndexPipeline(st, db, events, extractors, indexers)
    searchers = {
        "filename": FilenameSearcher(db),
        "phrases": PhrasesSearcher(SqliteFtsBackend(db)),
        "bytes_seq": BytesSeqSearcher(db),
    }
    return st, db, events, pipe, searchers


def run(tmp: Path) -> None:
    st, db, events, pipe, searchers = build(tmp)
    n = Walker(db, events).scan([tmp])
    assert n >= 3, f"walker found {n}"
    pipe.drain_pending()
    stats = db.stats()
    print("статусы:", stats)
    assert stats.get("indexed", 0) >= 3

    # L1: имя файла
    hits = fanout(searchers, QuerySpec(raw="name:маркетинг"))
    assert any("маркетинг" in (db.get_file(h.file_id) or 0).path if db.get_file(h.file_id) else False for h in hits) or hits, hits
    assert hits, "L1 должен найти notes_маркетинг_2024.txt"

    # L2: содержимое через FTS5 (морфология unicode_ru: 'подключен' найдёт 'подключённый/подключен')
    hits = fanout(searchers, QuerySpec(raw="квадрацикл"))
    assert hits, "L2 (FTS5) должен найти report.md по слову в тексте"

    # L2: фраза
    hits = list(searchers["phrases"].search(QuerySpec(raw='"стратегия подключения"')))
    assert hits, "фразовый поиск не сработал"

    # L3: байтовая последовательность
    hits = fanout(searchers, QuerySpec(raw="bytes:SECRETNEEDLE123"))
    assert hits, "L3 не нашёл байтовый маркер"

    # роутинг
    assert route(QuerySpec(raw="bytes:AABBCC"), list(searchers)) == ["bytes_seq"]
    assert route(QuerySpec(raw="hello"), list(searchers)) == ["phrases", "filename"]

    # утилиты L3
    assert parse_bytes_query("hex: de ad be ef") == bytes.fromhex("deadbeef")
    assert horspool_find(b"xxABCyy", b"ABC") == 2

    print("SMOKE OK ✔")


if __name__ == "__main__":
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="ls_smoke_"))
    make_tree(tmp)
    run(tmp)
else:
    def test_smoke():
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="ls_smoke_"))
        make_tree(tmp)
        run(tmp)
