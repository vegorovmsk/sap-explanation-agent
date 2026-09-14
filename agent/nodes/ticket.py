# -*- coding: utf-8 -*-
"""
Узел ticket — черновик обращения в поддержку.

Ветка, ради которой агент вообще отличается от справочной системы. Найдя
расхождение между нормой и реализацией, он не объявляет ошибку, а формализует
наблюдение: что предписано, что происходит, чем подтверждено и как повторить.
Решение принимает человек — агент только избавляет его от получаса раскопок.
"""
from __future__ import annotations

import dataclasses

from agent import locators
from agent.deps import Deps
from agent.prompts import answer as answer_prompt
from agent.prompts import ticket as prompt
from agent.state import AgentState


def ticket(state: AgentState, deps: Deps) -> dict:
    trace = deps.trace
    with trace.step("ticket", role=state.role):
        resp = deps.client.chat(state.role, prompt.build_messages(state),
                                purpose="draft_support_ticket",
                                json_schema=prompt.SCHEMA, schema_name="support_ticket")
    data = dict(resp.data or {})
    known = state.locators()
    data["evidence_locators"] = locators.known_only(
        data.get("evidence_locators") or [], known)

    with trace.step("answer", role=state.role, note="ответ к обращению"):
        ans = deps.client.chat(state.role, answer_prompt.build_messages(state),
                               purpose="generate_answer",
                               json_schema=answer_prompt.SCHEMA, schema_name="answer")
    adata = ans.data or {}
    updates = {
        "ticket": data,
        "ticket_text": prompt.render(data, state),
        "answer_summary": adata.get("summary", ""),
        "answer_explanation": adata.get("explanation", ""),
        "cited_locators": locators.known_only(adata.get("cited_locators") or [], known),
        "confidence_label": adata.get("confidence") or state.confidence_label,
    }
    updates["answer"] = answer_prompt.render(dataclasses.replace(state, **updates))
    trace.decision(node="ticket", action="support_ticket_drafted",
                   reason_summary=data.get("title", ""),
                   severity=data.get("severity"), locators=len(data["evidence_locators"]))
    return updates
