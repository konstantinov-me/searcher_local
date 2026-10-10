"""Экстракторы Office-документов: OOXML (.docx/.xlsx/.pptx) и ODF (.odt/.ods/.odp).

OOXML — это zip с XML внутри, поэтому разбираем напрямую zipfile+ElementTree
без тяжёлых зависимостей (python-docx/openpyxl не нужны). Извлекаем текст
параграфов/ячеек/слайдов и core-properties (автор, дата) в метаданные.
"""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

from ..core.contracts import Artifacts, ExtractCtx, FileProbe, Support
from ..core.registry import registry
from .base import BaseExtractor, chunk_text, clean_text

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
CP_NS = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
ODF_TEXT = re.compile(r"<[^>]+>")

_OOXML_ROUTES = {  # ext → (mime, путь к содержимому внутри zip)
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              "word/document.xml"),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
              "xl/sharedStrings.xml"),
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation",
              None),  # special-case: ppt/slides/slideN.xml
}


class OfficeExtractor(BaseExtractor):
    name = "office"
    priority = 30
    formats = tuple(m for m, _ in _OOXML_ROUTES.values() if m) + (
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet")

    def supports(self, probe: FileProbe) -> Support:
        return Support.YES if probe.ext in _OOXML_ROUTES or probe.ext in (
            ".odt", ".ods", ".odp") else Support.NO

    def extract(self, rec: Any, ctx: ExtractCtx) -> Artifacts:
        ext = (rec.ext if rec else "") or ""
        with ctx.open() as f:
            if not zipfile.is_zipfile(f):
                return Artifacts()
            z = zipfile.ZipFile(f)
            meta = self._core_props(z)
            if ext in _OOXML_ROUTES:
                text = self._ooxml_text(z, ext)
            else:
                text = self._odf_text(z)
        chunks = chunk_text(clean_text(text))
        return Artifacts(chunks=chunks, metadata=meta)

    # ------------------------- внутреннее ------------------------- #

    @staticmethod
    def _core_props(z: zipfile.ZipFile) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            root = ET.fromstring(z.read("docProps/core.xml"))
            for tag in ("creator", "title", "subject", "description"):
                el = root.find(f"{CP_NS}{tag}")
                if el is not None and el.text:
                    out[tag] = el.text
        except (KeyError, ET.ParseError):
            pass
        return out

    @classmethod
    def _ooxml_text(cls, z: zipfile.ZipFile, ext: str) -> str:
        parts: list[str] = []
        if ext == ".docx":
            root = ET.fromstring(z.read("word/document.xml"))
            for p in root.iter(f"{W_NS}p"):
                t = "".join(node.text or "" for node in p.iter(f"{W_NS}t"))
                if t.strip():
                    parts.append(t)
        elif ext == ".xlsx":
            try:
                root = ET.fromstring(z.read("xl/sharedStrings.xml"))
                parts = ["".join(n.text or "" for n in si.iter(f"{S_NS}t"))
                         for si in root.iter(f"{S_NS}si")]
            except KeyError:
                pass   # workbook без shared strings — пропустим
        elif ext == ".pptx":
            names = sorted((n for n in z.namelist()
                            if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                           key=lambda s: int(re.search(r"\d+", s.split("/")[-1]).group()))
            for n in names:
                root = ET.fromstring(z.read(n))
                parts.append("\n".join(
                    "".join(t.text or "" for t in p.iter(f"{A_NS}t"))
                    for p in root.iter(f"{A_NS}p")))
        return "\n\n".join(parts)

    @staticmethod
    def _odf_text(z: zipfile.ZipFile) -> str:
        for entry in ("content.xml",):
            try:
                raw = z.read(entry).decode("utf-8", errors="replace")
            except KeyError:
                continue
            # грубо, но без зависимости от odfpy: режем теги, оставляя текстовые узлы
            txt = raw.replace("</text:p>", "\n").replace("</text:h>", "\n")
            return ODF_TEXT.sub("", txt)
        return ""


registry.register("extractor", OfficeExtractor(), "builtin")
