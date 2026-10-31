"""L4-индексер векторов: FAISS (или numpy-fallback) по эмбеддингам текста и картинок.

consumes={'vectors'}; один индекс с полем modality ('text'/'image'); id-вектор ↔ file_id
маппится списком ids.txt рядом с .faiss. Модель эмбеддинга текстовых чанков считается
лениво (sentence-transformers), при отсутствии — модуль просто не активен.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..core.contracts import Artifacts

log = logging.getLogger("localsearch.vector")


class VectorStoreIndexer:
    name = "vector_store"
    target = "faiss"
    consumes = {"vectors"}
    priority = 40

    def __init__(self, cfg: Any, db: Any):
        self.cfg = cfg                    # settings.VectorCfg
        self.db = db
        self.index_path = Path(cfg.index_path)
        self._model: Any = None
        self._faiss: Any = None
        self._ids: list[int] = []         # позиция в индексе → file_id
        self._meta_path = self.index_path.with_suffix(".ids.json")

    # ------------------------- модель ------------------------- #

    @property
    def model(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.cfg.text_model)
        return self._model

    # ------------------------- контракт Indexer ------------------------- #

    def setup(self) -> None:
        try:
            import faiss
            self._faiss = faiss
        except ImportError:
            log.warning("faiss не установлен — L4 отключён")
            return
        if self.index_path.exists():
            self._index = faiss.read_index(str(self.index_path))
            self._ids = json.loads(self._meta_path.read_text()) if self._meta_path.exists() else []
        else:
            self._index = faiss.IndexHNSWFlat(self.cfg.dim, 32, faiss.METRIC_INNER_PRODUCT)
            self._index.hnsw.efSearch = 64
            self._ids = []

    def index(self, rec: Any, art: Artifacts) -> None:
        if self._faiss is None:
            return
        vecs = list(art.vectors)
        # текстовые чанки тоже можно эмбедить сюда же (если включено):
        if getattr(self.cfg, "embed_chunks", True) and art.chunks and not vecs:
            emb = self.model.encode([c.text[:2000] for c in art.chunks],
                                    normalize_embeddings=True)
            vecs = [([float(x) for x in v], "text") for v in emb]
        if not vecs:
            return
        import numpy as np
        arr = np.array([v for v, _ in vecs], dtype="float32")
        arr /= (np.linalg.norm(arr, axis=1, keepdims=True) + 1e-9)
        self._index.add(arr)
        self._ids.extend([rec.id] * len(vecs))
        self._persist()

    def remove(self, file_id: int) -> None:
        """FAISS не поддерживает удаление из HNSW — помечаем tombstone-файл;
        компакция происходит при rebuild."""
        tombs = self.index_path.with_suffix(".tombstones.txt")
        with open(tombs, "a") as f:
            f.write(f"{file_id}\n")

    # ------------------------- внутреннее ------------------------- #

    def _persist(self) -> None:
        self._faiss.write_index(self._index, str(self.index_path))
        self._meta_path.write_text(json.dumps(self._ids))
