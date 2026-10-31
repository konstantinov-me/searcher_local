"""Fusion: роутинг запроса по уровням + слияние результатов (Reciprocal Rank Fusion).

Авто-роутинг (`level=auto`):
  префикс 'name:'/'*.ext'          → L1
  префикс 'bytes:'/'hex:'/чистый hex → L3
  префикс 'img:' / '~' / '?semantic' → L4 (+L2 как страховка)
  иначе                             → L2 (+L1 буст имён) + L4 если включён
Слияние нескольких источников — RRF: score = Σ 1/(k + rank_i), k=60.
Опциональный rerank тем же Manticore: собранные file_id прогоняются вторым
запросом с weight(), что даёт согласованное ранжирование без отдельной модели.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from ..core.contracts import Hit, QuerySpec

_HEX_ONLY = re.compile(r"^(?:[0-9a-fA-F]{2}[ ]?){3,}$")


def route(q: QuerySpec, available: list[str]) -> list[str]:
    """Какие серчеры участвуют для данного запроса."""
    s = q.raw.strip().lower()
    if q.level != "auto":
        want = {"name": "filename", "phrase": "phrases", "fuzzy": "phrases",
                "bytes": "bytes_seq", "semantic": "semantic"}[q.level]
        return [want] if want in available else available[:1]
    if s.startswith(("name:", "*")) and "filename" in available:
        return ["filename"]
    if (s.startswith(("bytes:", "hex:")) or _HEX_ONLY.match(s)) and "bytes_seq" in available:
        return ["bytes_seq"]
    if s.startswith("img:") and "semantic" in available:
        return ["semantic", "phrases"] if "phrases" in available else ["semantic"]
    main = ["phrases"] if "phrases" in available else []
    extra = [x for x in ("filename", "semantic") if x in available]
    return main + extra


def rrf(hits_by_source: dict[str, list[Hit]], k: int = 60) -> list[Hit]:
    fused: dict[int, float] = {}
    best: dict[int, Hit] = {}
    for source, hits in hits_by_source.items():
        for rank, h in enumerate(hits, start=1):
            fused[h.file_id] = fused.get(h.file_id, 0.0) + 1.0 / (k + rank)
            cur = best.get(h.file_id)
            if cur is None or h.score > cur.score:
                best[h.file_id] = h
    ordered = sorted(fused.items(), key=lambda kv: -kv[1])
    out: list[Hit] = []
    for fid, score in ordered:
        h = best[fid]
        ranks = {src: next((i for i, x in enumerate(hits, 1) if x.file_id == fid), None)
                 for src, hits in hits_by_source.items()
                 if any(x.file_id == fid for x in hits)}
        out.append(Hit(file_id=fid, score=score, source="fusion:" + "+".join(hits_by_source),
                       snippet=h.snippet, span=h.span,
                       meta={**h.meta, "per_source_ranks": ranks}))
    return out


def fanout(searchers: dict[str, Any], q: QuerySpec) -> list[Hit]:
    """Прогнать запрос через все назначенные серчеры и слить."""
    names = route(q, list(searchers))
    per_source: dict[str, list[Hit]] = {}
    for name in names:
        try:
            per_source[name] = list(searchers[name].search(q))
        except Exception:
            per_source[name] = []       # упавший источник не роняет весь поиск
    if len(per_source) <= 1:
        return sum(per_source.values(), [])
    return rrf(per_source)
