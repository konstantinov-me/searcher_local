"""Экстрактор PDF (.pdf): pypdf → pdfplumber каскадом; опциональный OCR сканов.

OCR-хук (pytesseract + pdf2image) включается только явно: config
`plugins.extractors.overrides.pdf.ocr: true` — он медленный и тяжёлый.
Если текста на странице нет и ocr=true — распознаём изображение страницы.
"""
from __future__ import annotations

from typing import Any

from ..core.contracts import Artifacts, ExtractCtx, FileProbe, Support
from ..core.registry import registry
from .base import BaseExtractor, chunk_text, clean_text


class PdfExtractor(BaseExtractor):
    name = "pdf"
    priority = 20
    formats = ("application/pdf",)
    ocr_enabled: bool = False            # ставится из конфига при композиций

    def supports(self, probe: FileProbe) -> Support:
        if probe.magic.startswith("25504446"):     # %PDF — надёжнее расширения
            return Support.YES
        return Support.NO

    def extract(self, rec: Any, ctx: ExtractCtx) -> Artifacts:
        with ctx.open() as f:
            pages = self._pypdf_pages(f) or self._pdfplumber_pages(f)
        meta: dict[str, Any] = {}
        try:
            f = ctx.open()
            from pypdf import PdfReader
            info = PdfReader(f).metadata or {}
            meta = {k.lstrip("/"): str(v) for k, v in info.items()
                    if k in ("/Title", "/Author", "/Subject", "/CreationDate")}
            f.close()
        except Exception:
            pass
        full = clean_text("\n\n".join(pages))
        chunks = chunk_text(full)
        if not full and self.ocr_enabled:
            chunks = chunk_text(self._ocr(ctx))
        return Artifacts(chunks=chunks, metadata={"pages": len(pages), **meta})

    # ------------------------- внутреннее ------------------------- #

    @staticmethod
    def _pypdf_pages(f: Any) -> list[str]:
        try:
            from pypdf import PdfReader
            return [(p.extract_text() or "") for p in PdfReader(f).pages]
        except ImportError:
            return []

    @staticmethod
    def _pdfplumber_pages(f: Any) -> list[str]:
        try:
            import pdfplumber
            with pdfplumber.open(f) as pdf:
                return [(p.extract_text() or "") for p in pdf.pages]
        except ImportError:
            return []

    def _ocr(self, ctx: ExtractCtx) -> str:
        try:
            import pytesseract
            from pdf2image import convert_from_path
            imgs = convert_from_path(ctx.path if hasattr(ctx, "path") else str(rec_path(ctx)))
            return "\n".join(pytesseract.image_to_string(im, lang="rus+eng") for im in imgs)
        except ImportError as e:
            ctx.log(f"OCR недоступен: {e}")
            return ""


def rec_path(ctx: ExtractCtx) -> str:
    raise RuntimeError("ctx должен предоставлять .path для OCR")


registry.register("extractor", PdfExtractor(), "builtin")
