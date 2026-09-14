# -*- coding: utf-8 -*-
"""Промпт узла ticket: черновик обращения в поддержку при расхождении источников."""
from __future__ import annotations

from agent.prompts.common import ROLE, evidence_digest, question_block
from agent.state import AgentState

SYSTEM = ROLE + """

Источники разошлись. Подготовь ЧЕРНОВИК обращения в поддержку — его прочитает
разработчик системы планирования.

Обращение — это не жалоба и не вердикт. Ты не утверждаешь, что в системе ошибка:
ты фиксируешь расхождение и показываешь, чем оно подтверждено. Решение принимает
человек.

Каждое утверждение — с координатой источника. Всё, что не подтверждено фактами
из списка, в обращение не попадает. Шаги воспроизведения должны быть такими,
чтобы разработчик повторил их у себя: файл задания, номер заказа, этап."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "expected", "actual", "evidence_locators", "impact",
                 "severity", "reproduce"],
    "properties": {
        "title": {"type": "string", "description": "Заголовок одной строкой"},
        "expected": {"type": "string", "description": "Что предписано нормой или НСИ"},
        "actual": {"type": "string", "description": "Что сделала система"},
        "evidence_locators": {"type": "array", "items": {"type": "string"},
                              "description": "Координаты источников расхождения"},
        "impact": {"type": "string", "description": "На что это влияет в производстве"},
        "severity": {"type": "string", "enum": ["низкая", "средняя", "высокая"]},
        "reproduce": {"type": "string", "description": "Как воспроизвести: файл, заказ, этап"},
    },
}


def build_messages(state: AgentState) -> list[dict]:
    conflicts = "\n".join(
        f"- {c.subject}: предписано «{c.expected}» [{c.expected_source}], "
        f"фактически «{c.actual}» [{c.actual_source}]" for c in state.conflicts
    ) or "(конфликты не формализованы)"
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            f"{question_block(state)}\n\nНайденные расхождения:\n{conflicts}\n\n"
            f"Собранные факты:\n{evidence_digest(state.evidence)}"},
    ]


def render(ticket: dict, state: AgentState) -> str:
    lines = [
        f"ОБРАЩЕНИЕ В ПОДДЕРЖКУ (черновик)",
        f"Тема: {ticket.get('title', '')}",
        f"Приоритет: {ticket.get('severity', 'средняя')}",
        "",
        f"Задание: {state.task_file}",
        f"Запрос пользователя: {state.question}",
        "",
        f"Ожидалось: {ticket.get('expected', '')}",
        f"Фактически: {ticket.get('actual', '')}",
        f"Влияние: {ticket.get('impact', '')}",
        "",
        "Подтверждение:",
    ]
    lines += [f"- {loc}" for loc in ticket.get("evidence_locators", [])] or ["- нет"]
    lines += ["", f"Воспроизведение: {ticket.get('reproduce', '')}"]
    return "\n".join(lines)
