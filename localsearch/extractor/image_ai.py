"""AI-экстрактор содержимого изображений (L4): CLIP/SigLIP-эмбеддинги + опц. caption.

Модуль ТЯЖЁЛЫЙ (модель ~1-2 ГБ) — по умолчанию выключен (`vector.enabled: false`).
Векторы кладутся в Artifacts.vectors [(vec, 'image')], текстовые чанки-описания
(BLIP/caption) — в chunks, чтобы фразовый поиск Manticore тоже видел содержание фото.
Импорты torch/transformers — ленивые, внутри методов.
"""
from __future__ import annotations

from typing import Any

from ..core.contracts import Artifacts, Chunk, ExtractCtx, FileProbe, Support
from ..core.registry import registry
from .base import BaseExtractor

_MODEL_CACHE: dict[str, Any] = {}


class ImageAIExtractor(BaseExtractor):
    name = "image_ai"
    priority = 55                          # после image_meta (EXIF важнее как fallback)
    formats = ("image/jpeg", "image/png", "image/webp")
    model_id: str = "clip-ViT-B-32-multilingual-v1"   # sentence-transformers
    caption_enabled: bool = False          # BLIP — ещё тяжелее, только явно

    def supports(self, probe: FileProbe) -> Support:
        if not _available():
            return Support.NO              # модуль выключен в конфиге → не участвует
        if probe.mime.startswith("image/") and probe.mime != "image/gif":
            return Support.YES
        return Support.NO

    def extract(self, rec: Any, ctx: ExtractCtx) -> Artifacts:
        vec = self._embed_image(ctx)
        chunks: list[Chunk] = []
        if self.caption_enabled:
            cap = self._caption(ctx)
            if cap:
                chunks.append(Chunk(text=cap, ordinal=0, title="caption"))
        art = Artifacts(chunks=chunks, searchable_bytes=False)
        if vec is not None:
            art.vectors = [(vec, "image")]
        return art

    # ------------------------- внутреннее ------------------------- #

    def _embed_image(self, ctx: ExtractCtx) -> list[float] | None:
        try:
            from PIL import Image
            model = _get_model(self.model_id)
            with ctx.open() as f:
                img = Image.open(f).convert("RGB")
            return [float(x) for x in model.encode(img, convert_to_numpy=True)]
        except Exception as e:
            ctx.log(f"clip embed failed: {e}")
            return None

    def _caption(self, ctx: ExtractCtx) -> str:
        try:
            from transformers import BlipProcessor, BlipForConditionalGeneration
            import torch
            from PIL import Image
            proc = _get_model("blip-processor", lambda: BlipProcessor.from_pretrained(
                "Salesforce/blip-image-captioning-base"))
            model = _get_model("blip-model", lambda: BlipForConditionalGeneration.from_pretrained(
                "Salesforce/blip-image-captioning-base"))
            with ctx.open() as f:
                img = Image.open(f).convert("RGB")
            inputs = proc(images=img, return_tensors="pt")
            with torch.no_grad():
                ids = model.generate(**inputs, max_new_tokens=40)
            return proc.decode(ids[0], skip_special_tokens=True)
        except Exception as e:
            ctx.log(f"caption failed: {e}")
            return ""


def _available() -> bool:
    """Флаг ставит app.py из конфига при композиции (vector.enabled)."""
    return getattr(ImageAIExtractor, "_enabled", False)


def _get_model(key: str, factory: Any = None) -> Any:
    m = _MODEL_CACHE.get(key)
    if m is None:
        if key == "clip-ViT-B-32-multilingual-v1":
            from sentence_transformers import SentenceTransformer
            m = SentenceTransformer(key)
        elif factory:
            m = factory()
        _MODEL_CACHE[key] = m
    return m


registry.register("extractor", ImageAIExtractor(), "builtin")
