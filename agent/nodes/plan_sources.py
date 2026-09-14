# -*- coding: utf-8 -*-
"""
Узел plan_sources — планирование источников.

Решение детерминировано и читается из `config/routing.yaml`, а не выводится
моделью. Так маршрут виден в трассе, воспроизводим от прогона к прогону и
проверяем метрикой «точность маршрута»: если бы набор источников каждый раз
выбирала модель, сравнивать было бы не с чем.

Здесь же управление retrieval со стороны агента: для простого поиска заказа
векторный поиск не предлагается вовсе, для вопроса о норме он обязателен, а на
повторных итерациях к набору добавляются инструменты под конкретные пробелы,
названные узлом observe.
"""
from __future__ import annotations

from agent.deps import Deps
from agent.state import AgentState
from tools.registry import tools_for_sources

# Пробел в доказательствах → каким источником его закрывать
GAP_HINTS = {
    "норматив": "nsi", "нси": "nsi", "таблиц": "nsi",
    "регламент": "regulations", "норма": "regulations", "пункт": "regulations",
    "код": "code", "реализ": "code", "функци": "code", "причин": "code",
    "лог": "logs", "журнал": "logs", "событи": "logs",
    "задани": "task", "заказ": "task",
    "план": "plan", "расписан": "plan", "партия": "plan",
}


def _sources_for_gaps(gaps: list[str]) -> list[str]:
    found: list[str] = []
    for gap in gaps:
        low = gap.lower()
        for marker, source in GAP_HINTS.items():
            if marker in low and source not in found:
                found.append(source)
    return found


def plan_sources(state: AgentState, deps: Deps) -> dict:
    trace = deps.trace
    with trace.step("plan_sources", role="router"):
        sources = list(state.required_sources)
        # Источник, названный в самом вопросе, читается всегда. «В журнале
        # расчёта есть замечание НСИ» — прямое указание, куда смотреть, и
        # прогон, в котором журнал так и не открыли, показал, что рассчитывать
        # на догадливость модели тут нельзя.
        named = _sources_for_gaps([state.question])
        for s in named:
            if s not in sources:
                sources.append(s)
        # Разбор конкретного заказа начинается с того, что было заказано:
        # параметры заказа — вход для любого норматива и любого ограничения.
        # Без задания сверка расчёта с НСИ повисает: объёма взять негде.
        if state.entities.get("order_number") and "task" not in sources:
            sources.append("task")
        # пробелы прошлой итерации превращаются в источники: узел выполняется
        # перед каждым заходом в act, поэтому набор инструментов растёт по мере
        # того, как выясняется, чего не хватает
        from_gaps = _sources_for_gaps(state.gaps + state.missing_sources())
        for s in from_gaps:
            if s not in sources:
                sources.append(s)
        # Кандидат в расхождение проверяется только чтением обеих сторон:
        # нормы и кода. Без этого обход находит зацепку, а подтвердить её нечем.
        if (state.stage_map or {}).get("непокрытые_условия"):
            for s in ("regulations", "code"):
                if s not in sources:
                    sources.append(s)
        # на повторных итерациях необязательные источники тоже идут в дело:
        # первый заход намеренно узкий, дальше держать их закрытыми незачем
        if state.iterations >= 1:
            for s in state.optional_sources:
                if s not in sources:
                    sources.append(s)

        tools = tools_for_sources(sources)
        retrieval = [t for t in tools if t in ("search_regulations", "search_code")]
        trace.decision(
            node="plan_sources", action="tools_selected",
            reason_summary=(
                "вопрос закрывается точечным чтением; поиск по регламентам и коду "
                "подключится, только если observe укажет на пробел"
                if not retrieval else
                "в набор добавлен поиск: " + ", ".join(retrieval)),
            sources=sources, tools=tools, retrieval=bool(retrieval),
            role=state.role, companion_role=state.companion_role)
    return {"planned_tools": tools, "required_sources": state.required_sources,
            "optional_sources": state.optional_sources}
