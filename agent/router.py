# -*- coding: utf-8 -*-
"""
Условные рёбра графа.

Три развилки и одно обратное ребро — это и есть агентность решения в проверяемом
виде. Функции здесь намеренно простые и без обращений к модели: решение о
маршруте должно читаться в коде и повторяться от прогона к прогону, иначе
метрика «точность маршрута» не имеет смысла.
"""
from __future__ import annotations

from agent.state import AgentState


def after_classify(state: AgentState) -> str:
    """Развилка 1: хватает ли сущностей, чтобы вообще начинать сбор фактов."""
    if state.status == "error":
        return "insufficient"
    return "clarify" if state.status == "clarify" else "plan"


def after_observe(state: AgentState) -> str:
    """Развилка 2 и обратное ребро: продолжать сбор или переходить к выводу."""
    if state.status == "insufficient":
        return "insufficient"
    if state.evidence_enough:
        return "consistency"
    return "act"          # обратное ребро: через plan_sources — см. graph.py

    # Раньше здесь стояло правило «нет фактов после первой итерации — сдаваться».
    # Оно обрывало прогон ровно в том случае, ради которого цикл и нужен: первый
    # вызов инструмента не удался, модель ещё не видела текста ошибки. Остановку
    # по исчерпанию лимитов делают узлы act и observe, и этого достаточно.


def after_verify(state: AgentState) -> str:
    """Развилка 4: принять ответ или один раз переписать его по замечаниям.

    Рецензент — редактор, а не судья. Прогон золотого набора показал, к чему
    ведёт роль судьи: в шести кейсах сверка источников давала `confirmed` с
    уверенностью 0.8–0.9, объяснение было верным, и статус всё равно падал до
    `insufficient` из-за одной лишней фразы. Правильная реакция на ключевое
    замечание — снять спорное утверждение и оставить вывод, а не забраковать
    разбор целиком. Если после переписывания замечание осталось, вердикт
    снимается: два захода — предел.
    """
    critical = (state.verification or {}).get("critical") or []
    # Переписывать можно только то, что написал узел answer. Прогон с
    # обращением в поддержку сюда попадать не должен: текст там собирает узел
    # ticket, и отправка его в answer подменяет фиксацию расхождения обычным
    # объяснением. Живой прогон дал ровно это — и упал на переписывании.
    if critical and state.answer_revisions == 0 and state.answer_summary \
            and state.ticket is None:
        return "answer"
    return "end"


def after_consistency(state: AgentState) -> str:
    """Развилка 3: доказательный ответ, обращение в поддержку или честный отказ."""
    if state.status == "conflict":
        return "ticket"
    if state.status == "confirmed":
        return "answer"
    return "insufficient"
