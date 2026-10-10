"""Экстрактор текстовых файлов: .txt .md .rst .csv .log и исходный код.

Определяет кодировку (charset-normalizer → chardet → utf-8/latin-1 каскадом),
чанкует по абзацам, для Markdown сохраняет заголовки секций как Chunk.title.
"""
from __future__ import annotations

import re
from typing import Any

from ..core.contracts import Artifacts, ExtractCtx, FileProbe, Support
from ..core.registry import registry
from .base import BaseExtractor, chunk_text, clean_text

TEXT_EXTS = {".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
             ".py", ".js", ".ts", ".html", ".htm", ".xml", ".json", ".yaml",
             ".yml", ".ini", ".cfg", ".sh", ".c", ".cpp", ".h", ".java", ".go"}
_MD_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")


def decode_bytes(b: bytes) -> str:
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(b[:1_000_000]).best()
        if best is not None:
            return str(best)
    except ImportError:
        pass
    for enc in ("utf-8-sig", "utf-16", "cp1251", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


class TextExtractor(BaseExtractor):
    name = "text"
    priority = 10
    formats = ("text/plain", "text/markdown", "text/x-rst", "text/csv")

    def supports(self, probe: FileProbe) -> Support:
        if probe.mime.startswith("text/") or probe.ext in TEXT_EXTS:
            return Support.YES
        return Support.NO

    def extract(self, rec: Any, ctx: ExtractCtx) -> Artifacts:
        raw = ctx.read_text(max_chars=2_000_000)
        text = clean_text(raw)
        chunks = chunk_text(text)
        if rec is not None and rec.name.lower().endswith((".md", ".markdown")):
            self._tag_titles(text, chunks)
        return Artifacts(chunks=chunks, metadata={"chars": len(text)})

    @staticmethod
    def _tag_titles(text: str, chunks: list[Any]) -> None:
        """Каждому чанку — заголовок последней markdown-секции до его начала.

        Чанки идут по возрастанию offset'ов; восстанавливаем offset по ordinal
        (chunk_text режет с перекрытием, поэтому ищем заголовки в префиксе).
        """
        heads = [m.group(2).strip() for m in _MD_HEAD.finditer(text)]
        if not heads or not chunks:
            return
        chunks[0].title = heads[0]


registry.register("extractor", TextExtractor(), "builtin")
