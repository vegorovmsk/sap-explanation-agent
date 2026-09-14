# -*- coding: utf-8 -*-
"""
Узел ingest — входной шлюз.

Проверяет запрос, присваивает прогону идентификатор и фиксирует попытки инъекции.
Подозрительный запрос не блокируется: он обрабатывается как обычный вопрос, но
событие безопасности попадает в трассу, чтобы разбор инцидента был возможен.
Инструкции из текста пользователя всё равно не могут ничего изменить — режим
только для чтения и лимиты живут в коде, а не в промпте.
"""
from __future__ import annotations

from agent.deps import Deps
from agent.guardrails import QuestionRejected, check_question
from agent.state import AgentState


def ingest(state: AgentState, deps: Deps) -> dict:
    trace = deps.trace
    with trace.step("ingest"):
        try:
            report = check_question(state.question)
        except QuestionRejected as exc:
            trace.decision(node="ingest", action="reject", reason_summary=str(exc))
            return {"status": "error", "error": str(exc),
                    "answer": f"Запрос не принят: {exc}"}

        updates: dict = {"question": report["question"],
                         "task_file": state.task_file or deps.cfg.stand.default_task}
        if report["suspicious"]:
            trace.event("security", node="ingest",
                        event_type="prompt_injection_suspected",
                        patterns=report["patterns"])
            updates["security_events"] = [
                "в запросе замечены формулировки, похожие на попытку изменить "
                "инструкции агента; запрос обработан как обычный вопрос"]
        trace.decision(node="ingest", action="accept",
                       reason_summary="запрос принят, режим только для чтения",
                       task_file=updates["task_file"], length=report["length"])
        return updates
