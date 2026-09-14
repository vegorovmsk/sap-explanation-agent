# -*- coding: utf-8 -*-
"""
Векторное хранилище: Qdrant как основной бэкенд, numpy как запасной.

**Qdrant.** Выбран не «потому что векторная база», а за три вещи, которые нужны
именно этому агенту. Первое — именованные векторы: в одной точке лежат плотный
эмбеддинг и разреженный BM25, а выдачи объединяются по RRF прямо на стороне базы.
Нормативный текст держится на точных обозначениях («Флексо-4», «ЛП2»,
«приложение 5»), и плотная модель их размывает — разреженная половина это
чинит. Второе — фильтры по payload с индексами: отсечь охрану труда или оставить
один этап производства нужно на каждом запросе. Третье — рост: сегодня 172
фрагмента демо-стенда, в боевом контуре это сотни документов, и переезжать с
хранилища на хранилище на полпути не хочется.

Режимы: сервер по `QDRANT_URL` (полный набор возможностей, включая индексы по
payload), встроенный по `path` (без сервера, индексы payload не создаются — сам
клиент об этом предупреждает) и `:memory:` для тестов.

**numpy.** Матрица эмбеддингов в `.npz`, метаданные в `.json`, косинусная
близость перебором. Нужен как запасной вариант там, где Qdrant поднять нельзя:
и встроенный Qdrant, и Chroma держат метаданные в SQLite, а на сетевом или
синхронизируемом каталоге SQLite не получает блокировок и падает с
`disk I/O error`. Гибридного поиска здесь нет — только плотный вектор.

Индекс помнит, каким эмбеддером собран, и отказывается искать чужим, а не
возвращает правдоподобный мусор.
"""
from __future__ import annotations

import atexit
import gc
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

NAMESPACE = uuid.UUID("6ba7b812-9dad-11d1-80b4-00c04fd430c8")
SparseVec = tuple[list[int], list[float]]


@dataclass
class Hit:
    id: str
    score: float          # в гибридном режиме это RRF, в плотном — косинус
    text: str
    meta: dict
    dense: float | None = None   # косинусная близость, всегда сопоставимая между режимами

    def to_dict(self) -> dict:
        return {"id": self.id, "score": round(self.score, 4), **self.meta, "текст": self.text}


class StoreError(RuntimeError):
    pass


# ============================================================== numpy (запасной)
class NumpyVectorStore:
    backend = "numpy"
    supports_hybrid = False

    def __init__(self, path: Path, name: str, **_: Any):
        self.dir = Path(path)
        self.name = name
        self.vec_path = self.dir / f"{name}.npz"
        self.meta_path = self.dir / f"{name}.json"
        self._vectors: np.ndarray | None = None
        self._records: list[dict] = []
        self._info: dict = {}

    def build(self, ids: list[str], texts: list[str], metas: list[dict],
              vectors: np.ndarray, embedder_name: str,
              sparse: Sequence[SparseVec] | None = None) -> None:
        if not (len(ids) == len(texts) == len(metas) == len(vectors)):
            raise StoreError("Списки идентификаторов, текстов, метаданных и векторов разной длины")
        self.dir.mkdir(parents=True, exist_ok=True)
        self._vectors = np.asarray(vectors, dtype=np.float32)
        self._records = [{"id": i, "text": t, "meta": m} for i, t, m in zip(ids, texts, metas)]
        self._info = {"embedder": embedder_name, "dim": int(self._vectors.shape[1]),
                      "count": len(ids), "backend": self.backend, "hybrid": False}
        np.savez_compressed(self.vec_path, vectors=self._vectors)
        self.meta_path.write_text(
            json.dumps({"info": self._info, "records": self._records}, ensure_ascii=False),
            encoding="utf-8")

    def load(self) -> None:
        if not self.exists():
            raise StoreError(f"Индекс «{self.name}» не собран")
        data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self._info, self._records = data["info"], data["records"]
        self._vectors = np.load(self.vec_path)["vectors"]

    @property
    def info(self) -> dict:
        if not self._info:
            self.load()
        return self._info

    def exists(self) -> bool:
        return self.vec_path.exists() and self.meta_path.exists()

    def search(self, query_vector: np.ndarray, k: int = 6, where: dict | None = None,
               embedder_name: str | None = None,
               sparse_query: SparseVec | None = None) -> list[Hit]:
        if self._vectors is None:
            self.load()
        _check_embedder(self.name, self._info, embedder_name)
        q = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        if q.shape[0] != self._vectors.shape[1]:
            raise StoreError("Размерность запроса не совпадает с размерностью индекса")
        keep = np.arange(len(self._records))
        if where:
            mask = [all(r["meta"].get(key) == value for key, value in where.items())
                    for r in self._records]
            keep = np.nonzero(np.asarray(mask))[0]
            if keep.size == 0:
                return []
        scores = self._vectors[keep] @ q          # векторы нормированы
        order = np.argsort(-scores)[:k]
        return [Hit(id=self._records[keep[i]]["id"], score=float(scores[i]),
                    text=self._records[keep[i]]["text"], meta=self._records[keep[i]]["meta"],
                    dense=float(scores[i]))
                for i in order]

    def get(self, chunk_id: str) -> Hit | None:
        if self._vectors is None:
            self.load()
        for r in self._records:
            if r["id"] == chunk_id:
                return Hit(id=r["id"], score=1.0, text=r["text"], meta=r["meta"])
        return None

    def all_records(self) -> list[dict]:
        if self._vectors is None:
            self.load()
        return list(self._records)


# ===================================================================== Qdrant
_CLIENTS: dict[str, Any] = {}


def _qdrant_client(location: str, api_key: str | None, timeout: int):
    """Клиенты кэшируются: у `:memory:` без кэша каждый вызов давал бы пустую базу."""
    if location in _CLIENTS:
        return _CLIENTS[location]
    from qdrant_client import QdrantClient
    try:
        if location == ":memory:":
            client = QdrantClient(location=":memory:")
        elif location.startswith(("http://", "https://")):
            client = QdrantClient(url=location, api_key=api_key or None, timeout=timeout)
            client.get_collections()          # ранняя и понятная ошибка вместо таймаута в поиске
        else:
            client = QdrantClient(path=location)
    except StoreError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StoreError(_qdrant_hint(location, exc)) from exc
    _CLIENTS[location] = client
    return client


def _qdrant_hint(location: str, exc: Exception) -> str:
    if location.startswith(("http://", "https://")):
        return (f"Qdrant по адресу {location} недоступен: {exc}. "
                f"Поднимите его — docker run -p 6333:6333 qdrant/qdrant — "
                f"или уберите QDRANT_URL, чтобы использовать встроенный режим.")
    if "already accessed" in str(exc) or "AlreadyLocked" in type(exc).__name__:
        return (
            f"Встроенный Qdrant в {location} уже занят другим процессом: {exc}\n"
            f"    Это ВСТРОЕННЫЙ режим — значит QDRANT_URL пуст. Встроенный режим "
            f"держит каталог монопольно: одновременно с ним не может работать ни "
            f"второй python, ни Streamlit, ни незакрытый прошлый прогон.\n"
            f"    Правильное лечение, если сервер уже поднят "
            f"(docker compose up -d qdrant): впишите в .env\n"
            f"        QDRANT_URL=http://localhost:6333\n"
            f"    и повторите. Сервер снимает монополию на каталог совсем.\n"
            f"    Если сервер не нужен — закройте процесс, который держит каталог "
            f"(Streamlit, прошлый python), и повторите."
        )
    if "disk I/O" in str(exc) or "OperationalError" in type(exc).__name__:
        return (f"Встроенный Qdrant не смог открыть хранилище в {location}: {exc}. "
                f"Он держит метаданные в SQLite, а на сетевом или синхронизируемом "
                f"каталоге блокировки не работают. Варианты: указать QDRANT_PATH "
                f"на локальный диск, поднять сервер и задать QDRANT_URL, либо "
                f"переключиться на запасной бэкенд: SAP_AGENT_MEMORY_BACKEND=numpy")
    return f"Не удалось открыть Qdrant ({location}): {exc}"


class QdrantVectorStore:
    backend = "qdrant"

    def __init__(self, path: Path, name: str, *, location: str, prefix: str = "sap_",
                 api_key: str | None = None, timeout: int = 30, hybrid: bool = True):
        self.dir = Path(path)
        self.name = name
        self.location = location
        self.collection = f"{prefix}{name}"
        self.api_key = api_key
        self.timeout = timeout
        self.supports_hybrid = hybrid
        self._info: dict = {}

    @property
    def client(self):
        return _qdrant_client(self.location, self.api_key, self.timeout)

    @property
    def server_mode(self) -> bool:
        return self.location.startswith(("http://", "https://"))

    @staticmethod
    def _point_id(chunk_id: str) -> str:
        # идентификаторы точек в Qdrant — число или UUID, а у нас строки вида «ТР-ЭКС п. 4.3»
        return str(uuid.uuid5(NAMESPACE, chunk_id))

    # ------------------------------------------------------------------ запись
    def build(self, ids, texts, metas, vectors, embedder_name, sparse=None) -> None:
        from qdrant_client import models

        vectors = np.asarray(vectors, dtype=np.float32)
        dim = int(vectors.shape[1])
        use_sparse = bool(sparse) and self.supports_hybrid

        vectors_config = {"dense": models.VectorParams(size=dim, distance=models.Distance.COSINE)}
        sparse_config = {"sparse": models.SparseVectorParams()} if use_sparse else None
        try:
            if self.client.collection_exists(self.collection):
                self.client.delete_collection(self.collection)
            self.client.create_collection(self.collection, vectors_config=vectors_config,
                                          sparse_vectors_config=sparse_config)
        except StoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StoreError(_qdrant_hint(self.location, exc)) from exc

        points = []
        for i, chunk_id in enumerate(ids):
            vector: dict[str, Any] = {"dense": vectors[i].tolist()}
            if use_sparse:
                idx, val = sparse[i]
                vector["sparse"] = models.SparseVector(indices=idx, values=val)
            payload = {"chunk_id": chunk_id, "text": texts[i],
                       "_embedder": embedder_name, "_hybrid": use_sparse, **metas[i]}
            points.append(models.PointStruct(id=self._point_id(chunk_id),
                                             vector=vector, payload=payload))
        for start in range(0, len(points), 128):
            self.client.upsert(self.collection, points=points[start:start + 128], wait=True)

        # индексы по payload имеют смысл только на сервере; локальный клиент их игнорирует
        if self.server_mode:
            for field, schema in (("к_планированию", models.PayloadSchemaType.BOOL),
                                  ("этап", models.PayloadSchemaType.KEYWORD),
                                  ("документ", models.PayloadSchemaType.KEYWORD),
                                  ("файл", models.PayloadSchemaType.KEYWORD),
                                  ("chunk_id", models.PayloadSchemaType.KEYWORD)):
                try:
                    self.client.create_payload_index(self.collection, field, field_schema=schema)
                except Exception:  # noqa: BLE001 — поле может отсутствовать в этой коллекции
                    pass

        self._info = {"embedder": embedder_name, "dim": dim, "count": len(ids),
                      "backend": self.backend, "hybrid": use_sparse,
                      "collection": self.collection, "location": self.location}

    # ------------------------------------------------------------------ чтение
    def load(self) -> None:
        if not self.exists():
            raise StoreError(f"Индекс «{self.name}» не собран")
        point = self.client.scroll(self.collection, limit=1, with_payload=True)[0]
        payload = point[0].payload if point else {}
        info = self.client.get_collection(self.collection)
        params = info.config.params.vectors
        dim = params["dense"].size if isinstance(params, dict) else params.size
        self._info = {
            "embedder": payload.get("_embedder", ""), "dim": int(dim),
            "count": int(self.client.count(self.collection, exact=True).count),
            "backend": self.backend, "hybrid": bool(payload.get("_hybrid")),
            "collection": self.collection, "location": self.location,
        }

    @property
    def info(self) -> dict:
        if not self._info:
            self.load()
        return self._info

    def exists(self) -> bool:
        try:
            return self.client.collection_exists(self.collection)
        except StoreError:
            raise
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ поиск
    def _filter(self, where: dict | None):
        from qdrant_client import models
        if not where:
            return None
        return models.Filter(must=[
            models.FieldCondition(key=key, match=models.MatchValue(value=value))
            for key, value in where.items()])

    def search(self, query_vector, k: int = 6, where: dict | None = None,
               embedder_name: str | None = None,
               sparse_query: SparseVec | None = None) -> list[Hit]:
        from qdrant_client import models

        _check_embedder(self.name, self.info, embedder_name)
        dense = np.asarray(query_vector, dtype=np.float32).reshape(-1).tolist()
        qfilter = self._filter(where)

        if sparse_query and self.info.get("hybrid") and sparse_query[0]:
            indices, values = sparse_query
            result = self.client.query_points(
                self.collection,
                prefetch=[
                    models.Prefetch(query=dense, using="dense", limit=max(k * 4, 20),
                                    filter=qfilter),
                    models.Prefetch(query=models.SparseVector(indices=indices, values=values),
                                    using="sparse", limit=max(k * 4, 20), filter=qfilter),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=k, query_filter=qfilter, with_payload=True, with_vectors=True)
            # RRF ранжирует, но его шкала несопоставима с косинусом, поэтому
            # плотную близость считаем отдельно — по ней принимается решение
            # «нашлось ли релевантное»
            qv = np.asarray(dense, dtype=np.float32)
            out = []
            for p in result.points:
                vec = (p.vector or {}).get("dense") if isinstance(p.vector, dict) else None
                cosine = float(np.dot(np.asarray(vec, dtype=np.float32), qv)) if vec else None
                hit = _hit(p.payload, p.score)
                hit.dense = cosine
                out.append(hit)
            return out

        result = self.client.query_points(
            self.collection, query=dense, using="dense", limit=k,
            query_filter=qfilter, with_payload=True)
        hits = [_hit(p.payload, p.score) for p in result.points]
        for h in hits:
            h.dense = h.score
        return hits

    def get(self, chunk_id: str) -> Hit | None:
        points = self.client.retrieve(self.collection, ids=[self._point_id(chunk_id)],
                                      with_payload=True)
        return _hit(points[0].payload, 1.0) if points else None

    def all_records(self) -> list[dict]:
        out, offset = [], None
        while True:
            batch, offset = self.client.scroll(self.collection, limit=256, offset=offset,
                                               with_payload=True)
            for p in batch:
                hit = _hit(p.payload, 1.0)
                out.append({"id": hit.id, "text": hit.text, "meta": hit.meta})
            if offset is None:
                break
        return out


# ==================================================================== общее
def _hit(payload: dict, score: float) -> Hit:
    payload = dict(payload or {})
    chunk_id = payload.pop("chunk_id", "")
    text = payload.pop("text", "")
    meta = {k: v for k, v in payload.items() if not k.startswith("_")}
    return Hit(id=chunk_id, score=float(score), text=text, meta=meta)


def _check_embedder(name: str, info: dict, embedder_name: str | None) -> None:
    built = info.get("embedder")
    if not (embedder_name and built) or built == embedder_name:
        return
    # Частный случай с другим лечением: индекс собран настоящим эмбеддером, а
    # поиск идёт запасным. Значит не «пересоберите индекс», а «поднимите
    # Ollama» — пересборка тут только испортит хороший индекс.
    if str(embedder_name).startswith("hashing") and not str(built).startswith("hashing"):
        raise StoreError(
            f"Индекс «{name}» собран эмбеддером {built}, а поиск откатился на "
            f"запасной {embedder_name}: значит эмбеддер сейчас недоступен. "
            f"Запустите Ollama (ollama serve) и повторите — ИНДЕКС ПЕРЕСОБИРАТЬ "
            f"НЕ НУЖНО. Если Ollama больше не будет, пересоберите индекс "
            f"запасным эмбеддером: python -m memory.build --fallback")
    raise StoreError(
        f"Индекс «{name}» собран эмбеддером {built}, а поиск идёт эмбеддером "
        f"{embedder_name}. Пересоберите индекс: python -m memory.build")


def resolve_location(cfg) -> str:
    q = cfg.settings["memory"].get("qdrant", {})
    url = str(q.get("url") or "").strip()
    if url:
        return url
    path = str(q.get("path") or "memory/store/qdrant").strip()
    if path == ":memory:":
        return path
    return str((cfg.root / path).resolve())


def open_store(cfg, name: str):
    mem = cfg.settings["memory"]
    path = cfg.root / mem["store_dir"]
    backend = str(mem.get("backend", "qdrant")).lower()
    if backend == "numpy":
        return NumpyVectorStore(path, name)
    if backend != "qdrant":
        raise StoreError(f"Неизвестный бэкенд памяти: {backend}. Доступны: qdrant, numpy")
    import os
    q = mem.get("qdrant", {})
    return QdrantVectorStore(
        path, name,
        location=resolve_location(cfg),
        prefix=str(q.get("collection_prefix", "sap_")),
        api_key=os.environ.get(str(q.get("api_key_env") or ""), "") or None,
        timeout=int(q.get("timeout_s", 30)),
        hybrid=bool(mem.get("hybrid", True)))


def reset_clients() -> None:
    """Закрывает клиентов Qdrant и отпускает ссылки.

    Встроенный режим держит файловую блокировку, а снимает её в `close()`, где
    внутри делается `import portalocker`. Если этот вызов достанется сборщику
    мусора на разрушении интерпретатора, импорт уже невозможен и Python печатает
    трейсбек «sys.meta_path is None» поверх успешно завершённой работы. Поэтому
    закрываем сами и заранее: обработчик atexit срабатывает, пока импорты ещё
    живы, а `gc.collect()` доводит уборку до конца там же.
    """
    for client in list(_CLIENTS.values()):
        try:
            client.close()
        except Exception:  # noqa: BLE001 — на выходе из программы молча
            pass
    _CLIENTS.clear()
    gc.collect()


atexit.register(reset_clients)
