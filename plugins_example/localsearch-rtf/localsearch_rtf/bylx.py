"""Пример внешнего плагина-экстрактора для формата .bylx (Лексикон, закрытый).

Демонстрирует оба способа расширения системы:

  1. Как pip-пакет с entry-point (см. pyproject.toml ниже комментарием):
        [project.entry-points."localsearch.extractors"]
        localsearch_bylx = "localsearch_rtf.bylx:BylxExtractor"
     Registry.discover_entrypoints() подхватит его сам после `pip install`.

  2. Как «плагин-файл»: скопировать bylx.py в
        ~/.config/localsearch/plugins/bylx.py
     и добавить в конец функцию register_extractors(registry) — её вызовет
     Registry.discover_dir() без всякого packaging.

Контракт минимален: name/priority/formats + supports(probe)->Support +
extract(rec, ctx)->Artifacts. Всё остальное — забота ядра.
"""
from __future__ import annotations

from typing import Any

# ядро импортируется как библиотека — это единственная зависимость плагина
from localsearch.core.contracts import Artifacts, Chunk, ExtractCtx, FileProbe, Support


class BylxExtractor:
    """Экстрактор .bylx: контейнер Лексикона с zlib-потоками статей.

    Парсер эвристический (структура закрытая и недокументированная): ищем
    DEFLATE-блоки (0x78 0x9C...) внутри файла, расжимаем, отбираем валидный UTF-8.
    """
    name = "bylx_ext"                    # уникальный id плагина
    priority = 41                        # сразу после встроенного 'lexicon'
    formats = ("application/x-lexicon-bylx",)

    def supports(self, probe: FileProbe) -> Support:
        return Support.YES if probe.ext == ".bylx" else Support.NO

    def extract(self, rec: Any, ctx: ExtractCtx) -> Artifacts:
        import zlib
        raw = ctx.read_bytes(limit=256 << 20)
        texts: list[str] = []
        i = 0
        while True:
            j = raw.find(b"\x78\x9c", i)   # заголовок zlib (level default)
            if j < 0 or len(texts) > 100_000:
                break
            try:
                dec = zlib.decompressobj(-15 if False else 15)
                chunk_data = dec.decompress(raw[j:], 1 << 20)
                s = chunk_data.decode("utf-8")
                if sum(c.isalpha() or c.isspace() for c in s[:500]) > 350:
                    texts.append(s)
                i = j + max(2, len(raw[j:]) - len(dec.unused_data))
            except Exception:
                i = j + 2
        chunks = [Chunk(text=t.strip(), ordinal=n, title=t.splitlines()[0][:80])
                  for n, t in enumerate(texts) if len(t.strip()) >= 20]
        return Artifacts(chunks=chunks,
                         metadata={"format": "bylx-heuristic", "streams": len(texts)})


# ------------------------- способ 2: плагин-файл ------------------------- #

def register_extractors(reg: Any) -> None:
    """Вызывается Registry.discover_dir() для файлов из ~/.config/localsearch/plugins."""
    reg.register("extractor", BylxExtractor(), source="file:bylx.py")
