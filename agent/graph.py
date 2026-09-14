# -*- coding: utf-8 -*-
"""
Граф выполнения на LangGraph.

Цикл Reason → Act → Observe выражен не скрытой цепочкой рассуждений модели, а
явными узлами и рёбрами: по трассе видно, какое действие выбрано, почему и на
чём прогон остановился.

    ingest → classify ─┬─ clarify ──────────────────────────────────→ конец
                       └→ plan_sources → act → observe ─┬─ (назад в plan_sources)
                                                        ├─ insufficient → конец
                                                        └→ check_consistency
                                                             ├→ answer ─┐
                                                             ├→ ticket ─┼→ verify → конец
                                                             └→ insufficient → конец

Состояние — обычный dataclass: узлы возвращают только изменённые поля, поэтому
любой шаг виден как дельта, а весь прогон восстанавливается из трассы.
Конфигурация, клиент моделей и трасса передаются замыканием и в состояние не
попадают: им там не место, и сериализовать их незачем.
"""
from __future__ import annotations

import dataclasses
from functools import partial

from langgraph.graph import END, StateGraph

from agent import router
from agent.deps import Deps
from agent.nodes.act import act
from agent.nodes.answer import answer
from agent.nodes.classify import classify
from agent.nodes.consistency import check_consistency
from agent.nodes.ingest import ingest
from agent.nodes.observe import observe
from agent.nodes.plan_sources import plan_sources
from agent.nodes.stage_context import stage_context
from agent.nodes.terminals import clarify, insufficient
from agent.nodes.ticket import ticket
from agent.nodes.verify import verify
from agent.state import AgentState

NODES = {
    "ingest": ingest,
    "classify": classify,
    "stage_context": stage_context,
    "plan_sources": plan_sources,
    "act": act,
    "observe": observe,
    "check_consistency": check_consistency,
    "answer": answer,
    "ticket": ticket,
    "verify": verify,
    "clarify": clarify,
    "insufficient": insufficient,
}


def build_graph(deps: Deps):
    """Собирает и компилирует граф. Узлы получают зависимости замыканием."""
    graph = StateGraph(AgentState)
    for name, fn in NODES.items():
        graph.add_node(name, partial(fn, deps=deps))

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "classify")
    # Обход «этап → норма → таблица → код» идёт до первого сбора фактов и ровно
    # один раз: обратное ребро из observe возвращает в plan_sources, минуя его.
    graph.add_conditional_edges("classify", router.after_classify, {
        "clarify": "clarify", "plan": "stage_context", "insufficient": "insufficient"})
    graph.add_edge("stage_context", "plan_sources")
    graph.add_edge("plan_sources", "act")
    graph.add_edge("act", "observe")
    # Обратное ребро ведёт в plan_sources, а не сразу в act. Иначе набор
    # инструментов остаётся тем, что выбран до первой итерации, и пробелы,
    # названные узлом observe, ничего не меняют: агент видит, что не хватает
    # нормы, но поиск по регламентам ему так и не предлагают.
    graph.add_conditional_edges("observe", router.after_observe, {
        "act": "plan_sources", "consistency": "check_consistency",
        "insufficient": "insufficient"})
    graph.add_conditional_edges("check_consistency", router.after_consistency, {
        "answer": "answer", "ticket": "ticket", "insufficient": "insufficient"})
    graph.add_edge("answer", "verify")
    graph.add_edge("ticket", "verify")
    # Второе обратное ребро: ключевое замечание проверки возвращает ответ на
    # переписывание — один раз. Рецензент здесь редактор, а не судья.
    graph.add_conditional_edges("verify", router.after_verify, {
        "answer": "answer", "end": END})
    graph.add_edge("clarify", END)
    graph.add_edge("insufficient", END)
    return graph.compile()


def run(question: str, task_file: str, deps: Deps) -> AgentState:
    """Один прогон целиком. Возвращает финальное состояние."""
    compiled = build_graph(deps)
    initial = AgentState(request_id=deps.trace.request_id, question=question,
                         task_file=task_file)
    # потолок шагов графа: страховка поверх лимитов узлов на случай, если
    # ветвление когда-нибудь зациклится не там, где ожидалось
    max_steps = int(deps.cfg.limits["max_iterations"]) * 3 + 10
    result = compiled.invoke(initial, config={"recursion_limit": max_steps})
    return result if isinstance(result, AgentState) else AgentState(
        **{k: v for k, v in result.items()
           if k in {f.name for f in dataclasses.fields(AgentState)}})
