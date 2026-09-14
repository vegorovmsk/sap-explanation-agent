# -*- coding: utf-8 -*-
"""
Узел answer — доказательное объяснение пользователю.

Ответ собирается по фиксированному шаблону: краткий вывод, перечень проверенных
источников, объяснение цепочкой, статус, уверенность и признак эскалации. Шаблон
нужен не для красоты — он делает ответ машинно проверяемым, и на нём же держится
прогон evals.

Координаты, которых нет в доказательной базе, вычёркиваются здесь же, до
проверки: модель не должна иметь возможности сослаться на несуществующую строку.
Проверяется не только список ссылок, но и сам текст: вычеркнуть выдуманную
координату из списка мало, если фраза, которая на неё опиралась, осталась в
объяснении — а живой прогон дал именно это, и верный разбор был забракован
проверкой из-за одной лишней ссылки на непрочитанный регламент.

Порядок такой: сначала модели прямо говорят, какие координаты выдуманы, и просят
переписать ответ без них. Если и после этого ссылка осталась, фраза с ней
вычёркивается механически, а в «чего не удалось подтвердить» появляется строка.
"""
from __future__ import annotations

import dataclasses

from agent import locators
from llm.client import LLMError
from agent.deps import Deps
from agent.prompts import answer as prompt
from agent.state import AgentState


def answer(state: AgentState, deps: Deps) -> dict:
    trace = deps.trace
    # Второй заход: проверка вернула ответ с ключевыми замечаниями. Собираем
    # заново без спорных утверждений, а не бракуем разбор целиком.
    critical = (state.verification or {}).get("critical") or []
    revision = bool(critical) and state.answer_revisions == 0
    messages = (prompt.build_revision_messages(state, critical) if revision
                else prompt.build_messages(state))
    purpose = "generate_answer:revision" if revision else "generate_answer"
    if revision:
        trace.event("answer.revision", node="answer",
                    claims=[c[:80] for c in critical][:5])

    try:
        with trace.step("answer", role=state.role):
            resp = deps.client.chat(state.role, messages, purpose=purpose,
                                    json_schema=prompt.SCHEMA, schema_name="answer")
    except LLMError as exc:
        if not revision:
            raise
        # Разбор уже готов, переписывание — улучшение. Модель, не справившаяся
        # со схемой на втором заходе, не повод превращать законченный прогон в
        # ошибку: оставляем прежний текст, а вердикт снимет проверка.
        trace.event("answer.revision_failed", node="answer", status="kept_previous",
                    error=f"{type(exc).__name__}: {exc}"[:300])
        return {"answer_revisions": state.answer_revisions + 1}
    data = resp.data or {}
    if revision and not data:
        # Модель не вернула ничего пригодного. Пустой ответ хуже спорного:
        # оставляем прежний текст, а вердикт снимет следующая проверка.
        data = {"summary": state.answer_summary,
                "explanation": state.answer_explanation,
                "cited_locators": list(state.cited_locators),
                "confidence": state.confidence_label}
        trace.event("answer.revision_empty", node="answer", status="kept_previous")
    known = state.locators()

    def unsupported(payload: dict) -> list[str]:
        """Выдуманные координаты — и в списке ссылок, и в тексте объяснения."""
        claimed = list(payload.get("cited_locators") or [])
        claimed += locators.in_text(payload.get("explanation", ""))
        claimed += locators.in_text(payload.get("summary", ""))
        seen, uniq = set(), []
        for c in claimed:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        return locators.unknown(uniq, known)

    invented = unsupported(data)
    if invented:
        trace.event("evidence.invented_locators", node="answer", locators=invented[:5],
                    status="repair_requested")
        with trace.step("answer.repair", role=state.role):
            repair = deps.client.chat(
                state.role,
                prompt.build_repair_messages(state, invented, resp.text or ""),
                purpose="generate_answer:no_invented_locators",
                json_schema=prompt.SCHEMA, schema_name="answer")
        if repair.data:
            data = repair.data
            invented = unsupported(data)

    explanation = data.get("explanation", "")
    summary = data.get("summary", "")
    struck: list[str] = []
    if invented:
        # Модель не отступилась. Дальше уже не уговоры, а вычёркивание.
        explanation, struck = locators.strike(explanation, invented)
        summary, struck_summary = locators.strike(summary, invented)
        struck += struck_summary
        trace.event("evidence.invented_locators", node="answer", locators=invented[:5],
                    struck=len(struck), status="struck")

    claimed = list(data.get("cited_locators") or [])
    updates = {
        "answer_summary": summary,
        "answer_explanation": explanation,
        "cited_locators": locators.known_only(claimed, known),
        "confidence_label": data.get("confidence") or state.confidence_label,
    }
    if struck:
        note = "часть утверждений снята: ссылки на источники, которые не были прочитаны"
        if note not in state.gaps:
            updates["gaps"] = list(state.gaps) + [note]
        updates["struck_claims"] = state.struck_claims + len(struck)
    if revision:
        updates["answer_revisions"] = state.answer_revisions + 1
    updates["answer"] = prompt.render(dataclasses.replace(state, **updates))
    trace.decision(node="answer", action="answer_revised" if revision else "answer_drafted",
                   reason_summary="ответ собран по шаблону",
                   cited=len(updates["cited_locators"]), dropped=len(invented),
                   struck=len(struck))
    return updates
