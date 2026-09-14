# -*- coding: utf-8 -*-
"""
Терминальные узлы: уточняющий вопрос и честный отказ.

Оба выхода намеренно не пытаются ответить по существу. Уточнение возвращает
ровно один вопрос — не список из пяти, иначе пользователь получает анкету вместо
диалога. Отказ прямо называет, чего не хватило: это полезнее вежливой
неопределённости и защищает от главного риска объяснителя — правдоподобной
выдумки.
"""
from __future__ import annotations

from agent.deps import Deps
from agent.state import AgentState


def clarify(state: AgentState, deps: Deps) -> dict:
    question = state.ambiguity.get("question") or "Уточните, пожалуйста, номер заказа."
    deps.trace.decision(node="clarify", action="ask_user",
                        reason_summary="в запросе не хватает ключевой сущности")
    return {"status": "clarify", "answer": question, "confidence_label": "низкая"}


def insufficient(state: AgentState, deps: Deps) -> dict:
    lines = ["Краткий вывод:",
             "Причина не подтверждена по доступным данным."]
    if state.limited_by:
        lines.append(f"Сбор фактов остановлен по ограничению: {state.limited_by}.")
    if state.gaps:
        lines += ["", "Чего не хватило:"] + [f"- {g}" for g in state.gaps[:6]]
    # Координаты без значения в список не идут. Живой прогон 14.09 напечатал
    # «Что удалось проверить:» и под ним одно пустое тире: факты были — два
    # промаха вида «заказа нет», — но координаты у них пустые, потому что искали
    # несуществующий объект. Пустой пункт хуже отсутствующего раздела: он
    # выглядит как оборванный вывод.
    seen: list[str] = []
    for e in state.evidence:
        if e.locator and e.locator not in seen:
            seen.append(e.locator)
    if seen:
        lines += ["", "Что удалось проверить:"] + [f"- {loc}" for loc in seen[:8]]

    if not seen and not state.gaps:
        # Совсем пустой отказ бесполезен: человек не знает ни что случилось, ни
        # что делать дальше. Сказать нечего — значит, надо сказать хотя бы это.
        tried = {f["инструмент"] for f in state.failed_calls}
        lines += ["", "Ни один источник не дал подтверждённых фактов."]
        if tried:
            lines.append("Не удались вызовы: " + ", ".join(sorted(tried)) + ".")
        lines.append("Уточните вопрос — например, назовите номер заказа, "
                     "линию или этап.")

    lines += ["", "Уверенность: низкая",
              "Нужно обращение в поддержку: нет (недостаточно данных для формулировки)"]
    deps.trace.decision(node="insufficient", action="report_gap",
                        reason_summary="доказательств недостаточно, догадки не выдаются",
                        gaps=state.gaps[:5])
    return {"status": "insufficient", "answer": "\n".join(lines),
            "confidence_label": "низкая"}
