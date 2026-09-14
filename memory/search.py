# -*- coding: utf-8 -*-
"""
Механика поиска: плотный вектор, разреженный BM25, объединение и порог.

Решение «нашлось ли релевантное» принимается не по позиции в выдаче, а по двум
независимым признакам. Первый — плотная косинусная близость: она сопоставима
между режимами, в отличие от RRF, у которого своя шкала. Второй — лексическое
попадание: если фрагмент содержит термин запроса («Флексо-4», «приложение 5»),
он релевантен, даже когда плотная близость низкая. Именно ради таких терминов и
добавлен разреженный вектор, и отбрасывать их по плотному порогу было бы
самоопровержением.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from memory.embeddings import get_embedder
from memory.sparse import BM25Encoder, terms
from memory.vector_store import Hit, StoreError, open_store

_CACHE: dict[str, object] = {}
MIN_TERM = 3


@dataclass
class SearchOutcome:
    hits: list[Hit]
    below_threshold: list[Hit]
    embedder: str
    fallback_reason: str | None
    index_info: dict
    hybrid: bool = False


def _embedder(cfg, client):
    key = f"embedder|{id(cfg)}|{'client' if client else 'none'}"
    if key not in _CACHE:
        _CACHE[key] = get_embedder(cfg, client)
    return _CACHE[key]


def bm25_path(cfg, name: str) -> Path:
    return cfg.root / cfg.settings["memory"]["store_dir"] / f"{name}.bm25.json"


def _bm25(cfg, name: str) -> BM25Encoder | None:
    key = f"bm25|{id(cfg)}|{name}"
    if key not in _CACHE:
        _CACHE[key] = BM25Encoder.load(bm25_path(cfg, name))
    return _CACHE[key]  # type: ignore[return-value]


def _lexical_match(hit: Hit, query: str) -> bool:
    """Есть ли в фрагменте хоть один содержательный термин запроса.

    Сравниваются основы, а не словоформы: иначе «приложение» в запросе не
    совпадёт с «приложению» в тексте пункта.
    """
    in_text = {t for t in terms(hit.text or "") if len(t) >= MIN_TERM}
    return any(t in in_text for t in terms(query) if len(t) >= MIN_TERM)


def semantic_search(cfg, name: str, query: str, *, client=None, k: int | None = None,
                    where: dict | None = None, min_score: float | None = None) -> SearchOutcome:
    mem = cfg.settings["memory"]
    k = int(k or mem["top_k"])

    embedder, reason = _embedder(cfg, client)
    store = open_store(cfg, name)
    if not store.exists():
        raise StoreError(f"Индекс «{name}» не собран. Выполните: python -m memory.build")

    hybrid = bool(store.info.get("hybrid"))
    sparse_query = None
    if hybrid:
        encoder = _bm25(cfg, name)
        if encoder is not None:
            sparse_query = encoder.encode_query(query)

    vector = embedder.encode([query])[0]
    hits = store.search(vector, k=k, where=where, embedder_name=embedder.name,
                        sparse_query=sparse_query)

    dense_threshold = float(mem["min_score"] if min_score is None else min_score)
    good, weak = [], []
    for h in hits:
        dense = h.dense if h.dense is not None else h.score
        if dense >= dense_threshold or _lexical_match(h, query):
            good.append(h)
        else:
            weak.append(h)

    return SearchOutcome(hits=good, below_threshold=weak, embedder=embedder.name,
                         fallback_reason=reason, index_info=store.info,
                         hybrid=hybrid and sparse_query is not None)


def reset_cache() -> None:
    _CACHE.clear()
