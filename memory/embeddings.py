# -*- coding: utf-8 -*-
"""
Эмбеддеры для векторной памяти.

Основной — `bge-m3` через Ollama: модель мультиязычная, а корпус у нас
русскоязычный (регламенты) с вкраплениями кода. Она же остаётся доступной в
закрытом контуре, где облачных эмбеддингов нет.

Запасной — лексический хеширующий эмбеддер на символьных n-граммах. Он НЕ
семантический и нужен ровно для одного: чтобы индекс собирался и поиск работал
на машине без Ollama — например при прогоне тестов в CI. Какой эмбеддер
использован, пишется в метаданные индекса, чтобы результаты нельзя было
перепутать: индекс, собранный одним эмбеддером, другим не ищется.
"""
from __future__ import annotations

import re
import zlib
from typing import Iterable, Protocol

import numpy as np

TOKEN_RE = re.compile(r"[\w\-\.]+", re.UNICODE)


class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: Iterable[str]) -> np.ndarray: ...


def _l2(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class HashingEmbedder:
    """Символьные n-граммы и слова, хешированные в фиксированную размерность.

    Ловит совпадения по словоформам и по номерам («4.3», «табл. 27»), что для
    нормативного текста работает неожиданно неплохо, но синонимов не понимает.
    """

    def __init__(self, dim: int = 1024, ngrams: tuple[int, ...] = (3, 4, 5)):
        self.dim = int(dim)
        self.ngrams = ngrams
        self.name = f"hashing-{self.dim}"

    def _features(self, text: str) -> list[str]:
        low = text.lower()
        feats = TOKEN_RE.findall(low)
        compact = re.sub(r"\s+", " ", low)
        for n in self.ngrams:
            feats.extend(compact[i:i + n] for i in range(max(0, len(compact) - n + 1)))
        return feats

    def encode(self, texts: Iterable[str]) -> np.ndarray:
        rows = []
        for text in texts:
            vec = np.zeros(self.dim, dtype=np.float32)
            for feat in self._features(str(text)):
                idx = zlib.crc32(feat.encode("utf-8")) % self.dim
                vec[idx] += 1.0
            # сублинейный tf: длинный документ не должен выигрывать одним объёмом
            np.log1p(vec, out=vec)
            rows.append(vec)
        return _l2(np.vstack(rows)) if rows else np.zeros((0, self.dim), dtype=np.float32)


class OllamaEmbedder:
    """Эмбеддинги через роль `embeddings` профиля моделей."""

    def __init__(self, client, role: str = "embeddings", batch: int | None = None,
                 progress=None):
        self.client = client
        self.role = role
        # Размер пачки — из конфига: она уходит в ОДНОМ запросе и считается на
        # сервере последовательно, поэтому от него зависит, упрётся ли сборка
        # в таймаут. По умолчанию берём скромный, а не быстрый.
        self.batch = int(batch or getattr(client, "embed_batch", 8) or 8)
        self.progress = progress
        spec = client.cfg.model_for(role)
        self.name = f"{spec.provider}:{spec.model}"
        self.dim = 0  # станет известна после первого вызова

    def encode(self, texts: Iterable[str]) -> np.ndarray:
        items = [str(t) for t in texts]
        out: list[list[float]] = []
        for i in range(0, len(items), self.batch):
            out.extend(self.client.embed(items[i:i + self.batch], role=self.role))
            if self.progress:
                self.progress(min(i + self.batch, len(items)), len(items))
        if not out:
            return np.zeros((0, self.dim or 1), dtype=np.float32)
        matrix = np.asarray(out, dtype=np.float32)
        self.dim = matrix.shape[1]
        return _l2(matrix)


def get_embedder(cfg, client=None, *, force_fallback: bool = False) -> tuple[Embedder, str | None]:
    """Возвращает эмбеддер и, если пришлось откатиться, причину отката."""
    fallback_dim = int(cfg.settings["memory"].get("embedding_dim_fallback", 1024))
    if force_fallback:
        return HashingEmbedder(fallback_dim), "запрошен запасной эмбеддер"
    if client is None:
        return HashingEmbedder(fallback_dim), "клиент моделей не передан"
    try:
        emb = OllamaEmbedder(client)
        emb.encode(["проверка доступности эмбеддера"])
        return emb, None
    except Exception as exc:  # noqa: BLE001 — сбой эмбеддера не должен ронять сборку индекса
        return HashingEmbedder(fallback_dim), f"{type(exc).__name__}: {exc}"
