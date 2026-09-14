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
    if state.evidence:
        lines += ["", "Что удалось проверить:"]
        seen: list[str] = []
        for e in state.evidence:
            if e.locator not in seen:
                seen.append(e.locator)
        lines += [f"- {loc}" for loc in seen[:8]]
    lines += ["", "Уверенность: низкая",
              "Нужно обращение в поддержку: нет (недостаточно данных для формулировки)"]
    deps.trace.decision(node="insufficient", action="report_gap",
                        reason_summary="доказательств недостаточно, догадки не выдаются",
                        gaps=state.gaps[:5])
    return {"status": "insufficient", "answer": "\n".join(lines),
            "confidence_label": "низкая"}
