"""Экстрактор файлов словаря «Лексикон» и ABBYY Lingvo DSL.

Форматы:
  *.dsl      — ABBYY Lingvo: текстовый, статьи вида `заголовок`<br>статья; кодировка
               обычно UTF-16LE или cp1251. Каждая статья = отдельный Chunk(title=заголовок)
               → Manticore ищет по заголовкам статей как фразам.
  *.lsd      — закрытый бинарный формат Лексикона. Здесь — каркас: парсится заголовок
               и извлекаются видимые UTF-16 строки (эвристика); полноценный reverse-
               парсер подключается как внешний плагин (см. plugins_example).
  *.lex      — старый формат Лексикона: пробуем как DSL/UTF-16.
"""
from __future__ import annotations

import re
from typing import Any

from ..core.contracts import Artifacts, Chunk, ExtractCtx, FileProbe, Support
from ..core.registry import registry
from .base import BaseExtractor, clean_text

_DSL_ARTICLE = re.compile(r"`(?P<head>[^<>`]{1,128})`\s*(?P<body>.+?)(?=`[^\s<>]|$)",
                          re.S)
_TAG = re.compile(r"<[^>]+>")
_UTF16_RUN = re.compile(rb"(?:[\x20-\x7e\x80-\xff]\x00){4,}")   # печатные UTF-16LE куски


class LexiconExtractor(BaseExtractor):
    name = "lexicon"
    priority = 40
    formats = ("application/x-lingvo-dsl", "application/x-lexicon",
               "application/x-lexicon-lsd", "application/x-lexicon-bylx")

    def supports(self, probe: FileProbe) -> Support:
        if probe.ext in (".dsl", ".lsd", ".bylx", ".lex"):
            return Support.YES
        if probe.mime.startswith("application/x-lexicon"):
            return Support.YES
        return Support.NO

    def extract(self, rec: Any, ctx: ExtractCtx) -> Artifacts:
        ext = (rec.ext if rec else ctx_ext(ctx)) or ""
        if ext == ".dsl" or ext == ".lex":
            return self._parse_dsl(ctx)
        if ext == ".lsd":
            return self._parse_lsd(ctx)
        return Artifacts()   # bylx — только через внешний плагин

    # ------------------------- DSL ------------------------- #

    def _parse_dsl(self, ctx: ExtractCtx) -> Artifacts:
        raw = ctx.read_bytes(limit=64 << 20)
        text = self._decode(raw)
        chunks: list[Chunk] = []
        for i, m in enumerate(_DSL_ARTICLE.finditer(text)):
            body = clean_text(_TAG.sub(" ", m.group("body")))
            if len(body) >= 2:
                chunks.append(Chunk(text=f"{m.group('head')}\n{body}",
                                    ordinal=i, title=m.group("head").strip()))
        return Artifacts(chunks=chunks[:50_000],
                         metadata={"format": "dsl", "articles": len(chunks)})

    @staticmethod
    def _decode(b: bytes) -> str:
        if b.startswith(b"\xff\xfe") or b[:2] == b"\x00" :
            try:
                return b.decode("utf-16", errors="ignore")
            except Exception:
                pass
        for enc in ("utf-16-le", "cp1251", "utf-8"):
            try:
                s = b.decode(enc)
                if sum(c.isalpha() or c.isspace() for c in s[:2000]) > 1500:
                    return s
            except UnicodeDecodeError:
                continue
        return b.decode("utf-16-le", errors="ignore")

    # ------------------------- LSD (эвристический каркас) ------------------------- #

    def _parse_lsd(self, ctx: ExtractCtx) -> Artifacts:
        """Закрытый формат: извлекаем длинные ASCII-последовательности из UTF-16-блоков.

        Это компромисс 'хотя бы что-то индексируется'; точный парсер — внешний плагин.
        """
        raw = ctx.read_bytes(limit=128 << 20)
        found: list[str] = []
        for m in _UTF16_RUN.finditer(raw):
            s = m.group().decode("utf-16-le", errors="ignore").strip()
            if len(s) >= 6:
                found.append(s)
        text = clean_text("\n".join(found))
        from .base import chunk_text
        return Artifacts(chunks=chunk_text(text),
                         metadata={"format": "lsd-heuristic", "strings": len(found)})


def ctx_ext(ctx: ExtractCtx) -> str:
    p = getattr(ctx, "path", None)
    return p.suffix.lower() if p else ""


registry.register("extractor", LexiconExtractor(), "builtin")
