"""Экстрактор архивов: .zip/.tar(.gz)/.7z/.rar — рекурсивная распаковка во временную директорию.

Извлечённые участники ставятся в очередь как «дочерние» файлы (parent_id + member_name)
и проходят через тот же конвейер экстракторов. Защита от zip-bomb: лимиты на число
участников и суммарный размер; skip __MACOSX и бинарных мусорных веток.
"""
from __future__ import annotations

import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from ..core.contracts import Artifacts, ExtractCtx, FileProbe, Support
from ..core.registry import registry
from .base import BaseExtractor

MAX_MEMBERS = 2_000
MAX_TOTAL_MB = 500


class ArchiveExtractor(BaseExtractor):
    name = "archive"
    priority = 60
    formats = ("application/zip", "application/x-tar", "application/gzip",
               "application/x-7z-compressed", "application/x-rar")

    def supports(self, probe: FileProbe) -> Support:
        if probe.ext in (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar"):
            return Support.YES
        return Support.NO

    def extract(self, rec: Any, ctx: ExtractCtx) -> Artifacts:
        """Сам архив текстового чанка не даёт; главное — enqueue детей."""
        ext = (rec.ext if rec else "") or ""
        outdir = Path(tempfile.mkdtemp(prefix="lsarch_"))
        n = 0
        try:
            if ext == ".zip" or (ext == ".jar" or ext == ".apk"):
                n = self._unzip(ctx, outdir, rec.id)
            elif ext in (".tar", ".gz", ".tgz", ".bz2", ".xz"):
                n = self._untar(ctx, outdir, rec.id)
            elif ext in (".7z", ".rar"):
                n = self._unpack_7z_rar(ctx, outdir, rec.id)
        except Exception as e:
            ctx.log(f"archive failed: {e}")
        finally:
            # дети уже скопированы/прочитаны при индексации — чистим по завершении job'а
            pass
        return Artifacts(metadata={"members_enqueued": n}, searchable_bytes=False)

    # ------------------------- внутреннее ------------------------- #

    def _guard(self, name: str) -> bool:
        return not (name.startswith("__MACOSX") or "/." in f"/{name}"
                    or name.endswith(("/Thumbs.db", "/desktop.ini")))

    def _unzip(self, ctx: ExtractCtx, outdir: Path, parent_id: int) -> int:
        n = total = 0
        with ctx.open() as f, zipfile.ZipFile(f) as z:
            infos = [i for i in z.infolist() if not i.is_dir() and self._guard(i.filename)]
            if len(infos) > MAX_MEMBERS:
                ctx.log(f"archive: >{MAX_MEMBERS} участников, обрезано")
                infos = infos[:MAX_MEMBERS]
            for info in infos:
                total += info.file_size
                if total > MAX_TOTAL_MB << 20:
                    ctx.log("archive: превышен лимит размера (zip-bomb guard)")
                    break
                target = outdir / Path(info.filename).name   # плоская раскладка против path traversal
                with z.open(info) as src, open(target, "wb") as dst:
                    for chunk_b in iter(lambda: src.read(1 << 20), b""):
                        dst.write(chunk_b)
                ctx.enqueue_child(target, parent_id, info.filename)
                n += 1
        return n

    def _untar(self, ctx: ExtractCtx, outdir: Path, parent_id: int) -> int:
        n = 0
        with ctx.open() as f, tarfile.open(fileobj=f) as t:
            for m in t.getmembers():
                if not m.isfile() or n >= MAX_MEMBERS or not self._guard(m.name):
                    continue
                if m.size > (MAX_TOTAL_MB << 20) // max(n + 1, 1):
                    break
                target = outdir / Path(m.name).name
                src = t.extractfile(m)
                if src is None:
                    continue
                with open(target, "wb") as dst:
                    for chunk_b in iter(lambda: src.read(1 << 20), b""):
                        dst.write(chunk_b)
                ctx.enqueue_child(target, parent_id, m.name)
                n += 1
        return n

    def _unpack_7z_rar(self, ctx: ExtractCtx, outdir: Path, parent_id: int) -> int:
        """py7zr / rarfile — опциональные зависимости; без них — пропуск."""
        ext = getattr(ctx, "path", Path("")).suffix.lower()
        n = 0
        try:
            if ext == ".7z":
                import py7zr
                with ctx.open() as f, py7zr.SevenZipFile(f) as a:
                    for name, bio in a.readall().items():
                        if n >= MAX_MEMBERS or not self._guard(name):
                            continue
                        target = outdir / Path(name).name
                        target.write_bytes(bio.read())
                        ctx.enqueue_child(target, parent_id, name)
                        n += 1
            else:
                import rarfile
                with rarfile.RarFile(str(getattr(ctx, "path", ""))) as rf:
                    for info in rf.infolist():
                        if info.is_dir() or n >= MAX_MEMBERS or not self._guard(info.filename):
                            continue
                        target = outdir / Path(info.filename).name
                        with rf.open(info) as src, open(target, "wb") as dst:
                            for chunk_b in iter(lambda: src.read(1 << 20), b""):
                                dst.write(chunk_b)
                        ctx.enqueue_child(target, parent_id, info.filename)
                        n += 1
        except ImportError as e:
            ctx.log(f"7z/rar недоступен ({e}) — установите py7zr/rarfile")
        return n


registry.register("extractor", ArchiveExtractor(), "builtin")
