"""Базовые утилиты для экстракторов + fallback-экстрактор бинарных файлов."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from ..core.contracts import Artifacts, Chunk, ExtractCtx, FileProbe, Support
from ..core.registry import registry


def content_hash(path: Path, algo: str = "blake3_or_sha256") -> str:
    """Хэш содержимого: blake3 если установлен, иначе sha256 (потоково)."""
    try:
        import blake3
        h = blake3.blake3()
    except ImportError:
        h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


_WS = re.compile(r"[ \t\f\v]+")
_NL = re.compile(r"\n{3,}")


def clean_text(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _WS.sub(" ", s)
    return _NL.sub("\n\n", s).strip()


def chunk_text(text: str, max_chars: int = 4000, overlap: int = 400) -> list[Chunk]:
    """Разбивка на чанки по границам абзацев с перекрытием (для L4 и Manticore)."""
    if len(text) <= max_chars:
        return [Chunk(text=text, ordinal=0)] if text else []
    chunks: list[Chunk] = []
    start = 0
    i = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            # режем по последней границе абзаца/пробела
            cut = text.rfind("\n\n", start, end)
            if cut == -1 or cut - start < max_chars // 2:
                cut = text.rfind(" ", start, end)
            end = max(cut, start + 1)
        chunks.append(Chunk(text=text[start:end], ordinal=i))
        i += 1
        start = end - overlap
    return chunks


class BaseExtractor:
    """Необязательные хелперы; плагину достаточно реализовать supports/extract."""

    def probe_ext(self, probe: FileProbe, exts: tuple[str, ...]) -> bool:
        return probe.ext in exts


class BinaryFallback(BaseExtractor):
    """Файл неизвестного формата: не извлекаем текст, но имя и байты индексируются."""
    name = "binary_fallback"
    priority = 999                       # всегда последний
    formats = ("*/*",)

    def supports(self, probe: FileProbe) -> Support:
        return Support.FALLBACK

    def extract(self, rec: Any, ctx: ExtractCtx) -> Artifacts:
        return Artifacts(searchable_bytes=True)


registry.register("extractor", BinaryFallback(), "builtin")
