# -*- coding: utf-8 -*-
"""
Узел verify — проверка ответа перед выдачей.

Два слоя, и механический идёт первым. Сначала сверяются координаты: в тексте
ответа не должно быть ссылок, которых нет в доказательной базе — это проверяется
кодом, без модели и без права на ошибку. Потом сильная модель читает ответ как
рецензент и ищет утверждения, не опирающиеся ни на один факт.

Найденные нарушения не переписывают ответ молча: они дописываются оговоркой.
Пользователь должен видеть, что часть вывода не подтверждена, а не получить
отредактированный текст без следов правки.

Вердикт снимается не любым замечанием. Механическое нарушение — ссылка на
координату вне доказательной базы — снимает его всегда: это не вопрос вкуса.
Замечание рецензента снимает вердикт только если оно ключевое, то есть без
спорного утверждения объяснение не держится. Иначе выходит то, что показал
прогон золотого набора: семь верных разборов из семи забракованы за придирку к
формулировке, и «уверенность 0.9, статус insufficient» в одной строке.
"""
from __future__ import annotations

from agent import locators
from agent.deps import Deps
from agent.nodes import judgement
from agent.prompts import verify as prompt
from agent.state import AgentState

def _unknown_locators(text: str, known: set[str]) -> list[str]:
    """Разбор координат в тексте — общий с узлом answer, чтобы правила совпадали."""
    return sorted(locators.unknown(locators.in_text(text), known))


LOWER = {"высокая": "средняя", "средняя": "низкая", "низкая": "низкая"}


def _lower(label: str) -> str:
    return LOWER.get(label or "", "низкая")


def _findings(raw) -> list[dict]:
    """Замечания рецензента в едином виде: текст, значимость, проверенные факты.

    Схема требует объект, но модель может отдать и просто строку — старый формат
    или сбой деградации до json_object. Такой случай считаем ключевым:
    неизвестная значимость не повод ослаблять проверку.
    """
    out = []
    for item in (raw or []):
        if isinstance(item, str):
            claim, severity, checked = item.strip(), "ключевое", []
        elif isinstance(item, dict):
            claim = str(item.get("claim") or "").strip()
            severity = item.get("severity") or "ключевое"
            checked = [c for c in (item.get("checked_facts") or [])
                       if isinstance(c, int)]
        else:
            continue
        if claim:
            out.append({"claim": claim,
                        "severity": "второстепенное" if severity == "второстепенное"
                        else "ключевое",
                        "checked_facts": checked})
    return out


def verify(state: AgentState, deps: Deps) -> dict:
    cfg, trace = deps.cfg, deps.trace
    role = cfg.routing["escalation"]["verify_answer"]["role"]
    known = state.locators()

    unknown = _unknown_locators(state.answer_explanation, known)
    if unknown:
        trace.event("verify.unknown_locators", node="verify", locators=unknown[:5],
                    status="flagged")

    degraded = False
    with trace.step("verify", role=role):
        try:
            resp = deps.client.chat(role, prompt.build_messages(state, state.answer),
                                    purpose="verify_answer",
                                    json_schema=prompt.SCHEMA, schema_name="verification")
            data = resp.data or {}
        except Exception as exc:                                   # noqa: BLE001
            if not judgement.is_judgement_failure(exc):
                raise
            # Рецензент не ответил. Готовый ответ из-за этого не выбрасывается —
            # он уже прошёл механические проверки, и они сильнее мнения модели.
            # Но и «проверено» сказать нельзя: пользователю дописывается
            # оговорка, что проверка не состоялась. Молчаливое одобрение здесь
            # было бы худшим из возможных исходов.
            judgement.degrade(trace, "verify", exc,
                              "ответ сохранён, дописана оговорка о непроведённой проверке")
            data, degraded = {}, True
    findings = _findings(data.get("unsupported"))

    # Замечание принимается только вместе с проверкой: рецензент обязан назвать
    # номера фактов, в которых искал утверждение. Пустой список означает, что он
    # не искал, — и прогон золотого набора показал, чем это кончается: на простом
    # вопросе «где заказ Z-1001» рецензент объявил неподтверждённым всё, хотя
    # линия, время и координата строки лежали в фактах.
    ungrounded = [f for f in findings if not f["checked_facts"]]
    if ungrounded and state.evidence:
        trace.event("verify.ungrounded_findings", node="verify",
                    claims=[f["claim"][:80] for f in ungrounded][:5], status="dropped")
        findings = [f for f in findings if f["checked_facts"]]
    critical = [f for f in findings if f["severity"] == "ключевое"]
    # Вердикт выводится из того, что осталось после проверок, а не из флага
    # модели: рецензент, поставивший ok=false и не назвавший ни одного
    # обоснованного замечания, не сказал ничего, что можно предъявить.
    ok = not unknown and not findings and not state.struck_claims and not degraded

    report = {"ok": ok, "degraded": degraded,
              "unsupported": [f["claim"] for f in findings],
              "struck_claims": state.struck_claims,
              "critical": [f["claim"] for f in critical],
              "unknown_locators": unknown, "verdict": data.get("verdict_summary", "")}
    updates: dict = {"verification": report}

    if ok:
        trace.decision(node="verify", action="answer_approved",
                       reason_summary=data.get("verdict_summary",
                                               "каждое утверждение опирается на факт"))
        return updates

    # Первое ключевое замечание не снимает вердикт, а возвращает ответ на
    # переписывание (обратное ребро verify → answer). Понижать статус здесь
    # нельзя: переписанный ответ будет собран по шаблону уже с этим статусом,
    # и чистая правка всё равно выглядела бы отказом.
    # Условие обязано совпадать с router.after_verify: иначе узел объявляет
    # возврат на переписывание, роутер его не делает, и замечания пропадают
    # молча — ни оговорки в ответе, ни понижения вердикта. Живой прогон дал это
    # на ветке обращения в поддержку: решение в трассе было, последствий не было.
    if critical and state.answer_revisions == 0 and state.ticket is None:
        trace.decision(node="verify", action="answer_returned_for_revision",
                       reason_summary=data.get("verdict_summary",
                                               "есть утверждения без опоры на факты"),
                       critical=len(critical), unsupported=len(findings))
        return updates

    if degraded:
        # Ни замечаний, ни одобрения: сказать можно только то, что проверка не
        # прошла. Механические проверки координат при этом отработали — они в
        # коде и от модели не зависят, — поэтому вердикт не снимается.
        trace.decision(node="verify", action="verification_unavailable",
                       reason_summary="проверка ответа не состоялась, "
                                      "механические проверки координат пройдены")
        updates["answer"] = state.answer + (
            "\n\nПроверка ответа моделью-рецензентом не состоялась (сбой провайдера). "
            "Механическая сверка координат пройдена: все ссылки ведут на прочитанные "
            "источники.")
        return updates

    note_lines = ["", "Проверка ответа: часть утверждений не подтверждена источниками."]
    note_lines += [f"- {f['claim']}" for f in findings[:5]]
    note_lines += [f"- ссылка на источник вне доказательной базы: {u}" for u in unknown[:3]]
    if state.struck_claims:
        note_lines.append(f"- вычеркнуто утверждений без основания: {state.struck_claims}")
    updates["answer"] = state.answer + "\n" + "\n".join(note_lines)

    # Вердикт снимают механическое нарушение — всегда — и ключевое замечание,
    # но только ПЕРВОЕ. Рецензенту даётся ровно один рычаг: вернуть ответ на
    # переписывание. Дальше его мнение совещательное.
    #
    # Так решено не из снисходительности, а по данным. В прогоне по заказу
    # A-3063 сверка источников дала «confirmed», желаемая и расчётная даты
    # лежали в фактах с координатой «Все_ПП, строка 12», а рецензент дважды
    # заявил, что подтверждения опоздания нет, — и верный разбор получил
    # «insufficient». Механические проверки — координаты и номера проверенных
    # фактов — остаются жёсткими и снимают вердикт в любой момент; суждение
    # модели после уже использованной попытки правки только понижает
    # уверенность и дописывает оговорку.
    # Вычеркнутое утверждение — такое же механическое нарушение, как ссылка вне
    # базы, просто пойманное раньше, в узле ответа: модель сослалась на
    # непрочитанный источник и не отступилась после прямой просьбы. К моменту
    # проверки координаты в тексте уже нет, и по ней судить нельзя — судим по
    # факту вычёркивания.
    mechanical = bool(unknown) or state.struck_claims > 0
    advisory = bool(critical) and state.answer_revisions > 0 and not mechanical
    if advisory:
        trace.decision(node="verify", action="answer_flagged_advisory",
                       reason_summary="замечание после переписывания: вердикт не "
                                      "снимается, оговорка дописана",
                       critical=len(critical))
    if mechanical or (critical and not advisory):
        updates["confidence_label"] = "низкая"
        if state.status == "confirmed":
            updates["status"] = "insufficient"
        action = "answer_flagged"
    else:
        updates["confidence_label"] = _lower(state.confidence_label)
        action = "answer_flagged_minor"
    trace.decision(node="verify", action=action,
                   reason_summary=data.get("verdict_summary",
                                           "найдены неподтверждённые утверждения"),
                   unsupported=len(findings), critical=len(critical),
                   unknown_locators=len(unknown))
    return updates
