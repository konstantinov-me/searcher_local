"""Контракты (Protocol'ы) всех модулей системы.

Вся модульность держится на этих интерфейсах:
  * Extractor  — превращает файл в «сырьё» (Artifacts); расширяется плагинами.
  * Indexer    — кладёт сырьё в конкретное хранилище.
  * Searcher   — ищет в своём хранилище, возвращает нормализованные Hit'ы.
  * Scanner    — обход дерева директорий / наблюдатель.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, Protocol, runtime_checkable


# --------------------------------------------------------------------------- #
# Вспомогательные типы
# --------------------------------------------------------------------------- #

class Support(str, Enum):
    """Ответ экстрактора на вопрос «могу ли я обработать этот файл?»."""
    YES = "yes"        # уверенно берём (например, расширение + magic совпали)
    FALLBACK = "fallback"  # можем, но уступим более специализированным
    NO = "no"


@dataclass(slots=True)
class FileProbe:
    """Результат «прощупывания» файла для маршрутизации по экстракторам."""
    path: Path
    ext: str                    # нижний регистр, с точкой: '.pdf'
    mime: str                   # 'application/pdf' | 'text/plain' | ...
    magic: str                  # сигнатура hexdump первых байт: '255044462...'
    head: bytes                 # первые ~8 КБ содержимого (для детекции)
    size: int


@dataclass(slots=True)
class Chunk:
    """Кусок извлечённого текста (чанкуются большие документы и статьи словарей)."""
    text: str
    ordinal: int = 0            # номер чанка внутри файла
    title: str | None = None    # заголовок секции/статьи (для lexicon, md, docx)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Artifacts:
    """«Сырьё», которое экстрактор отдаёт индексерам. Все поля опциональны."""
    chunks: list[Chunk] = field(default_factory=list)          # -> manticore_text, vector_store
    metadata: dict[str, Any] = field(default_factory=dict)     # -> sqlite (EXIF, ID3, схема БД...)
    vectors: list[tuple[list[float], str]] = field(default_factory=list)
    # (embedding, modality:'text'|'image') -> vector_store
    thumbnail: bytes | None = None                             # -> sqlite blob (превью картинок)
    searchable_bytes: bool = True                              # false => не строим byte-trigrams
    extra: dict[str, Any] = field(default_factory=dict)        # произвольные данные своих индексеров


@dataclass(slots=True)
class Hit:
    """Нормализованный результат поиска от любого серчера."""
    file_id: int
    score: float                # чем больше, тем лучше (серчер сам нормализует)
    source: str                 # id серчера: 'filename' | 'phrases' | 'bytes' | 'semantic'
    snippet: str = ""           # контекст вокруг совпадения
    span: tuple[int, int] | None = None   # позиция совпадения в тексте/байтах
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class QuerySpec:
    """Единый формат запроса для всех серчеров."""
    raw: str                            # исходная строка пользователя
    level: str = "auto"                 # auto|name|phrase|bytes|semantic
    filters: dict[str, Any] = field(default_factory=dict)
    # примеры фильтров: {'ext': ['.pdf'], 'path_prefix': '/home/u/docs',
    #                    'mtime_after': 1700000000, 'size_max': 50_000_000}
    limit: int = 50


# --------------------------------------------------------------------------- #
# Контекст извлечения
# --------------------------------------------------------------------------- #

@runtime_checkable
class ExtractCtx(Protocol):
    """Передаётся экстракторам: даёт ленивый доступ к содержимому файла
    и позволяет ставить в очередь вложенные файлы (для archive-экстрактора)."""

    def open(self) -> BinaryIO: ...
    def read_bytes(self, limit: int | None = None) -> bytes: ...
    def read_text(self, max_chars: int) -> str: ...
    def enqueue_child(self, tmp_path: Path, parent_id: int, member_name: str) -> None: ...
    def log(self, msg: str) -> None: ...


# --------------------------------------------------------------------------- #
# Основные контракты модулей
# --------------------------------------------------------------------------- #

@runtime_checkable
class PluginBase(Protocol):
    name: str                   # уникальный id: 'pdf', 'office', 'lexicon'...
    priority: int               # меньше = проверяется раньше; fallback всегда последний


@runtime_checkable
class Extractor(PluginBase, Protocol):
    """Плагин обработки конкретного формата файлов."""
    formats: tuple[str, ...]    # поддерживаемые mime/расширения — для диагностики и UI

    def supports(self, probe: FileProbe) -> Support: ...
    def extract(self, rec: Any, ctx: ExtractCtx) -> Artifacts: ...


class Indexer(Protocol):
    """Модуль записи артефактов в конкретное хранилище (структурный тип;
    runtime_checkable для Protocol с не-методовыми атрибутами недоступен)."""
    name: str                   # уникальный id: 'manticore_text' | 'sqlite_fts'...
    target: str                 # 'manticore' | 'sqlite' | 'faiss' | 'byteindex'
    consumes: set[str]          # нужные поля Artifacts: {'text'} / {'metadata'} / {'vectors'}

    def setup(self) -> None: ...                       # создать таблицы/индексы при старте
    def index(self, rec: Any, art: Artifacts) -> None: ...
    def remove(self, file_id: int) -> None: ...


class Searcher(Protocol):
    """Модуль поиска в своём хранилище."""
    name: str
    level: int                  # 1..4 — «сложность» уровня (см. ARCHITECTURE.md §4)

    def search(self, q: QuerySpec) -> Iterator[Hit]: ...


@runtime_checkable
class Scanner(Protocol):
    def scan(self, roots: list[Path]) -> Iterator[Any]: ...
