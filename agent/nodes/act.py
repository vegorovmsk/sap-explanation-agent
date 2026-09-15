# -*- coding: utf-8 -*-
"""
Узел act — вызов инструментов через function calling.

Модель сама решает, что читать следующим: ей дан набор инструментов, отобранный
маршрутом, и сводка уже собранных фактов. Ответ пользователю здесь не пишется —
только сбор доказательств.

Каждый результат превращается в факты с координатой. Отсутствие записи тоже
становится фактом: если норматива для ключа в таблице нет, это доказывает
недопустимость, и в доказательную базу оно обязано попасть наравне с найденным.
"""
from __future__ import annotations

from typing import Any

from agent.deps import Deps
from agent.guardrails import limit_reached
from agent.prompts import act as prompt
from tools.nsi_lookup import list_nsi_tables
from agent.state import AgentState, Evidence
from tools.base import ToolResult
from tools.registry import ToolLimitExceeded, execute, openai_tools

MAX_ROWS = 8          # сколько строк одного результата превращать в факты
UNTRUSTED_TOOLS = {"search_regulations", "search_code", "read_logs"}


def _short(value: Any, limit: int = 160) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _fields(row: dict, keys: list[str]) -> str:
    return ", ".join(f"{k}: {row[k]}" for k in keys if row.get(k) not in (None, ""))


def evidence_from(result: ToolResult, args: dict) -> list[Evidence]:
    """Переводит результат инструмента в факты с координатами."""
    tool = result.tool
    source = {"read_task": "task", "read_plan": "plan", "lookup_nsi": "nsi",
              "search_regulations": "regulations", "search_code": "code",
              "read_logs": "logs", "list_nsi_tables": "nsi",
              "list_tasks": "task"}.get(tool, tool)
    trusted = tool not in UNTRUSTED_TOOLS
    payload = result.payload or {}
    out: list[Evidence] = []

    def add(claim: str, locator: str, value: Any = None) -> None:
        # Координата приводится к строке здесь, у истока. Пустая строка — это
        # «координаты нет», и её видно всем проверкам; None — это дыра, которая
        # всплывает где угодно потом: сборка ответа падала на «", ".join» уже
        # после того, как ответ был написан, и уносила весь прогон.
        out.append(Evidence(claim=claim, value=value, source=source,
                            locator=locator or result.source or "",
                            tool=tool, trusted=trusted))

    # отсутствие записи — тоже доказательство
    if result.status == "not_found":
        locator = result.locators[0] if result.locators else result.source
        add(result.error or "искомого в источнике нет", locator)
        diag = payload.get("диагностика_ключа") or []
        for item in diag:
            if "различие" in item:
                d = item["различие"]
                add(f"ключ «{d['запрошено']}» и «{d['в таблице']}» визуально совпадают, "
                    f"но записаны разными символами: "
                    f"{'; '.join(str(x) for x in d['различия'][:2])}", locator)
        return out

    if result.status != "ok":
        return out

    if tool == "read_task":
        add(f"заказ {payload.get('order_number')} есть в задании: "
            + _fields(payload, ["kind", "sort", "caliber", "color", "thickness",
                                "order_volume", "due_date", "services", "print_type",
                                "exclusivity", "ring_diameter"]),
            payload.get("_locator", ""))
    elif tool == "read_plan":
        for part in (payload.get("партии") or [])[:MAX_ROWS]:
            add(f"{part.get('НомерЗаказа')}, этап {part.get('Этап')}: "
                # Желаемая и расчётная даты обязаны быть в фактах. Без них вопрос
                # «почему заказ готов позже желаемой даты» доказать нечем: в
                # расписании они есть, а в сводку не попадали, и проверка ответа
                # справедливо браковала объяснение опоздания.
                + _fields(part, ["Линия", "ДатаНачалаПП", "ДатаОкончанияПП",
                                 "ЖелаемаяДатаГотовности", "РасчётнаяДатаГотовности",
                                 "Опоздание, дн", "Потери, кг",
                                 "ДопустимыеЛинии", "Причина",
                                 "ОбъёмЦветовогоБлока", "МинЦветовойБлок", "ГодГотовности"]),
                part.get("_locator", ""))
            neigh = part.get("соседи_по_очереди") or {}
            for side in ("предыдущая", "следующая"):
                item = neigh.get(side)
                if item:
                    add(f"{side} партия на линии {item.get('Линия')}: "
                        + _fields(item, ["НомерЗаказа", "ДатаНачалаПП", "Калибр", "Цвет"]),
                        item.get("_locator", ""))
        for err in (payload.get("замечания_НСИ") or [])[:MAX_ROWS]:
            add("замечание НСИ: " + _fields(err, ["Таблица", "Код причины", "Уровень",
                                                  "Этап", "Детализация"]),
                err.get("_locator", ""))
        for item in (payload.get("очередь") or [])[:MAX_ROWS]:
            add(f"очередь на {payload.get('линия')}: "
                + _fields(item, ["НомерЗаказа", "ДатаНачалаПП", "Калибр", "Цвет"]),
                item.get("_locator", ""))
    elif tool == "lookup_nsi":
        table = payload.get("таблица")
        name = payload.get("название", "")
        for row in (payload.get("строки") or [])[:MAX_ROWS]:
            body = ", ".join(f"{k}: {v}" for k, v in row.items()
                             if k != "_locator" and v not in (None, ""))
            add(f"табл. {table} «{name}»: {_short(body, 220)}", row.get("_locator", ""))
    elif tool == "search_regulations":
        for item in (payload.get("пункты") or [])[:MAX_ROWS]:
            add(_short(item.get("текст", ""), 300), item.get("координата", ""))
    elif tool == "search_code":
        for item in (payload.get("фрагменты") or [])[:MAX_ROWS]:
            doc = item.get("документация") or ""
            head = f"{item.get('полное_имя')} ({item.get('файл')})"
            add(f"{head}: {_short(doc or item.get('код', ''), 300)}",
                item.get("координата", ""), value=f"строки {item.get('строки')}")
    elif tool == "read_logs":
        for line in (payload.get("строки") or [])[:MAX_ROWS]:
            add(_short(line.get("текст", ""), 220), line.get("_locator", ""))
    elif tool == "list_nsi_tables":
        names = ", ".join(f"{t['таблица']} — {t['название']}"
                          for t in (payload.get("таблицы") or [])[:20])
        add(f"каталог таблиц НСИ: {_short(names, 400)}", "каталог НСИ")
    return out


_CATALOGUE_CACHE: dict[str, str] = {}


def _nsi_catalogue(cfg, state: AgentState) -> str:
    """Перечень таблиц НСИ прямо в промпте, если маршрут ведёт к нормативам.

    Дешевле и надёжнее, чем надеяться, что модель сама вызовет каталог: номера
    таблиц не выводятся из здравого смысла, и без подсказки модель уверенно
    спрашивает допустимость печатной линии в таблице условий экструзии.

    Вместе с номером и названием печатается СОСТАВ КОЛОНОК. Инструмент отдавал
    их и раньше, а этот узел выбрасывал — и прогон 14.09 заплатил за это восемью
    отказами `invalid_args` из сорока вызовов НСИ: фильтры ставились по
    выдуманным колонкам («Линия» в табл. 1, «Причина» в табл. 27, «Калибр» в
    табл. 23). Ни один из этих вызовов не мог сработать, и каждый стоил круга.
    Названия колонок — это факт о стенде, который лежит в файле; показывать его
    дешевле, чем оплачивать догадки.
    """
    if "nsi" not in set(state.required_sources) | set(state.optional_sources):
        return ""
    key = str(cfg.stand.params_dir)
    if key not in _CATALOGUE_CACHE:
        try:
            tables = (list_nsi_tables(cfg).payload or {}).get("таблицы", [])
            lines = []
            for t in tables:
                # Кавычки, а не запятые: имя колонки само содержит запятую
                # («Нормативная производительность, км/час»), и список через
                # запятую распадался бы на две несуществующие колонки.
                columns = " ".join(f"«{c}»" for c in (t.get("колонки") or [])) \
                    or "колонки не прочитаны"
                lines.append(f"  {t['таблица']:>2} — {t['название']}\n"
                             f"       колонки: {columns}")
            _CATALOGUE_CACHE[key] = "\n".join(lines)
        except Exception:  # noqa: BLE001 — каталог не критичен для работы узла
            _CATALOGUE_CACHE[key] = ""
    return _CATALOGUE_CACHE[key]


def act(state: AgentState, deps: Deps) -> dict:
    cfg, trace, client = deps.cfg, deps.trace, deps.client
    iteration = state.iterations + 1

    stopper = limit_reached(cfg, trace, client)
    if stopper:
        trace.limit_hit(stopper, cfg.limits.get(stopper))
        return {"iterations": iteration, "status": "insufficient",
                "gaps": state.gaps + [f"сбор фактов остановлен по лимиту {stopper}"]}

    trace.iteration(iteration, reason_summary="сбор фактов")
    catalogue = _nsi_catalogue(cfg, state)
    evidence = list(state.evidence)
    sources_seen = list(state.sources_seen)
    failed = list(state.failed_calls)
    tools = openai_tools(state.planned_tools)

    # Роль-напарник включается там, где на ходе предстоит работать с исходниками:
    # разбор кода — отдельный навык, и маршрут объявляет для него свою модель
    # (M_code у классов «что означает код причины» и «дефект данных»). Раньше
    # companion_role только писалась в трассу и не вызывалась ни разу — маршрут
    # обещал четыре роли, а работали три.
    role = state.role
    if state.companion_role and "search_code" in state.planned_tools:
        role = state.companion_role

    with trace.step("act", role=role, iteration=iteration):
        resp = client.chat(role, prompt.build_messages(state, iteration, catalogue),
                           purpose="collect_evidence", tools=tools)

        if not resp.tool_calls:
            trace.decision(node="act", action="no_tool_calls",
                           reason_summary=_short(resp.text or "модель не запросила чтение", 200))
            return {"iterations": iteration, "evidence": evidence,
                    "sources_seen": sources_seen, "failed_calls": failed}

        for call in resp.tool_calls:
            try:
                result = execute(call.name, call.arguments, cfg=cfg, trace=trace, client=client)
            except ToolLimitExceeded as exc:
                trace.decision(node="act", action="stop_on_limit", reason_summary=str(exc))
                break
            if result.suggest:
                # инструмент нашёл искомое, но в другом месте: это промах запроса,
                # а не доказательство отсутствия. В доказательную базу такое
                # пускать нельзя — иначе «в табл. 27 нет строк по ЛП2» будет
                # выглядеть как установленный факт о печатной линии
                failed.append({"инструмент": call.name, "аргументы": call.arguments,
                               "ошибка": result.error, "подсказка": result.suggest})
                continue
            if result.status in ("invalid_args", "error"):
                # неудачный вызов не доказывает ничего, но модель обязана его увидеть:
                # иначе на следующей итерации она повторит ту же ошибку вслепую
                failed.append({"инструмент": call.name, "аргументы": call.arguments,
                               "ошибка": result.error, "подсказка": result.hint})
                continue
            facts = evidence_from(result, call.arguments)
            evidence.extend(facts)
            if facts and facts[0].source not in sources_seen:
                sources_seen.append(facts[0].source)

    return {"iterations": iteration, "evidence": evidence, "sources_seen": sources_seen,
            "failed_calls": failed, "tool_calls": trace.tool_calls}
