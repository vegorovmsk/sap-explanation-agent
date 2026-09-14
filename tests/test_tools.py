# -*- coding: utf-8 -*-
"""
Проверка инструментов чтения на реальном стенде — без обращения к моделям.

Каждый кейс здесь — из золотого набора: то, что агент должен уметь доказать.

Запуск: python -m tests.test_tools   (из корня sap_agent)
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import get_config                       # noqa: E402
from observability.trace import Trace                    # noqa: E402
from tools import registry                               # noqa: E402
from tools.registry import ToolLimitExceeded, execute    # noqa: E402

FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if condition else 'СБОЙ'}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILED.append(name)


def main() -> int:
    cfg = get_config()
    runs = Path(tempfile.mkdtemp(prefix="sap-agent-tools-"))
    # лимит вызовов инструментов считается на один вопрос, поэтому каждая группа
    # проверок — это отдельный прогон со своей трассой
    traces: list[Trace] = []

    def new_trace(question: str) -> Trace:
        t = Trace(runs_dir=runs, question=question, task_file=cfg.stand.default_task,
                  console=False)
        traces.append(t)
        return t

    current = new_trace("проверка инструментов")

    def call(tool, **args):
        return execute(tool, args, cfg=cfg, trace=current)

    print("\nНомер приложения — не номер таблицы")
    # Прогон 15.09, кейс B1: регламент говорит «с учётом приложения 5», агент
    # прочитал это как «табл. 5», трижды спросил несуществующую таблицу и вывел
    # «таблицы 5 в системе нет — значит, нет и нормативной группировки калибров».
    # Ложный вывод, построенный на собственной ошибке адресации.
    r_app = call("lookup_nsi", table="5")
    check("несуществующий номер не выдаётся за отсутствие норматива",
          r_app.status == "invalid_args", r_app.status)
    check("сказано, какие таблицы лежат в приложении 5",
          "табл. 12" in (r_app.error or "") and "табл. 29" in (r_app.error or ""),
          str(r_app.error)[:110])
    check("названы оба документа — приложения нумеруются внутри каждого",
          "ТР-ЭКС" in (r_app.error or "") and "ТР-ПЕЧ" in (r_app.error or ""),
          str(r_app.error)[:110])
    r_none = call("lookup_nsi", table="99")
    check("выдуманный номер остаётся просто ошибкой",
          "Неизвестная таблица" in (r_none.error or ""), str(r_none.error)[:70])

    print("\nРазделы конфига стенда берутся из файла, а не из списка в коде")
    # Здесь лежал белый список из девяти разделов с моими пояснениями («служебные
    # годы готовности»), и эти пояснения уходили модели как «о чём». Агент
    # показывал МОИ формулировки вместо того, что система сказала о себе; раздел,
    # которого я не предусмотрел, для агента не существовал.
    cat = call("read_stand_config")
    sections = cat.payload["разделы"]
    names = [x["раздел"] for x in sections]
    check("разделов больше, чем было в рукописном списке", len(names) >= 10,
          f"{len(names)}: {names[:4]}…")
    check("видны и те, что в список бы не попали",
          "output_file" in names or "data_folder" in names, str(names))
    said = {x["раздел"]: x["о чём"] for x in sections}
    check("объяснение — комментарий самой системы",
          "2070" in said.get("service_years", ""), said.get("service_years", "")[:70])
    check("где система промолчала, агент не придумывает за неё",
          said.get("column_translation", "") == "",
          repr(said.get("column_translation")))

    print("\nОпечатка в значении из закрытого списка")
    # Живой прогон 14.09, вопрос «какие этапы планирования есть в системе»: модель
    # поступила правильно — опросила регламенты всех трёх этапов по очереди, — но
    # написала «эструзия». Схема отвергла значение, экструзия выпала из
    # доказательной базы целиком, и ответ перечислил три этапа, прочитав нормы
    # только двух. Одна пропущенная буква стоила источника.
    from tools.registry import _nearest_enum                     # noqa: E402
    STAGES = ["экструзия", "печать", "кольцевание"]
    check("пропущенная буква исправляется",
          _nearest_enum(STAGES, "эструзия") == "экструзия")
    check("регистр и падеж тоже",
          _nearest_enum(STAGES, "ЭКСТРУЗИЯ") == "экструзия"
          and _nearest_enum(STAGES, "экструзии") == "экструзия")
    check("верное значение не трогаем",
          _nearest_enum(STAGES, "кольцевание") is None)
    check("далёкое слово остаётся ошибкой — гадать нельзя",
          _nearest_enum(STAGES, "обжиг") is None
          and _nearest_enum(STAGES, "") is None)
    check("двусмысленность остаётся ошибкой",
          _nearest_enum(["альфа", "альфб"], "альфв") is None)

    # Режим «только этап» детерминированный и не поднимает векторную память:
    # проверяется исправление аргумента, а не поиск.
    r_fix = call("search_regulations", stage="эструзия")
    check("вызов с опечаткой доходит до источника", r_fix.status == "ok", r_fix.status)
    written = [json.loads(x) for x in
               current.path.read_text(encoding="utf-8").splitlines()]
    check("исправление видно в трассе, а не сделано молча",
          any(e.get("kind") == "tool.args_repaired" for e in written),
          "нужно событие tool.args_repaired")

    print("\nread_task")
    r = call("read_task", order_number="Z-1060")
    p = r.payload or {}
    check("заказ найден", r.ok, r.error or "")
    check("параметры продукции прочитаны",
          (p.get("kind"), p.get("sort"), p.get("caliber"), p.get("color"))
          == ("Демолон", "Дк", 60, "Лимонный"), str(p.get("kind")))
    check("маршрут разобран по услугам", p.get("services_list") == ["Экструзия", "Печать"])
    check("вид печати и эксклюзивность на месте",
          p.get("print_type") == "Флексо-4" and p.get("exclusivity") == "Премиум")
    check("координата источника проставлена", "строка" in (p.get("_locator") or ""),
          p.get("_locator", ""))

    r = call("read_task", order_number="Z-9999")
    check("несуществующий заказ даёт not_found", r.status == "not_found")
    check("к not_found приложена подсказка", bool(r.hint))
    r = call("read_task")
    check("вызов без обязательного аргумента отбит схемой", r.status == "invalid_args", r.error or "")

    print("\nread_plan — ключевой кейс Z-1060")
    current = new_trace("read_plan: Z-1060")
    r = call("read_plan", order_number="Z-1060")
    p = r.payload or {}
    parts = p.get("партии", [])
    check("две партии: экструзия и печать", len(parts) == 2, str(len(parts)))
    check("партии идут в порядке маршрута",
          [x["Этап"] for x in parts] == ["экструзия", "печать"])
    pr = next((x for x in parts if x["Этап"] == "печать"), {})
    check("печать назначена на ЛП1", pr.get("Линия") == "ЛП1", str(pr.get("Линия")))
    check("допустимые линии печати — ЛП1 и ЛП3, ЛП2 отсутствует",
          pr.get("ДопустимыеЛинии") == "ЛП1;ЛП3", str(pr.get("ДопустимыеЛинии")))
    check("готовых вердиктов в партии нет",
          not any("не выбрана" in str(v) or "недоступен" in str(v) for v in pr.values()))
    check("соседи по очереди посчитаны", "соседи_по_очереди" in pr)
    check("у каждой партии своя координата",
          all("строка" in x.get("_locator", "") for x in parts))

    print("\nread_plan — заказы, не попавшие в расписание")
    current = new_trace("read_plan: неразмещённые")
    r = call("read_plan", order_number="Z-1030")
    p = r.payload or {}
    check("исключённый заказ остаётся в результате", r.ok)
    check("линия служебная — «Отложенные»",
          p["партии"][0]["Линия"] == "Отложенные", str(p["партии"][0]["Линия"]))
    check("признак «не размещён» выставлен", p.get("размещён") is False)
    check("причина исключения прочитана",
          p["партии"][0]["Причина"] == "ассортимент не обрабатывается")

    r = call("read_plan", order_number="Z-1010")
    p = r.payload or {}
    part = p["партии"][0]
    check("заказ без блока — линия «Без блока»", part["Линия"] == "Без блока")
    check("служебный год 2070 проставлен", part["ГодГотовности"] == 2070)
    check("объём цветового блока меньше норматива",
          part["ОбъёмЦветовогоБлока"] < part["МинЦветовойБлок"],
          f'{part["ОбъёмЦветовогоБлока"]} < {part["МинЦветовойБлок"]}')

    r = call("read_plan", order_number="Z-1020")
    check("снятый по НСИ заказ отдаёт замечания НСИ",
          bool((r.payload or {}).get("замечания_НСИ")))
    check("в замечании указана таблица 8",
          str((r.payload or {})["замечания_НСИ"][0].get("Таблица")) == "8")

    print("\nread_plan — очередь на линии")
    current = new_trace("read_plan: очередь")
    r = call("read_plan", line="ЛП2", stage="печать", limit=5)
    p = r.payload or {}
    check("очередь на ЛП2 получена", r.ok and p.get("всего_партий", 0) > 0,
          str(p.get("всего_партий")))
    check("очередь отсортирована по времени начала",
          [x["ДатаНачалаПП"] for x in p["очередь"]]
          == sorted(x["ДатаНачалаПП"] for x in p["очередь"]))
    r = call("read_plan")
    check("вызов без заказа и линии отбит", r.status == "invalid_args")

    print("\nlookup_nsi — норматив как доказательство")
    current = new_trace("lookup_nsi: нормативы")
    r = call("lookup_nsi", table="18", filters={"Вид печати": "Флексо-4"})
    lines = {x["Единица оборудования"] for x in (r.payload or {}).get("строки", [])}
    check("Флексо-4 допускается на ЛП1 и ЛП3", lines == {"ЛП1", "ЛП3"}, str(sorted(lines)))
    check("ЛП2 в допустимые не входит", "ЛП2" not in lines)
    check("источник назван по номеру таблицы", "табл. 18" in r.source, r.source)

    r = call("lookup_nsi", table="12", filters={"Вид": "Синтекс", "Тип": "Ск"})
    row = (r.payload or {}).get("строки", [{}])[0]
    check("минимальный цветовой блок Синтекс Ск равен 20 км",
          row.get("Блок 2, км") == 20, str(row.get("Блок 2, км")))

    r = call("lookup_nsi", table="27", caliber=60,
             filters={"Линия": "ЛЭ3", "Вид": "Демолон", "Тип": "Дк"})
    check("ЛЭ3 не производит Демолон Дк калибра 60 — строк нет",
          r.status == "not_found", str(r.status))
    check("отсутствие норматива объяснено как доказательство", "доказательство" in (r.hint or ""))

    print("\nlookup_nsi — диагностика ключа (кейс Z-1061)")
    current = new_trace("lookup_nsi: диагностика ключа")
    r = call("lookup_nsi", table="29", filters={"Вид": "Синтекс", "Тип": "Ск"})
    diag = (r.payload or {}).get("диагностика_ключа", [])
    check("норматив срока хранения не найден", r.status == "not_found")
    twin = next((d for d in diag if "различие" in d), None)
    check("найден визуально совпадающий кандидат", twin is not None)
    if twin:
        diff = twin["различие"]
        check("кандидат признан визуально идентичным", diff["визуально совпадают"] is True)
        first = diff["различия"][0]
        check("различие локализовано в первом символе", first["позиция"] == 1, str(first["позиция"]))
        check("в запросе кириллическая С", "CYRILLIC" in first["в запросе"], first["в запросе"])
        check("в таблице латинская C", "LATIN" in first["в таблице"], first["в таблице"])
        check("подсказка ведёт к обращению в поддержку", "поддержку" in (r.hint or ""))

    r = call("lookup_nsi", table="18", filters={"Единица оборудования": "ЛП2"},
             columns=["Единица оборудования", "ДопустимыеЛинии"])
    rows = (r.payload or {}).get("строки", [])
    check("несуществующая колонка в проекции не роняет верный запрос", r.ok, r.error or "")
    check("выдача не урезана по ошибочному списку колонок",
          bool(rows) and "Вид печати" in rows[0], str(list(rows[0]))[:80] if rows else "")
    check("ЛП2 допускает Флексо-2 и Цифровую, но не Флексо-4",
          {x["Вид печати"] for x in rows} == {"Флексо-2", "Цифровая"},
          str({x["Вид печати"] for x in rows}))
    check("пропущенная колонка названа, и сказано, где она живёт",
          "ДопустимыеЛинии" in (r.payload or {}).get("пропущенные_колонки", [])
          and "read_plan" in (r.payload or {}).get("примечание_колонок", ""))
    r = call("lookup_nsi", table="18", filters={"Линия": "ЛП2"})
    check("ошибка в фильтре по-прежнему строго отбивается", r.status == "invalid_args")
    check("подсказка объясняет, что это колонка плана", "read_plan" in (r.hint or ""))

    current = new_trace("lookup_nsi: каталог и таблицы")
    r = call("lookup_nsi", table="99")
    check("несуществующая таблица отбита", r.status == "invalid_args")
    check("в подсказке перечислены доступные таблицы", "27" in (r.hint or ""))
    r = call("list_nsi_tables")
    check("каталог НСИ содержит 16 таблиц", len((r.payload or {}).get("таблицы", [])) == 16)

    print("\nread_logs")
    current = new_trace("read_logs")
    r = call("read_logs", order_number="Z-1010")
    check("события по заказу найдены", r.ok, r.error or "")
    check("событие распознано как «НЕ РАЗМЕЩЁН»",
          "НЕ РАЗМЕЩЁН" in (r.payload or {}).get("события", []),
          str((r.payload or {}).get("события")))
    r = call("read_logs", order_number="Z-1020", pattern="СНЯТ ПО НСИ")
    check("поиск по заказу и шаблону вместе работает", r.ok, r.error or "")
    r = call("read_logs", order_number="A-9999")
    check("пустой результат подан как отсутствие подтверждения",
          r.status == "not_found" and "тоже факт" in (r.hint or ""))

    print("\nНазвания таблиц НСИ — из регламентов, а не из кода агента")
    from tools.nsi_lookup import catalogue                       # noqa: E402
    cat = catalogue(cfg)
    check("каталог собран из конфига стенда", len(cat) >= 16, str(len(cat)))
    check("название таблицы 18 взято у регламента",
          cat["18"]["title"] == "Печатное оборудование (допустимость линий)",
          cat["18"]["title"])
    check("и у названия есть координата",
          "приложение" in cat["18"]["источник_названия"], cat["18"]["источник_названия"])
    check("таблица без приложения не получает выдуманного названия",
          cat["23"]["title"] == cat["23"]["slug"] and not cat["23"]["источник_названия"],
          f"{cat['23']['title']} / {cat['23']['источник_названия']!r}")
    import tools.nsi_lookup as _nsi
    check("словаря названий в коде инструмента больше нет",
          not hasattr(_nsi, "TITLES"))

    print("\nНормы этапа: выборка, а не поиск")
    current = new_trace("нормы этапа")
    before = current.retrieval_queries
    r = call("search_regulations", stage="экструзия")
    p = r.payload or {}
    check("все нормы этапа возвращены списком", r.ok and p.get("найдено") == 29,
          f"{r.status}: {p.get('найдено')}")
    check("режим назван выборкой по индексу", "фильтр по индексу" in p.get("режим", ""),
          p.get("режим", ""))
    check("квота векторной памяти не потрачена",
          current.retrieval_queries == before, str(current.retrieval_queries))
    check("у каждого пункта есть координата",
          all(c.get("координата") for c in p.get("пункты", [])))
    r2 = call("search_regulations", table="18")
    check("переход по графу связей тоже не тратит квоту",
          current.retrieval_queries == before and r2.ok, str(current.retrieval_queries))
    # Этап в схеме перечислением, поэтому несуществующее значение отсекается
    # проверкой аргументов — модель сразу видит допустимый список.
    r3 = call("search_regulations", stage="выдумка")
    check("несуществующий этап отвергнут по схеме", r3.status == "invalid_args", r3.status)
    check("в ошибке названы допустимые значения",
          "экструзия" in (r3.error or "") + (r3.hint or ""), (r3.error or "")[:80])

    print("\nНезаполненные аргументы при function calling")
    # Модели заполняют все поля схемы, подставляя null в неиспользуемые. Живой
    # прогон потерял на этом целый кейс: search_code отвергался пять раз подряд,
    # код не был прочитан ни разу, и сверка объявила расхождение на пустом месте.
    current = new_trace("пустые аргументы")
    r = call("lookup_nsi", table="18", filters={"Единица оборудования": "ЛП1"},
             caliber=None, columns=None, limit=None)
    check("lookup_nsi не спотыкается о null в необязательных полях",
          r.status != "invalid_args", f"{r.status}: {r.error or ''}")
    r = call("search_code", symbol="ExtrusionStage.run", query=None, clause=None,
             table=None, top_k=None, include_generators=None)
    check("search_code не спотыкается о null в необязательных полях",
          r.status != "invalid_args", f"{r.status}: {r.error or ''}")
    check("обязательный аргумент по-прежнему обязателен",
          call("read_task", order_number=None).status == "invalid_args")
    # Модели путают «1» и 1. Для номера таблицы разницы нет, а вызов отвергался.
    r = call("lookup_nsi", table=1, filters={"Оборудование": "ЛЭ2",
                                             "Вид оболочки": "Демолон",
                                             "Тип оболочки": "Дк"}, caliber="60")
    check("номер таблицы числом принимается", r.ok, f"{r.status}: {r.error or ''}")
    rows = (r.payload or {}).get("строки") or []
    check("и калибр строкой тоже",
          bool(rows) and rows[0].get("Нормативная производительность, км/час") == 6.0,
          str(rows[:1])[:80])

    before = current.retrieval_queries
    call("search_code", query=None, clause=None, table=None, symbol=None)
    check("отвергнутый по схеме вызов не тратит квоту векторной памяти",
          current.retrieval_queries == before, str(current.retrieval_queries))

    print("\nРеестр и ограничения")
    current = new_trace("реестр и ограничения")
    r = call("несуществующий_инструмент")
    check("незарегистрированный инструмент не вызывается", r.status == "invalid_args")
    check("режим только для чтения: пишущих инструментов нет",
          not any(x in registry.REGISTRY for x in
                  ("write_plan", "update_nsi", "modify_code", "run_optimizer")))
    check("инструменты выводятся из источников маршрута",
          set(registry.tools_for_sources(["plan", "nsi"]))
          == {"read_plan", "lookup_nsi", "list_nsi_tables"},
          str(sorted(registry.tools_for_sources(["plan", "nsi"]))))
    fc = registry.openai_tools(["read_plan"])
    check("схема для function calling собрана",
          fc[0]["type"] == "function" and fc[0]["function"]["name"] == "read_plan")
    check("описание инструмента содержит SOP про служебные линии",
          "Отложенные" in fc[0]["function"]["description"])

    limit = int(cfg.limits["max_tool_calls"])
    current = new_trace("проверка лимита вызовов")
    hit = False
    for _ in range(limit + 2):
        try:
            call("list_nsi_tables")
        except ToolLimitExceeded:
            hit = True
            break
    check(f"лимит вызовов инструментов ({limit}) срабатывает", hit)

    print("\nТрасса")
    events = [json.loads(x) for t in traces
              for x in t.path.read_text(encoding="utf-8").splitlines()]
    tools = [e for e in events if e["kind"] == "tool"]
    check("каждый вызов записан в трассу", len(tools) >= 20, str(len(tools)))
    check("у успешных вызовов записан источник",
          all(t.get("source") for t in tools if t["status"] == "ok"))
    check("ошибки записаны со статусом и текстом",
          all(t.get("error") for t in tools if t["status"] in ("invalid_args", "not_found")))
    check("сработавший лимит зафиксирован",
          any(e["kind"] == "limit" for e in events))

    print()
    if FAILED:
        print(f"Провалено проверок: {len(FAILED)}")
        for f in FAILED:
            print(f"  · {f}")
        return 1
    print(f"Все проверки пройдены ({len(events)} событий в трассе).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
