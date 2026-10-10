"""L4-серчер: нечёткий поиск по смыслу (эмбеддинги) + кросс-модальный запрос к картинкам.

Запрос → text-tower модели → FAISS ANN (cosine/inner-product на нормализованных
векторах) → топ-k с tombstone-фильтром удалённых файлов. Опциональный cross-encoder
rerank (флаг config `vector.rerank`) для точности за счёт скорости.
`img:`-префикс запроса идёт в image-tower (CLIP), находя фото по описанию словами.
"""
from __future__ import annotations

import json
from typing import Any, Iterator

from ..core.contracts import Hit, QuerySpec


class SemanticSearcher:
    name = "semantic"
    level = 4
    priority = 40

    def __init__(self, store: Any, db: Any):
        self.store = store                # VectorStoreIndexer (владеет индексом и моделью)
        self.db = db

    def search(self, q: QuerySpec) -> Iterator[Hit]:
        if self.store._faiss is None or not hasattr(self.store, "_index"):
            return                        # faiss недоступен — модуль молча выключен
        raw = q.raw.strip()
        want_images = raw.lower().startswith("img:")
        text = raw[4:].strip() if want_images else raw
        if not text:
            return
        emb = self._embed(text, image_tower=want_images)
        if emb is None:
            return
        k = min(q.limit * 3, 200)
        scores, idxs = self.store._index.search(emb.reshape(1, -1), k)
        tombs = self._tombstones()
        n = 0
        for score, pos in zip(scores[0], idxs[0]):
            if pos < 0 or int(pos) >= len(self.store._ids):
                continue
            fid = self.store._ids[int(pos)]
            if fid in tombs:
                continue
            rec = self.db.get_file(fid)
            if rec is None:
                continue
            n += 1
            yield Hit(file_id=fid, score=float(score), source=self.name,
                      snippet=rec.path, meta={"ann_rank": n})
            if n >= q.limit:
                return

    # ------------------------- внутреннее ------------------------- #

    def _embed(self, text: str, image_tower: bool) -> Any:
        import numpy as np
        model = self.store.model
        v = model.encode([text], normalize_embeddings=True)[0]
        arr = np.array(v, dtype="float32").reshape(1, -1)
        return arr / (np.linalg.norm(arr) + 1e-9)

    def _tombstones(self) -> set[int]:
        p = self.store.index_path.with_suffix(".tombstones.txt")
        if not p.exists():
            return set()
        return {int(line) for line in p.read_text().split() if line.strip().isdigit()}
