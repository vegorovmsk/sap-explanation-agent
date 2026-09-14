# -*- coding: utf-8 -*-
"""
Узел stage_context — карта источников этапа, собранная кодом.

Выполняется один раз, между classify и первым plan_sources. Смысл узла в том,
чтобы снять с модели работу, которую код делает точно: какие нормы относятся к
этапу, на какие таблицы НСИ они ссылаются, какие функции эти таблицы читают.
Всё это уже лежит в индексах и в графе связей — и живые прогоны показали, во
что превращается попытка спросить это у модели: угаданные номера таблиц,
запросы не в ту таблицу, четыре круга подряд с одним и тем же пробелом
«нужна таблица, которую читает ExtrusionStage.run».

Карта — это указатели, а не доказательства. Факты по-прежнему появляются только
из вызовов инструментов, с координатами. Узел лишь избавляет модель от догадок
о структуре и показывает ей два места, где норма и код разошлись.
"""
from __future__ import annotations

from agent.deps import Deps
from agent.state import AgentState
from memory import traversal


def stage_context(state: AgentState, deps: Deps) -> dict:
    cfg, trace = deps.cfg, deps.trace

    with trace.step("stage_context", role="router"):
        # Этап определяется не только прямым словом. Прогон 14.09 дал
        # `stage_not_named` в 14 кейсах из 19 именно потому, что искалось
        # буквальное «экструзия»: вопрос про ЛК2, вопрос про таблицу
        # минимальных блоков и вопрос про переходы по калибру этап называют, но
        # другими словами. Способ вывода пишется в трассу рядом с ответом —
        # «ЛК2 относится к кольцеванию» проверяемо, «агент решил» нет.
        stage, how = traversal.detect_stage(cfg, state.question, state.entities)
        if not stage:
            # Этап не назван — вопрос либо про конкретный заказ (этап выяснится
            # из расписания), либо про систему целиком. Карту не строим: пустая
            # карта честнее выдуманной.
            trace.decision(node="stage_context", action="stage_not_named",
                           reason_summary=f"{how}, карта источников не собирается")
            return {}
        try:
            smap = traversal.build_map(cfg, stage)
        except Exception as exc:                                   # noqa: BLE001
            # Обход опирается на индексы. Если их нет, это не повод ронять
            # прогон: агент продолжит работать инструментами, как раньше.
            trace.event("stage_context.failed", node="stage_context",
                        error=f"{type(exc).__name__}: {exc}", status="skipped")
            return {}

        trace.decision(
            node="stage_context", action="map_built",
            reason_summary=f"этап «{stage}» ({how}): {smap['пунктов']} пунктов, "
                           f"{len(smap['таблицы'])} таблиц, "
                           f"{len(smap['код_этапа'])} функций",
            detected_by=how,
            stage=stage,
            tables=[t["таблица"] for t in smap["таблицы"]],
            candidates=[c["параметр"] for c in smap["непокрытые_условия"]],
            norms_without_code=smap["нормы_без_кода"],
            code_without_norms=smap["код_без_норм"])
    return {"stage_map": smap}
