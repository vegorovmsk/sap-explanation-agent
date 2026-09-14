# -*- coding: utf-8 -*-
"""
Узел check_consistency — сверка источников между собой.

Самое рискованное место всего агента: именно здесь можно объявить ошибкой то,
что ошибкой не является. Поэтому сверка отдана сильной модели, а её вывод
ограничен тремя исходами, и ни один из них не звучит как «в системе баг».
Противоречие ведёт не к вердикту, а к черновику обращения в поддержку.
"""
from __future__ import annotations

from agent import locators
from agent.deps import Deps
from agent.nodes import judgement
from agent.prompts import consistency as prompt
from agent.state import AgentState, Conflict
from memory.sparse import terms

LABEL = {0.75: "высокая", 0.45: "средняя"}

# Классы вопросов, где расхождение — это ответ, а не побочное наблюдение. В
# остальных пропуск, найденный картой, не поднимается: вопрос «где стоит заказ»
# не должен внезапно обрастать претензией к регламенту кольцевания.
CHECKING_INTENTS = ("DOC_CODE_CONSISTENCY_CHECK", "NSI_PLAN_CONSISTENCY_CHECK")


def _where_it_belonged(state: AgentState, clause: str, read_code: list[str]) -> str:
    """Из прочитанных мест кода — то, которое ближе всего к норме.

    Ближе всего то, что читает таблицу, на которую ссылается сам пункт: связь
    «пункт → таблица → функция» лежит в графе и в карте обхода. Для ТР-КОЛ п. 5.1
    это место, где загружается табл. 20 с колонкой «Диаметр кольца» — то есть
    колонка в коде есть, а в отборе не участвует. Без этой связки пришлось бы
    показывать первую попавшуюся функцию, и обращение в поддержку указывало бы
    в случайную строку.
    """
    for entry in (state.stage_map or {}).get("таблицы") or []:
        if clause not in (entry.get("пункты") or []):
            continue
        linked = [c for c in (entry.get("функции") or []) if c in read_code]
        if linked:
            return linked[0]
    return read_code[0]


def _grounded_omission(state: AgentState, item: dict) -> Conflict | None:
    """Расхождение-пропуск, у которого обе стороны прочитаны.

    Расхождение вида «норма требует параметр, а код его не использует» нельзя
    подтвердить положительной координатой: указывать не на что, там пусто.
    Механическая проверка ниже справедливо снимала такой вердикт — прогон 14.09,
    кейс B2: модель верно нашла, что ТР-КОЛ п. 5.1 требует учитывать диаметр
    кольца, а отбор линии идёт по одному калибру, и вердикт был снят как
    необоснованный. Эскалация по всему набору осталась нулевой.

    Обосновать пропуск всё-таки можно, но только двумя прочитанными местами:
    пунктом, который требует, и функцией, которой полагалось его исполнять.
    Если агент не открывал ни того, ни другого, он не вправе утверждать, что
    чего-то нет, — и расхождение не поднимается. Само отсутствие находит код
    обхода, а не модель: сравнение множеств параметров механическое.
    """
    known = state.locators()
    clause = item.get("координата_нормы") or item.get("пункт") or ""
    if not clause or locators.unknown([clause], known):
        return None
    read_code = [c for c in (item.get("координаты_кода") or [])
                 if not locators.unknown([c], known)]
    if not read_code:
        return None
    param = item.get("параметр", "")
    where = ", ".join(read_code[:3])
    return Conflict(
        subject=param,
        expected=f"норма требует учитывать «{param}»",
        expected_source=clause,
        actual=f"в прочитанном коде этапа «{param}» не встречается ({where})",
        actual_source=_where_it_belonged(state, clause, read_code),
        severity="medium")


def _relevant(state: AgentState, item: dict) -> bool:
    """Относится ли найденный пропуск к заданному вопросу."""
    if state.intent in CHECKING_INTENTS:
        return True
    param_stems = set(terms(item.get("параметр", "")))
    return bool(param_stems) and param_stems <= set(terms(state.question))


def confidence_label(value: float) -> str:
    if value >= 0.75:
        return "высокая"
    if value >= 0.45:
        return "средняя"
    return "низкая"


def check_consistency(state: AgentState, deps: Deps) -> dict:
    cfg, trace = deps.cfg, deps.trace
    role = cfg.routing["escalation"]["on_conflict"]["role"]

    with trace.step("check_consistency", role=role):
        try:
            resp = deps.client.chat(role, prompt.build_messages(state),
                                    purpose="cross_source_check",
                                    json_schema=prompt.SCHEMA, schema_name="consistency")
            data = resp.data or {}
        except Exception as exc:                                   # noqa: BLE001
            if not judgement.is_judgement_failure(exc):
                raise
            # Сверка не состоялась — вердикта нет. Не «подтверждено» и не
            # «расхождение»: и то и другое было бы утверждением, которого никто
            # не делал. Собранные факты при этом не пропадают, пользователь
            # получит их вместе с признанием, что сверка не прошла.
            judgement.degrade(trace, "check_consistency", exc,
                              "вердикт не выносится, статус insufficient")
            # Причина обязана дойти до пользователя, а не остаться в трассе:
            # ветка insufficient печатает именно пробелы, и без этой строки
            # человек прочитал бы «доказательств недостаточно» и решил, что
            # источников не хватило, — тогда как источники прочитаны, а не
            # состоялась сверка.
            degraded_gap = ("сверка источников не состоялась: ответ модели "
                            "не разобран, вердикт не выносится")
            return {"status": "insufficient", "confidence": 0.0,
                    "confidence_label": confidence_label(0.0), "conflicts": [],
                    "gaps": state.gaps + [degraded_gap]}
    status = data.get("status") or "insufficient"
    confidence = float(data.get("confidence") or 0.0)
    conflicts = [Conflict(**c) for c in (data.get("conflicts") or [])]

    # Механическая проверка расхождений: у каждой стороны конфликта должна быть
    # координата из доказательной базы. Без неё «противоречие» — это не спор
    # источников, а пересказ ожиданий пользователя, принятых за источник.
    # Живой прогон дал ровно такой случай: модель верно объяснила, что ЛП2
    # поддерживает только Флексо-2 и Цифровую, и назвала это конфликтом.
    known = state.locators()
    grounded, ungrounded = [], []
    for c in conflicts:
        if locators.unknown([c.expected_source, c.actual_source], known):
            ungrounded.append(c)
        else:
            grounded.append(c)
    # Прежде чем снять необоснованное расхождение, проверим, не пропуск ли это:
    # у пропуска положительной координаты нет по природе, но карта обхода знает
    # и норму, и место в коде, где ей полагалось быть.
    uncovered = (state.stage_map or {}).get("непокрытые_условия") or []
    rescued: list[Conflict] = []
    for c in list(ungrounded):
        match = next((u for u in uncovered
                      if set(terms(u.get("параметр", ""))) & set(terms(c.subject))), None)
        fixed = _grounded_omission(state, match) if match else None
        if fixed:
            ungrounded.remove(c)
            rescued.append(fixed)
            trace.decision(node="check_consistency", action="omission_grounded",
                           reason_summary=f"пропуск «{fixed.subject}» подтверждён "
                                          f"прочитанными местами: {fixed.expected_source} "
                                          f"и {fixed.actual_source}")
    if ungrounded:
        trace.event("consistency.ungrounded_conflicts", node="check_consistency",
                    dropped=[c.subject for c in ungrounded][:5], status="dropped")
    conflicts = grounded + rescued

    # Пропуск, который модель не назвала вовсе. Находит его код обхода, а не
    # суждение: если норма требует параметр, код этапа его не читает, и агент
    # открыл оба места — расхождение есть независимо от того, заметила ли его
    # модель. Это и есть «вердикт связывает код».
    named = {s for c in conflicts for s in terms(c.subject)}
    for u in uncovered:
        if set(terms(u.get("параметр", ""))) & named or not _relevant(state, u):
            continue
        found = _grounded_omission(state, u)
        if found:
            conflicts.append(found)
            status = "conflict"
            trace.decision(node="check_consistency", action="omission_found_by_code",
                           reason_summary=f"обход нашёл непокрытое условие "
                                          f"«{found.subject}»: {found.expected_source} "
                                          f"требует, {found.actual_source} не исполняет",
                           missed_by_model=True)

    if status == "conflict" and not conflicts:
        status = "confirmed" if confidence >= float(cfg.thresholds["min_confidence"]) \
            else "insufficient"
        trace.decision(node="check_consistency", action="conflict_not_grounded",
                       reason_summary="расхождение не подтверждено координатами "
                                      "источников, вердикт снят")

    threshold = float(cfg.thresholds["min_confidence"])
    if status == "confirmed" and confidence < threshold:
        # низкая уверенность не даёт права на утвердительный ответ
        status = "insufficient"
        trace.decision(node="check_consistency", action="downgrade_to_insufficient",
                       reason_summary=f"уверенность {confidence:.2f} ниже порога {threshold}")

    trace.decision(node="check_consistency", action=status,
                   reason_summary=data.get("reason_summary", ""),
                   confidence=round(confidence, 2), conflicts=len(conflicts))
    return {"status": status, "confidence": confidence,
            "confidence_label": confidence_label(confidence), "conflicts": conflicts}
