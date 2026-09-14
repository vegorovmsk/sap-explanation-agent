# -*- coding: utf-8 -*-
"""
Узел observe — оценка доказательной базы.

Здесь агент решает, продолжать сбор или переходить к выводу. Решение принимается
в два слоя: сначала жёсткие ограничения (лимиты и наличие фактов), потом суждение
модели о полноте. Порядок важен — лимит должен останавливать раньше, чем модель
успеет попросить «ещё чуть-чуть поискать».

Весь узел целиком обёрнут в шаг трассы, включая ранние выходы: шаг, который
иногда не пишется, хуже отсутствующего — по такой трассе нельзя восстановить
путь прогона, а именно ради этого она и ведётся.
"""
from __future__ import annotations

from agent.deps import Deps
from agent.guardrails import limit_reached
from agent.nodes import judgement
from agent.prompts import observe as prompt
from agent.prompts.common import SOURCE_NAMES
from agent.state import AgentState


def _short(text: str, limit: int = 60) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def observe(state: AgentState, deps: Deps) -> dict:
    cfg, trace = deps.cfg, deps.trace

    with trace.step("observe", role="M_balanced"):
        if not state.evidence:
            stopper = limit_reached(cfg, trace, deps.client)
            if stopper is None and state.failed_calls:
                # фактов нет, потому что вызовы не удались, — но лимит ещё не исчерпан.
                # Сдаваться рано: на следующей итерации модель увидит текст ошибки и
                # подсказку и сможет исправить вызов. Именно здесь окупается то, что
                # сбойные вызовы вообще доходят до модели.
                gaps = [f"вызов {f['инструмент']} не удался: {f.get('ошибка')}"
                        for f in state.failed_calls[-3:]]
                trace.decision(node="observe", action="retry_after_tool_error",
                               reason_summary="фактов нет из-за ошибок вызова, "
                                              "есть попытка исправиться",
                               failed=len(state.failed_calls))
                return {"gaps": gaps, "evidence_enough": False,
                        "gap_history": state.gap_history + gaps}

            if stopper is None and state.tool_calls == 0:
                # Инструменты не подвели — их вообще не вызвали. Живой прогон по
                # кейсу B1 дал именно это: модель написала план сбора прозой и
                # не сделала ни одного вызова, а агент на этом сдался за 11 с.
                # Пока лимиты позволяют, это повод повторить ход, а не итог.
                trace.decision(node="observe", action="retry_without_tool_calls",
                               reason_summary="ни один инструмент не был вызван, "
                                              "лимиты ещё позволяют повторить ход")
                gap = ("ни один инструмент не был вызван: план сбора фактами не является, "
                       "нужен вызов")
                return {"gaps": [gap], "evidence_enough": False,
                        "gap_history": state.gap_history + [gap]}

            reason = ("инструменты не дали ни одного факта" if stopper is None
                      else f"фактов нет, исчерпан лимит {stopper}")
            trace.decision(node="observe", action="insufficient", reason_summary=reason)
            return {"status": "insufficient",
                    "gaps": state.gaps + ["ни один источник не дал подтверждённых фактов"]}

        # Круг прошёл, а фактов не прибавилось — спрашивать модель не о чем.
        # Прогон 14.09: восемь кейсов из девятнадцати дошли до лимита итераций,
        # и в каждом последние круги повторяли один и тот же пробел поверх
        # неизменной доказательной базы. Оценка полноты стоит вызова модели;
        # платить за него, чтобы услышать вчерашний ответ, незачем.
        # Прогресс — это не только новый факт. Промах инструмента с подсказкой
        # («в табл. 27 нет строк по ЛП2; печатные линии — в табл. 18») фактом не
        # становится намеренно: отсутствие записи не в той таблице ничего не
        # доказывает. Но следующий круг он делает осмысленным. Прогон 14.09
        # показал цену этой разницы: кейсы A1 и A2 останавливались ровно на
        # круге, который принёс подсказку, и до нужной таблицы не доходили.
        grew = (len(state.evidence) > state.last_evidence_count
                or len(state.failed_calls) > state.last_failed_count)
        if not grew and state.iterations >= 2:
            trace.decision(node="observe", action="stop_no_progress",
                           reason_summary="круг не дал ни одного нового факта, "
                                          "сбор остановлен",
                           iterations=state.iterations,
                           evidence=len(state.evidence))
            return {"evidence_enough": True, "limited_by": "без_прогресса",
                    "last_evidence_count": len(state.evidence),
                    "last_failed_count": len(state.failed_calls),
                    "gaps": state.gaps}

        # Обязательный источник не прочитан — это видно коду, спрашивать модель
        # не о чем. Прогон 14.09: из 58 решений узла 36 были ровно этим, то есть
        # две трети вызовов самой дорогой роли уходили на пересказ того, что
        # `state.missing_sources()` считает сравнением двух списков. Оценка
        # полноты доминировала в латентности набора (в одном кейсе 67 с, в другом
        # с починкой 361 с) — и в большинстве случаев ни на что не влияла.
        #
        # Правило то же, что и во всём агенте: модель спрашивают о том, чего код
        # решить не может. Полнота списка источников решается сравнением списков;
        # достаточность фактов ВНУТРИ прочитанного — нет, и вот об этом вызов.
        missing = state.missing_sources()
        gap = ("не просмотрен обязательный источник: "
               + ", ".join(SOURCE_NAMES.get(m, m) for m in missing)) if missing else ""
        repeated = sum(1 for g in state.gap_history
                       if " ".join(str(g).lower().split())[:80] == gap.lower()[:80])
        stopper = limit_reached(cfg, trace, deps.client)
        if missing and not stopper and repeated < 2:
            trace.decision(node="observe", action="source_missing",
                           reason_summary=gap, missing_sources=missing,
                           reason_kind="механически, без обращения к модели")
            return {"gaps": [gap], "evidence_enough": False,
                    "last_evidence_count": len(state.evidence),
                    "last_failed_count": len(state.failed_calls),
                    "gap_history": state.gap_history + [gap]}

        try:
            resp = deps.client.chat("M_balanced", prompt.build_messages(state),
                                    purpose="evidence_sufficiency",
                                    json_schema=prompt.SCHEMA, schema_name="sufficiency")
        except Exception as exc:                                   # noqa: BLE001
            if not judgement.is_judgement_failure(exc):
                raise
            # Оценка полноты не состоялась. Осторожный исход — «фактов не
            # хватает»: он ведёт либо к ещё одному кругу сбора, либо, если
            # лимиты исчерпаны, к честному отказу. Объявить базу достаточной
            # значило бы выдать непроверенное за проверенное.
            judgement.degrade(trace, "observe", exc, "фактов не хватает")
            stopper = limit_reached(cfg, trace, deps.client)
            gap = "оценка полноты не состоялась: ответ модели не разобран"
            return {"gaps": state.gaps + [gap], "evidence_enough": bool(stopper),
                    "limited_by": stopper or state.limited_by,
                    "last_evidence_count": len(state.evidence),
                    "last_failed_count": len(state.failed_calls),
                    "gap_history": state.gap_history + [gap]}
        data = resp.data or {}
        enough = bool(data.get("enough"))
        gaps = [g for g in (data.get("gaps") or []) if g]

        # Тот же пробел в третий раз — это не нехватка фактов, а тупик: источник
        # уже читали, ответ получен, и повторная просьба ничего не изменит.
        # Останавливаемся сами, чтобы вывод строился на собранном, а не обрывался
        # по лимиту с пустыми руками.
        if not enough and gaps:
            seen = [" ".join(str(g).lower().split())[:80] for g in state.gap_history]
            stuck = [g for g in gaps
                     if seen.count(" ".join(str(g).lower().split())[:80]) >= 2]
            if stuck:
                trace.decision(node="observe", action="stop_gap_repeated",
                               reason_summary=f"пробел «{_short(stuck[0])}» заявлен "
                                              f"третий раз, источник уже отвечал",
                               gaps=gaps)
                return {"gaps": gaps, "evidence_enough": True,
                        "limited_by": "повтор_пробела",
                        "last_evidence_count": len(state.evidence),
                        "last_failed_count": len(state.failed_calls),
                        "gap_history": state.gap_history + gaps}

        if not enough and stopper:
            trace.limit_hit(stopper, cfg.limits.get(stopper))
            trace.decision(node="observe", action="stop_on_limit",
                           reason_summary=f"фактов не хватает, но исчерпан лимит {stopper}")
            return {"gaps": gaps or state.gaps,
                    "evidence_enough": True,      # дальше не ищем, идём к выводу как есть
                    "last_evidence_count": len(state.evidence),
                    "last_failed_count": len(state.failed_calls),
                    "limited_by": stopper}

        trace.decision(node="observe",
                       action="evidence_sufficient" if enough else "continue_collecting",
                       reason_summary=data.get("reason_summary", ""),
                       missing_sources=data.get("missing_sources") or [], gaps=gaps)
        return {"gaps": gaps, "evidence_enough": enough,
                "last_evidence_count": len(state.evidence),
                "last_failed_count": len(state.failed_calls),
                "gap_history": state.gap_history + gaps}
