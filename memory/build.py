# -*- coding: utf-8 -*-
"""
Сборка векторных индексов и графа связей.

    python -m memory.build                 # собрать всё
    python -m memory.build --fallback      # принудительно лексический эмбеддер
    python -m memory.build --stats         # что сейчас лежит в индексах

Индексируются только тексты — регламенты и код. Нормативные таблицы и результат
расчёта в векторное хранилище НЕ попадают сознательно: это структурированные
данные, к ним точечный доступ инструментами. Векторизовать таблицу нормативов
значило бы разрешить модели вспоминать числа приблизительно, а для агента, чья
ценность в доказательности, это худшее из возможных решений.
"""
from __future__ import annotations

import json

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import get_config                                  # noqa: E402
from memory import index_code, index_regulations, links, search      # noqa: E402
from memory.embeddings import get_embedder                           # noqa: E402
from memory.sparse import BM25Encoder                                 # noqa: E402
from memory.vector_store import open_store, reset_clients             # noqa: E402


def build(cfg, *, force_fallback: bool = False, quiet: bool = False) -> dict:
    client = None
    if not force_fallback:
        try:
            from llm.client import LLMClient
            client = LLMClient(cfg)
        except Exception:  # noqa: BLE001 — без моделей индекс всё равно должен собраться
            client = None

    embedder, reason = get_embedder(cfg, client, force_fallback=force_fallback)
    if not quiet:
        # Куда пишем и каким эмбеддером — до начала работы, а не после падения.
        # Прошлый раз сборка упала на блокировке каталога, и по выводу нельзя
        # было понять, что это встроенный режим, то есть QDRANT_URL пуст.
        backend = str(cfg.settings["memory"].get("backend", "qdrant")).lower()
        if backend == "qdrant":
            from memory.vector_store import resolve_location
            where = resolve_location(cfg)
            mode = ("сервер" if where.startswith(("http://", "https://"))
                    else "встроенный режим (каталог занимается монопольно)")
            print(f"  хранилище: {mode} — {where}")
        else:
            print(f"  хранилище: бэкенд {backend} — "
                  f"{cfg.root / cfg.settings['memory']['store_dir']}")
        print(f"  эмбеддер: {embedder.name}")
    if reason and not quiet:
        print(f"  ! эмбеддер {embedder.name}: {reason}")
        print("    индекс соберётся, но поиск будет лексическим, а не семантическим")

    hybrid = bool(cfg.settings["memory"].get("hybrid", True))
    report = {"эмбеддер": embedder.name, "откат": reason, "бэкенд": cfg.settings["memory"]["backend"]}

    def progress(done: int, total: int) -> None:
        if not quiet and total > 8:
            print(f"\r    эмбеддинги: {done}/{total}", end="", flush=True)
            if done >= total:
                print()

    if hasattr(embedder, "progress"):
        embedder.progress = progress

    def index(name: str, chunks: list[dict], dense_texts: list[str]) -> None:
        """Индексирует корпус. `dense_texts` — сокращённое представление ДЛЯ
        ПЛОТНОГО ВЕКТОРА: длинная функция иначе перевешивает короткую объёмом.

        Хранится и лексически индексируется при этом ПОЛНЫЙ текст фрагмента.
        Раньше сокращённое представление шло во все три места сразу, и это был
        тихий, но тяжёлый дефект: тело функции обрезалось на 1500 символах
        посреди выражения, а `search_code` отдавал модели именно его. Например
        `ExtrusionStage._transition_waste` — 2318 символов, и в обрезке не
        оставалось ни работы с эксклюзивностью, ни финального `return`. Вопрос
        «учитывается ли эксклюзивность» получал ответ по фрагменту, в котором
        её физически нет, и признака обрыва модель не видела. По той же причине
        лексический поиск не находил термины из хвоста длинных функций.
        """
        full_texts = [c["text"] for c in chunks]
        sparse = None
        if hybrid:
            # BM25 обучается на том же корпусе, что индексируется: статистика
            # терминов нужна и при кодировании запроса, поэтому кладётся рядом
            encoder = BM25Encoder().fit(full_texts)
            encoder.save(search.bm25_path(cfg, name))
            sparse = [encoder.encode_document(t) for t in full_texts]
        store = open_store(cfg, name)
        store.build([c["id"] for c in chunks], full_texts, [c["meta"] for c in chunks],
                    embedder.encode(dense_texts), embedder.name, sparse=sparse)
        report[name] = len(chunks)
        report[f"{name}_гибрид"] = bool(store.info.get("hybrid"))

    reg_chunks, appendices = index_regulations.collect(cfg)
    # Названия таблиц НСИ живут в приложениях регламентов — единственном месте
    # стенда, где они вообще есть. Собираем их рядом с индексами, чтобы
    # инструменты называли таблицы словами стенда, а не словами автора агента.
    titles = index_regulations.table_titles(cfg)
    store_dir = cfg.root / cfg.settings["memory"]["store_dir"]
    store_dir.mkdir(parents=True, exist_ok=True)
    with open(store_dir / "tables.json", "w", encoding="utf-8") as fh:
        json.dump(titles, fh, ensure_ascii=False, indent=2)
    report["названия таблиц"] = len(titles)
    if not quiet:
        print(f"  регламенты: {len(reg_chunks)} пунктов из {len(appendices)} документов")
    index("regulations", reg_chunks, [index_regulations.embedding_text(c) for c in reg_chunks])
    report["регламенты"] = len(reg_chunks)

    code_chunks = index_code.collect(cfg)
    if not quiet:
        print(f"  код: {len(code_chunks)} фрагментов")
    index("code", code_chunks, [index_code.embedding_text(c) for c in code_chunks])
    report["код"] = len(code_chunks)

    graph = links.build(reg_chunks, code_chunks)
    graph_path = cfg.root / cfg.settings["memory"]["store_dir"] / "links.json"
    links.save(graph, graph_path)
    stats = links.LinkGraph(graph_path).stats()
    report["граф"] = stats
    if not quiet:
        print(f"  граф связей: {stats['пунктов']} пунктов, {stats['функций']} функций, "
              f"таблиц с двусторонней связью: {len(stats['связанных_таблиц'])} "
              f"({', '.join(stats['связанных_таблиц'])})")
    if not quiet:
        mode = "гибридный (плотный + BM25)" if report.get("regulations_гибрид") else "только плотный"
        print(f"  бэкенд: {report['бэкенд']}, поиск: {mode}")
    search.reset_cache()
    return report


def stats(cfg) -> int:  # noqa: D401
    from memory.vector_store import resolve_location
    store_dir = cfg.root / cfg.settings["memory"]["store_dir"]
    where = (resolve_location(cfg) if cfg.settings["memory"]["backend"] == "qdrant"
             else str(store_dir))
    print(f"Бэкенд: {cfg.settings['memory']['backend']}   хранилище: {where}")
    for name in ("regulations", "code"):
        store = open_store(cfg, name)
        if not store.exists():
            print(f"  {name}: индекс не собран")
            continue
        info = store.info
        print(f"  {name}: {info['count']} фрагментов, эмбеддер {info['embedder']}, "
              f"размерность {info['dim']}, "
              f"поиск {'гибридный' if info.get('hybrid') else 'плотный'}")
    path = store_dir / "links.json"
    if path.exists():
        print(f"  граф связей: {links.LinkGraph(path).stats()}")
    else:
        print("  граф связей: не собран")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="memory.build", description="Сборка векторной памяти агента")
    p.add_argument("--fallback", action="store_true",
                   help="Принудительно использовать лексический эмбеддер (без Ollama)")
    p.add_argument("--stats", action="store_true", help="Показать состояние индексов и выйти")
    p.add_argument("--backend", choices=["qdrant", "numpy"],
                   help="Переопределить бэкенд памяти на этот запуск")
    args = p.parse_args(argv)

    if args.backend:
        import os
        os.environ["SAP_AGENT_MEMORY_BACKEND"] = args.backend
    cfg = get_config(reload=True)
    if args.stats:
        return stats(cfg)
    print(f"Сборка индексов из стенда {cfg.stand.root.name}")
    build(cfg, force_fallback=args.fallback)
    reset_clients()
    print("Готово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
