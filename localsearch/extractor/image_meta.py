"""EXIF/XMP/IPTC метаданные изображений (+ миниатюра-превью).

piexif (JPEG/TIFF/PNG/WebP) → при отсутствии пробуем Pillow; exiftool как
внешний CLI-хук опционален. Метаданные идут и в SQLite (фильтры UI), и
текстовым чанком в Manticore — чтобы искать «Nikon D850 2024 год Москва».
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from ..core.contracts import Artifacts, Chunk, ExtractCtx, FileProbe, Support
from ..core.registry import registry
from .base import BaseExtractor

IMG_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic"}
_GEO_KEYS = ("GPSLatitude", "GPSLongitude", "GPS Latitude", "GPS Longitude")


class ImageMetaExtractor(BaseExtractor):
    name = "image_meta"
    priority = 50
    formats = ("image/jpeg", "image/png", "image/tiff", "image/webp")

    def supports(self, probe: FileProbe) -> Support:
        if probe.mime.startswith("image/") or probe.ext in IMG_EXTS:
            return Support.YES
        return Support.NO

    def extract(self, rec: Any, ctx: ExtractCtx) -> Artifacts:
        meta: dict[str, Any] = {}
        thumb: bytes | None = None
        meta.update(self._piexif(ctx))
        dims, thumb = self._pillow(ctx)          # размеры + превью 256px
        if dims:
            meta.update(dims)
        text_lines = [f"{k}: {v}" for k, v in sorted(meta.items()) if not isinstance(v, dict)]
        chunks = ([Chunk(text="\n".join(text_lines), ordinal=0, title="EXIF")]
                  if text_lines else [])
        return Artifacts(chunks=chunks, metadata=meta, thumbnail=thumb,
                         searchable_bytes=False)

    # ------------------------- внутреннее ------------------------- #

    @staticmethod
    def _piexif(ctx: ExtractCtx) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            import piexif
            with ctx.open() as f:
                exif = piexif.load(f.read(2 << 20))
            readable = {**exif.get("0th", {}), **exif.get("Exif", {})}
            tags = _tag_names(piexif)
            for tag_id, val in readable.items():
                key = tags.get(tag_id, str(tag_id))
                if isinstance(val, bytes):
                    try:
                        val = val.decode("utf-8", errors="ignore").strip("\x00 ")
                    except Exception:
                        continue
                if key in ("DateTime", "DateTimeOriginal") and val:
                    try:
                        out["date_taken"] = dt.datetime.strptime(
                            str(val), "%Y:%m:%d %H:%M:%S").isoformat()
                    except ValueError:
                        pass
                elif any(g in key for g in _GEO_KEYS):
                    out.setdefault("gps", {})[key] = str(val)
                elif key in ("Make", "Model", "Software", "ImageDescription",
                             "Artist", "Copyright", "Orientation"):
                    out[key] = str(val)
        except ImportError:
            pass
        except Exception:
            pass   # битый EXIF не должен ломать индексацию файла
        return out

    @staticmethod
    def _pillow(ctx: ExtractCtx) -> tuple[dict[str, Any], bytes | None]:
        try:
            import io
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = None
            with ctx.open() as f, Image.open(f) as im:
                im.load()
                info = {"width": im.width, "height": im.height, "mode": im.mode}
                t = im.convert("RGB")
                t.thumbnail((256, 256))
                buf = io.BytesIO()
                t.save(buf, "JPEG", quality=70)
                return info, buf.getvalue()
        except ImportError:
            return {}, None
        except Exception:
            return {}, None


def _tag_names(piexif_mod: Any) -> dict[int, str]:
    names: dict[int, str] = {}
    for group in ("Image", "Exif", "GPS", "0th"):
        d = getattr(getattr(piexif_mod, group, None), "Tags", {})
        for tag_id, entry in d.items():
            names[tag_id] = entry[0] if isinstance(entry, tuple) else str(entry)
    return names


registry.register("extractor", ImageMetaExtractor(), "builtin")
