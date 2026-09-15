# -*- coding: utf-8 -*-
"""
Проверка векторной памяти и графа связей — без обращения к моделям.

Индекс собирается запасным лексическим эмбеддером, поэтому тест воспроизводим и
не требует ни Ollama, ни ключей. Семантическое качество проверяется отдельно,
на замерах этапа Э5; здесь проверяется механика: границы фрагментов, координаты,
связи, фильтры и ограничения.

Запуск: python -m tests.test_memory   (из корня sap_agent)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Тесты гоняют Qdrant в оперативной памяти: изолированно и не оставляет следов.
# Тесты обязаны быть автономными. QDRANT_URL сбрасывается ЖЁСТКО, а не через
# setdefault: как только в .env появляется адрес сервера, он перебивает путь
# (resolve_location предпочитает url), и набор начинает требовать запущенный
# Docker. База в памяти ничего снаружи не ждёт и ничего не оставляет после себя.
os.environ["QDRANT_URL"] = ""
os.environ["QDRANT_PATH"] = ":memory:"
# Индексы тоже строятся в своём каталоге. Сборка памяти кладёт JSON, npz и bm25
# рядом с базой Qdrant, поэтому набор, собирающий индекс запасным эмбеддером,
# затирал рабочий: после прогона тестов агент искал лексической заглушкой вместо
# bge-m3 и молчал об этом. Набор обязан быть автономным в обе стороны — не
# зависеть от окружения и не портить его.
os.environ["SAP_AGENT_STORE_DIR"] = tempfile.mkdtemp(prefix="sap-agent-store-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import get_config                       # noqa: E402
from memory import build as memory_build                 # noqa: E402
from memory import index_code, index_regulations, links  # noqa: E402
from memory.search import reset_cache, semantic_search   # noqa: E402
from memory.sparse import BM25Encoder, stem, terms        # noqa: E402
from memory.vector_store import open_store                # noqa: E402
from observability.trace import Trace                    # noqa: E402
from tools.registry import execute                       # noqa: E402

FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if condition else 'СБОЙ'}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILED.append(name)


def main() -> int:
    cfg = get_config(reload=True)
    runs = Path(tempfile.mkdtemp(prefix="sap-agent-memory-"))
    traces: list[Trace] = []

    def new_trace(q: str) -> Trace:
        t = Trace(runs_dir=runs, question=q, task_file=cfg.stand.default_task, console=False)
        traces.append(t)
        return t

    current = new_trace("сборка памяти")

    def call(tool, **args):
        return execute(tool, args, cfg=cfg, trace=current)

    print("\nСборка индексов (бэкенд: %s)" % cfg.settings["memory"]["backend"])
    reset_cache()
    report = memory_build.build(cfg, force_fallback=True, quiet=True)
    check("индекс регламентов собран", report["регламенты"] > 90, str(report["регламенты"]))
    check("индекс кода собран", report["код"] > 50, str(report["код"]))
    check("эмбеддер зафиксирован в отчёте", report["эмбеддер"].startswith("hashing"))
    for name in ("regulations", "code"):
        store = open_store(cfg, name)
        check(f"хранилище «{name}» доступно", store.exists())
        check(f"в «{name}» записан эмбеддер", store.info["embedder"] == report["эмбеддер"])
    check("гибридный поиск включён в индексе", report["regulations_гибрид"] is True)

    print("\nРазреженные векторы BM25")
    check("окончания усекаются до общей основы",
          stem("приложению") == stem("приложение") == stem("приложения"),
          f'{stem("приложение")} / {stem("приложению")}')
    check("обозначения и числа не усекаются",
          terms("Флексо-4 ЛП2")[:1] == ["флексо-4"], str(terms("Флексо-4 ЛП2")))
    enc = BM25Encoder.load(cfg.root / cfg.settings["memory"]["store_dir"] / "regulations.bm25.json")
    check("статистика корпуса сохранена рядом с индексом", enc is not None and enc.documents > 90,
          str(enc.documents if enc else 0))
    if enc:
        qi, qv = enc.encode_query("минимальный объём блока")
        di, dv = enc.encode_document("Минимальные объёмы блоков устанавливать согласно приложению 5")
        check("термины запроса и документа пересекаются", bool(set(qi) & set(di)),
              str(len(set(qi) & set(di))))
        check("веса документа посчитаны по BM25, у запроса единичные",
              all(v == 1.0 for v in qv) and any(v != 1.0 for v in dv))

    print("\nQdrant: точки, фильтры, гибридная выдача")
    store = open_store(cfg, "regulations")
    check("бэкенд по умолчанию — Qdrant", store.backend == "qdrant", store.backend)
    check("строковый идентификатор фрагмента сохранён рядом с UUID-точкой",
          store.get("ТР-ЭКС п. 4.3") is not None)
    out = semantic_search(cfg, "regulations", "переход по калибру от большего к меньшему",
                          k=4, where={"к_планированию": True})
    check("выдача объединена по RRF", out.hybrid is True)
    check("плотная близость посчитана отдельно от RRF",
          all(h.dense is not None and h.dense != h.score for h in out.hits[:1]),
          f"rrf={out.hits[0].score:.3f} плотн={out.hits[0].dense:.3f}" if out.hits else "")
    check("п. 4.3 найден", any(h.meta["пункт"] == "4.3" for h in out.hits),
          str([h.meta["пункт"] for h in out.hits]))
    check("фильтр по payload отсёк охрану труда",
          all(h.meta["к_планированию"] for h in out.hits))
    out2 = semantic_search(cfg, "regulations", "отбор оборудования", k=5,
                           where={"этап": "кольцевание"})
    check("фильтр по этапу работает",
          bool(out2.hits) and all(h.meta["этап"] == "кольцевание" for h in out2.hits),
          str({h.meta["этап"] for h in out2.hits}))

    print("\nЧанкинг регламентов: единица — нумерованный пункт")
    reg, appendices = index_regulations.collect(cfg)
    ids = {c["id"] for c in reg}
    check("три регламента разобраны", len(appendices) == 3, str(sorted(appendices)))
    check("пункт ТР-ЭКС 4.3 выделен как отдельный фрагмент", "ТР-ЭКС п. 4.3" in ids)
    c43 = next(c for c in reg if c["id"] == "ТР-ЭКС п. 4.3")
    check("координата пригодна для цитирования",
          c43["meta"]["координата"] == "ТР-ЭКС-2026/01 п. 4.3", c43["meta"]["координата"])
    check("раздел пункта сохранён", "Переходы" in c43["meta"]["раздел"])
    check("ссылка «приложение 5» разрешена в табл. 12",
          c43["meta"]["таблицы_НСИ"] == ["12"], str(c43["meta"]["таблицы_НСИ"]))
    check("карта приложений ТР-ЭКС верна",
          appendices["ТР-ЭКС"]["7"] == "27" and appendices["ТР-ЭКС"]["5"] == "12")
    safety = [c for c in reg if not c["meta"]["к_планированию"]]
    check("пункты охраны труда помечены как не относящиеся к планированию",
          len(safety) > 20 and all("охран" in c["meta"]["раздел"].lower()
                                   or "уборк" in c["meta"]["раздел"].lower()
                                   or "заключительн" in c["meta"]["раздел"].lower()
                                   for c in safety), str(len(safety)))

    print("\nЧанкинг кода: единица — функция, границы из AST")
    code = index_code.collect(cfg)
    by_id = {c["id"]: c for c in code}
    pg = by_id.get("demo/extrusion.py:ExtrusionStage._place_group")
    check("метод _place_group выделен целиком", pg is not None)
    if pg:
        start, end = (int(x) for x in pg["meta"]["строки"].split("-"))
        real = (cfg.stand.root / "demo/extrusion.py").read_text(encoding="utf-8").splitlines()
        check("фрагмент начинается с заголовка функции",
              real[start - 1].strip().startswith("def _place_group"), real[start - 1].strip()[:40])
        check("фрагмент кончается последней строкой тела",
              end <= len(real) and real[end - 1].strip() != "", str(end))
        check("координата содержит файл, имя и строки",
              pg["meta"]["координата"] == f"demo/extrusion.py:ExtrusionStage._place_group:{start}-{end}")
    gel = by_id.get("demo/input_data.py:InputData.get_eligible_lines")
    check("обращения к таблицам НСИ распознаны в коде",
          gel is not None and gel["meta"]["таблицы_НСИ"] == ["1", "27"],
          str(gel["meta"]["таблицы_НСИ"]) if gel else "")
    check("генератор синтетики помечен как не относящийся к планированию",
          all(not c["meta"]["к_планированию"] for c in code if "synth_data" in c["meta"]["файл"]))

    print("\nЧто НЕ попало в векторное хранилище")
    sources = {c["meta"]["источник"] for c in reg + code}
    check("векторизованы только регламенты и код", sources == {"regulations", "code"},
          str(sorted(sources)))
    check("нормативные таблицы в индекс не попали",
          not any("params" in str(c["meta"].get("файл", "")) for c in code))
    check("результат расчёта в индекс не попал",
          not any("calculation_results" in str(c["meta"].get("файл", "")) for c in code))

    print("\nГраф связей: пункт ↔ таблица НСИ ↔ функция")
    graph = links.LinkGraph(cfg.root / cfg.settings["memory"]["store_dir"] / "links.json")
    st = graph.stats()
    check("граф содержит все пункты и функции",
          st["пунктов"] == len(reg) and st["функций"] == len(code))
    check("не меньше 15 таблиц связаны с двух сторон",
          len(st["связанных_таблиц"]) >= 15, str(len(st["связанных_таблиц"])))
    check("ссылка на пункт распознаётся в трёх формах",
          graph.resolve("ТР-ЭКС п. 4.3") == graph.resolve("ТР-ЭКС-2026/01 п. 4.3")
          == graph.resolve("ТР-ЭКС 4.3") == "ТР-ЭКС п. 4.3")
    check("пункт про отбор линий ведёт к табл. 27 и 1",
          set(graph.tables_of_clause("ТР-ЭКС п. 5.1")) >= {"1", "27"},
          str(graph.tables_of_clause("ТР-ЭКС п. 5.1")))
    check("пункт про гомоглифы наследует табл. 29 из раздела",
          "29" in graph.tables_of_clause("ТР-ПЕЧ п. 5.3"),
          str(graph.tables_of_clause("ТР-ПЕЧ п. 5.3")))
    fns = graph.functions_of_table("27")
    check("функции сортируются от конкретных к общим",
          len(fns[0]["таблицы"]) <= len(fns[-1]["таблицы"]),
          f'{fns[0]["полное_имя"]} ({len(fns[0]["таблицы"])}) … '
          f'{fns[-1]["полное_имя"]} ({len(fns[-1]["таблицы"])})')
    back = {c["пункт_id"] for c in graph.clauses_for_code(
        "demo/input_data.py:InputData.get_eligible_lines")}
    check("обратный переход от функции к норме работает",
          "ТР-ЭКС п. 5.1" in back, str(sorted(back)[:3]))

    print("\nsearch_regulations")
    current = new_trace("поиск по регламентам")
    r = call("search_regulations", table="18")
    items = (r.payload or {}).get("пункты", [])
    check("режим по таблице НСИ — структурный", (r.payload or {}).get("режим", "").startswith("по таблице"))
    check("табл. 18 приводит к п. 3.1 ТР-ПЕЧ",
          any(i["координата"] == "ТР-ПЕЧ-2026/02 п. 3.1" for i in items),
          str([i["пункт"] for i in items]))
    r = call("search_regulations", query="переход по калибру от большего к меньшему")
    top = (r.payload or {}).get("пункты", [{}])[0]
    check("семантический поиск находит п. 4.3 первым",
          top.get("координата") == "ТР-ЭКС-2026/01 п. 4.3", str(top.get("координата")))
    check("выдача помечена как недоверенный контент",
          (r.payload or {}).get("недоверенный_контент") is True)
    check("лексический режим честно объявлен", "предупреждение" in (r.payload or {}))
    check("охрана труда в выдачу не попала",
          all("охран" not in i["раздел"].lower() for i in (r.payload or {})["пункты"]))

    print("\nsearch_code")
    current = new_trace("поиск по коду")
    r = call("search_code")
    check("вызов без единого критерия отбит", r.status == "invalid_args", r.error or "")
    current = new_trace("поиск по коду: граф")
    r = call("search_code", clause="ТР-КОЛ п. 5.1")
    frags = (r.payload or {}).get("фрагменты", [])
    check("переход от пункта к коду по графу работает",
          (r.payload or {}).get("режим", "").startswith("по пункту"))
    check("первым идёт код этапа кольцевания",
          frags and "ringing.py" in frags[0]["файл"], frags[0]["файл"] if frags else "")
    check("указано, через какую таблицу установлена связь",
          frags and frags[0].get("через_таблицу") == "20")
    r = call("search_code", symbol="_place_group")
    check("поиск по имени функции точен",
          (r.payload or {})["найдено"] == 1
          and (r.payload or {})["фрагменты"][0]["полное_имя"] == "ExtrusionStage._place_group")
    check("код помечен как недоверенный контент и без предложений правок",
          (r.payload or {}).get("недоверенный_контент") is True
          and "не предлагает изменений" in (r.payload or {}).get("примечание", ""))

    print("\nУправление retrieval и ограничения")
    current = new_trace("лимит обращений к памяти")
    rlimit = int(cfg.limits["max_retrieval_queries"])
    statuses = [call("search_regulations", query=f"норма номер {i}").status
                for i in range(rlimit + 2)]
    check(f"после {rlimit} обращений к памяти поиск закрывается — считаются и неудачные",
          statuses[rlimit] == "error" and statuses[-1] == "error", str(statuses))
    check("счётчик обращений к памяти учтён в трассе",
          current.retrieval_queries >= rlimit, str(current.retrieval_queries))
    r = call("read_plan", order_number="Z-1060")
    check("лимит памяти не блокирует обычные инструменты", r.ok)

    print("\nЗащита от несовпадения эмбеддеров")
    store = open_store(cfg, "regulations")
    import numpy as np
    try:
        store.search(np.zeros(store.info["dim"], dtype=np.float32), embedder_name="другой-эмбеддер")
        check("индекс отказывается искать чужим эмбеддером", False)
    except Exception as exc:  # noqa: BLE001
        check("индекс отказывается искать чужим эмбеддером",
              "Пересоберите" in str(exc), type(exc).__name__)

    print("\nЗапасной бэкенд numpy")
    os.environ["SAP_AGENT_MEMORY_BACKEND"] = "numpy"
    cfg_np = get_config(reload=True)
    reset_cache()
    rep = memory_build.build(cfg_np, force_fallback=True, quiet=True)
    np_store = open_store(cfg_np, "regulations")
    check("numpy-хранилище собирается тем же кодом", np_store.backend == "numpy" and np_store.exists())
    check("гибридного поиска в запасном бэкенде нет — и он об этом честно сообщает",
          rep["regulations_гибрид"] is False and np_store.info["hybrid"] is False)
    out = semantic_search(cfg_np, "regulations", "переход по калибру от большего к меньшему",
                          k=3, where={"к_планированию": True})
    check("плотный поиск в numpy находит тот же пункт",
          any(h.meta["пункт"] == "4.3" for h in out.hits),
          str([h.meta["пункт"] for h in out.hits]))
    check("в плотном режиме RRF не применяется", out.hybrid is False)
    os.environ["SAP_AGENT_MEMORY_BACKEND"] = "qdrant"
    get_config(reload=True)
    reset_cache()

    print("\nНабор не портит рабочий индекс")
    # Сборка памяти кладёт JSON, npz и bm25 в store_dir. Пока каталог был общим,
    # прогон тестов оставлял рабочий индекс собранным запасным эмбеддером —
    # и агент после этого искал лексической заглушкой, не сообщая об этом.
    from pathlib import Path as _P                               # noqa: E402
    store = _P(get_config().root) / get_config().settings["memory"]["store_dir"]
    check("индекс тестов лежит вне рабочего каталога",
          "sap-agent-store-" in str(store), str(store))
    check("рабочий каталог проекта не тронут",
          not str(store).endswith(str(_P("memory") / "store")), str(store))

    print()
    if FAILED:
        print(f"Провалено проверок: {len(FAILED)}")
        for f in FAILED:
            print(f"  · {f}")
        return 1
    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
