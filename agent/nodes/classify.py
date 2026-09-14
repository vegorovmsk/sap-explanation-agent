# -*- coding: utf-8 -*-
"""
Узел classify — первый шаг цикла Reason → Act → Observe.

Дешёвая модель (роль M_fast) переводит свободный текст в строгий JSON: класс
вопроса и сущности. По этому JSON роутер выбирает роль модели и набор источников,
поэтому один дешёвый вызов экономит все последующие.

Здесь же первое ветвление графа: если сущностей не хватает или вопрос допускает
несколько прочтений — агент задаёт ОДИН уточняющий вопрос вместо того, чтобы
выбрать заказ наугад.
"""
from __future__ import annotations

from agent.deps import Deps
from agent.prompts import classify as prompt
from agent.state import AgentState


# Уточняющий вопрос по недостающей сущности формулируется кодом, а не моделью:
# он должен быть один и тот же от прогона к прогону.
ENTITY_QUESTIONS = {
    "order_number": "Укажите номер заказа, о котором идёт речь.",
    "line": "Укажите линию, о которой идёт речь.",
    "nsi_table": "Укажите номер таблицы НСИ.",
    "stage": "Укажите этап: экструзия, печать или кольцевание.",
}


def classify(state: AgentState, deps: Deps) -> dict:
    cfg, trace = deps.cfg, deps.trace
    intents = cfg.routing["intents"]

    with trace.step("classify", role="M_fast"):
        resp = deps.client.chat(
            "M_fast",
            prompt.build_messages(state.question, state.task_file, intents, cfg.stand),
            purpose="intent+entities",
            json_schema=prompt.schema_for(intents),
            schema_name="intent_extraction",
        )
    data = resp.data or {}

    entities = {k: v for k, v in (data.get("entities") or {}).items() if v is not None}
    ambiguity = data.get("ambiguity") or {"is_ambiguous": False, "question": None}
    route = cfg.route_for(data.get("intent") or "GENERAL_LOGIC_EXPLANATION")

    updates = {
        "intent": route["intent"],
        "entities": entities,
        "ambiguity": ambiguity,
        "role": route["role"],
        "companion_role": route["companion_role"],
        "required_sources": route["required_sources"],
        "optional_sources": route["optional_sources"],
    }

    trace.decision(node="classify", action="route_selected",
                   reason_summary=data.get("reason_summary", ""),
                   intent=route["intent"], entities=entities, role=route["role"],
                   companion_role=route["companion_role"],
                   required_sources=route["required_sources"])

    # Проверка работает в обе стороны и не зависит от флага модели. Первый
    # прогон дал «Где находится заказ Z-1001?» в ветке уточнения при извлечённом
    # номере; третий — обратное: «Почему заказ не туда встал?» без номера заказа
    # прошло дальше, и агент принялся объяснять положение несуществующего заказа.
    missing = [e for e in route["key_entities"] if not entities.get(e)]
    # Принудительное уточнение включается только если у вопроса НЕТ НИ ОДНОЙ
    # зацепки. Иначе оно бьёт по общим вопросам: «объясни действующие ограничения
    # на экструзии» — этап назван, объект вопроса ясен, номера заказа там быть не
    # может, и требовать его значит не ответить на исправно заданный вопрос.
    # Отсутствие ключевой сущности при наличии других — повод читать шире, а не
    # переспрашивать.
    # Флаг модели здесь НЕ участвует, и это не упрощение, а починка. Раньше
    # условие требовало `not ambiguity["is_ambiguous"]`, и открывалась щель:
    # модель объявляла вопрос неоднозначным, но вопроса не формулировала. Тогда
    # эта ветка не срабатывала (флаг поднят), следующая тоже (вопроса нет) — и
    # прогон уходил дальше без ключевой сущности. Живой прогон 14.09 на «Почему
    # заказ опаздывает?» провалился ровно так: модель подставила в номер заказа
    # имя файла задания и честно выяснила, что заказа «input_task_1» нигде нет.
    #
    # Правило простое: нет ни одной зацепки — спрашиваем. Формулировку берём у
    # модели, если она её дала, иначе составляем сами: вопрос должен быть один и
    # тот же от прогона к прогону.
    if missing and not entities:
        question = (ambiguity.get("question")
                    or ENTITY_QUESTIONS.get(missing[0],
                                            "Уточните, о каком объекте идёт речь."))
        trace.decision(node="classify", action="clarify_forced",
                       reason_summary="ключевая сущность не названа ни в каком виде",
                       missing_entities=missing,
                       model_flagged=bool(ambiguity.get("is_ambiguous")))
        updates["status"] = "clarify"
        updates["answer"] = question
        updates["ambiguity"] = {"is_ambiguous": True, "question": question}
        return updates

    if ambiguity.get("is_ambiguous") and ambiguity.get("question"):
        # Заявление о неоднозначности проверяется механически, как и всё
        # остальное. Живой прогон дал два кейса, где модель извлекла номер
        # заказа и тут же попросила уточнить, какой заказ имеется в виду:
        # «Где находится заказ Z-1001?» ушло в ветку уточнения. Если ключевые
        # сущности намерения на месте, уточнять нечего.
        missing = [e for e in route["key_entities"] if not entities.get(e)]
        if route["key_entities"] and not missing:
            trace.decision(node="classify", action="ambiguity_overruled",
                           reason_summary="ключевые сущности извлечены, уточнение не нужно",
                           key_entities=route["key_entities"],
                           question_dropped=ambiguity["question"])
            updates["ambiguity"] = {"is_ambiguous": False, "question": None}
            return updates
        trace.decision(node="classify", action="ask_clarifying_question",
                       reason_summary="в запросе не хватает ключевой сущности",
                       missing_entities=missing)
        updates["status"] = "clarify"
        updates["answer"] = ambiguity["question"]
    return updates
