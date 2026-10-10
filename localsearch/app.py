"""Композиция root-модулей (app.py / CLI) — точка сборки системы.

build_app(settings): Database → probe-экстракторы из registry → индексеры
(Manticore или sqlite_fts; name/byte/vectors всегда при наличии полей) →
серчеры в dict → Walker/Watcher/IndexPipeline. Всё зависит только от контрактов,
поэтому замена любого модуля конфигом не трогает остальное.
"""
from __future__ import annotations

import logging
import queue as q
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core.db import Database
from .core.models import JobEvent
from .core.registry import PluginsCfg, registry
from .indexer.byte_index import ByteIndexer
from .indexer.manticore import ManticoreClient
from .indexer.manticore_text import ManticoreTextIndexer
from .indexer.name_index import NameIndexer
from .indexer.sqlite_fts import SqliteFtsIndexer
from .searcher.bytes_seq import BytesSeqSearcher
from .searcher.filename import FilenameSearcher
from .searcher.phrases import PhrasesSearcher, SqliteFtsBackend
from .settings import Settings
from .workers.pipeline import IndexPipeline

log = logging.getLogger("localsearch.app")


@dataclass
class App:
    settings: Settings
    db: Database
    events: "q.Queue[JobEvent]"
    indexers: list[Any]
    searchers: dict[str, Any]
    pipeline: IndexPipeline
    manticore: ManticoreClient | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def build_app(settings: Settings, plugins_dir: Path | None = None) -> App:
    # 1. реестр плагинов: builtins + entry-points + пользовательские файлы
    registry.discover_builtins()
    registry.discover_entrypoints()
    if plugins_dir is None:
        plugins_dir = Path.home() / ".config" / "localsearch" / "plugins"
    registry.discover_dir(plugins_dir)
    pcfg = PluginsCfg.from_dict(settings.plugins)

    extractors = registry.all_of_kind("extractor", pcfg)

    # 2. хранилище метаданных
    db = Database(settings.db_path)

    # 3. текстовый бэкенд: Manticore с graceful-degradation на SQLite FTS5
    mc: ManticoreClient | None = None
    use_sqlite_fts = settings.indexer_backend == "sqlite_fts"
    if not use_sqlite_fts:
        mc = ManticoreClient(settings.manticore.host, settings.manticore.port,
                             settings.manticore.index)
        if not (mc.connect() and mc.ping()):
            log.warning("Manticore недоступен — деградация на SQLite FTS5")
            mc, use_sqlite_fts = None, True

    indexers: list[Any] = [NameIndexer(db), ByteIndexer(db, settings.limits.byte_index_max_mb)]
    if use_sqlite_fts:
        indexers.append(SqliteFtsIndexer(db))
        backend: Any = SqliteFtsBackend(db)
    else:
        assert mc is not None
        mt = ManticoreTextIndexer(mc)
        mt.setup()
        indexers.append(mt)
        backend = mc

    vector_indexer = None
    if settings.vector.enabled:
        try:
            from .indexer.vector_store import VectorStoreIndexer
            vector_indexer = VectorStoreIndexer(settings.vector, db)
            vector_indexer.setup()
            indexers.append(vector_indexer)
            # включаем AI-экстрактор картинок (флаг читает image_ai._available())
            ai = registry.get("extractor", "image_ai")
            if ai is not None:
                type(ai)._enabled = True
        except ImportError as e:
            log.warning("L4 недоступен: %s", e)

    # 4. серчеры
    searchers: dict[str, Any] = {
        "filename": FilenameSearcher(db),
        "phrases": PhrasesSearcher(backend),
        "bytes_seq": BytesSeqSearcher(db),
    }
    if settings.vector.enabled and vector_indexer is not None:
        from .searcher.semantic import SemanticSearcher
        searchers["semantic"] = SemanticSearcher(vector_indexer, db)

    # 5. конвейер
    events: "q.Queue[JobEvent]" = q.Queue(maxsize=settings.workers.queue_size)
    pipeline = IndexPipeline(settings, db, events, extractors, indexers)

    return App(settings=settings, db=db, events=events, indexers=indexers,
               searchers=searchers, pipeline=pipeline, manticore=mc,
               extras={"vector_indexer": vector_indexer})
