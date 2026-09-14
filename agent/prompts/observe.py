# -*- coding: utf-8 -*-
"""Промпт узла observe: хватает ли доказательств, чтобы отвечать."""
from __future__ import annotations

from agent.prompts.common import ROLE, evidence_digest, question_block, sources_block
from agent.state import AgentState

SYSTEM = ROLE + """

Сейчас ты оцениваешь ПОЛНОТУ доказательной базы, а не отвечаешь на вопрос.

Доказательств достаточно, если собранных фактов хватает, чтобы объяснить решение
системы и сослаться на источник каждого утверждения. Отсутствие записи — тоже
факт: если норматива для ключа в таблице нет, это доказывает недопустимость, и
искать дальше не нужно.

Доказательств не хватает, если не просмотрен обязательный источник или ключевое
звено цепочки нечем подтвердить. Тогда назови, чего именно не хватает.

Не проси искать «на всякий случай»: каждый лишний шаг стоит времени и денег.

Пробел, который ты уже заявлял, а источник с тех пор прочитан, — закрыт. Если
чтение вернуло «записи нет», это и есть ответ на пробел, а не повод повторить
его ещё раз: заказа нет в расписании, норматива нет в таблице, пункта нет в
регламенте — всё это установленные факты, на них можно ссылаться. Повторять
один и тот же пробел, когда источник уже отвечал, значит держать прогон в
петле: он всё равно оборвётся по лимиту, но без вывода."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["enough", "missing_sources", "gaps", "reason_summary"],
    "properties": {
        "enough": {"type": "boolean", "description": "Хватает ли фактов для ответа"},
        "missing_sources": {
            "type": "array", "items": {"type": "string"},
            "description": "Ключи источников, которых не хватает: task, plan, nsi, "
                           "regulations, code, logs",
        },
        "gaps": {"type": "array", "items": {"type": "string"},
                 "description": "Чего конкретно не хватает, короткими формулировками"},
        "reason_summary": {"type": "string", "description": "Одна фраза для журнала"},
    },
}


def _history_block(state: AgentState) -> str:
    """Что уже спрашивали на прошлых кругах и сколько раз.

    Без этого блока узел оценивал полноту с чистого листа каждую итерацию и
    честно выводил один и тот же пробел. Прогон 14.09: по заказу B-3007
    расписание читали трижды, трижды получали «в расчёте его нет» — и все три
    раза оценка полноты заявляла, что отсутствие не подтверждено. Восемь кейсов
    из девятнадцати упёрлись в лимит итераций так же.
    """
    if not state.gap_history:
        return ""
    counts: dict[str, int] = {}
    order: list[str] = []
    for g in state.gap_history:
        key = " ".join(str(g).lower().split())[:80]
        if key not in counts:
            order.append(g)
        counts[key] = counts.get(key, 0) + 1
    lines = []
    for g in order[-6:]:
        n = counts[" ".join(str(g).lower().split())[:80]]
        lines.append(f"- {g}" + (f"  ← заявлен {n} раз(а)" if n >= 2 else ""))
    return ("\n\nЧего не хватало на прошлых кругах (источники с тех пор читались):\n"
            + "\n".join(lines))


def _failed_block(state: AgentState) -> str:
    """Вызовы, которые не состоялись. Их результат — не доказательство отсутствия."""
    if not state.failed_calls:
        return ""
    lines = [f"- {f['инструмент']}: {f.get('ошибка')}" for f in state.failed_calls[-3:]]
    return ("\n\nВызовы, которые не удались (это НЕ доказывает отсутствие записи, "
            "вызов просто не состоялся):\n" + "\n".join(lines))


def build_messages(state: AgentState) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            f"{question_block(state)}\n\n{sources_block(state)}\n\n"
            f"Собранные факты:\n{evidence_digest(state.evidence)}"
            f"{_history_block(state)}{_failed_block(state)}"},
    ]
