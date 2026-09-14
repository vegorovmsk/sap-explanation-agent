# -*- coding: utf-8 -*-
"""
Метрики прогона по золотому набору.

Проверяется обоснованность, а не гладкость текста. Все метрики считаются по
машинно проверяемым признакам: какой маршрут выбран, какие источники прочитаны,
на что агент сослался и совпал ли статус с эталоном. Единственное исключение —
запрещённые формулировки: там простой поиск подстроки, зато он ловит именно то,
ради чего всё затевалось, — правдоподобную выдумку.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from agent import locators


@dataclass
class CaseResult:
    case_id: str
    группа: str
    вопрос: str
    ожидание: dict
    факт: dict                       # результат run_question
    проверки: dict = field(default_factory=dict)
    нарушения: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.нарушения

    @property
    def infra(self) -> bool:
        """Прогон не состоялся по вине инфраструктуры, а не агента.

        Различать обязательно. Прогон 13.09 упёрся в недоступность моделей у
        OpenRouter: 14 кейсов из 19 не начались вовсе, а сводка показала
        «точность маршрута 0.222» — то есть измерила доступность провайдера и
        выдала это за качество агента. Такие кейсы считаются отдельно и в
        метрики качества не попадают.
        """
        err = str(self.факт.get("error") or "")
        return self.факт.get("status") == "error" and (
            "ProviderUnavailable" in err or "недоступн" in err.lower()
            or "No endpoints found" in err or "429" in err)


def evaluate(case: dict, result: dict) -> CaseResult:
    """Сверяет один прогон с эталоном кейса."""
    checks: dict[str, bool] = {}
    problems: list[str] = []
    answer = (result.get("answer") or "") + " " + (result.get("ticket") or "")
    low = answer.lower()
    route = result.get("route") or {}
    seen = set(route.get("sources_seen") or [])
    status = result.get("status")

    # --- маршрут
    # Намерение может быть списком: для части вопросов класс из самого вопроса
    # однозначно не выводится, а два разных класса ведут к одному набору
    # источников и одному ответу. Требовать конкретную метку там — значит мерить
    # не маршрут, а угадывание ответа до чтения данных.
    expected_intent = case.get("намерение")
    allowed_intents = ([expected_intent] if isinstance(expected_intent, str)
                       else list(expected_intent or []))
    if allowed_intents:
        ok = result.get("intent") in allowed_intents
        checks["намерение"] = ok
        if not ok:
            problems.append(f"намерение {result.get('intent')} вместо "
                            + " / ".join(allowed_intents))
    expected_role = case.get("маршрут_роль")
    if expected_role:
        ok = route.get("role") == expected_role
        checks["роль модели"] = ok
        if not ok:
            problems.append(f"роль {route.get('role')} вместо {expected_role}")

    # --- полнота источников
    required = set(case.get("источники") or [])
    if required:
        missing = sorted(required - seen)
        checks["источники"] = not missing
        if missing:
            problems.append("не просмотрены источники: " + ", ".join(missing))

    # --- статус
    allowed = list(case.get("статус") or [])
    if allowed:
        ok = status in allowed
        checks["статус"] = ok
        if not ok:
            problems.append(f"статус {status}, допустимы {allowed}")

    # --- доказательность: каждая процитированная координата есть в базе
    known = {e["locator"] for e in (result.get("evidence") or []) if e.get("locator")}
    cited = list((result.get("verification") or {}).get("unknown_locators") or [])
    checks["ссылки только на прочитанное"] = not cited
    if cited:
        problems.append("ссылки вне доказательной базы: " + "; ".join(cited[:3]))

    # --- обязательные координаты прозвучали
    must = case.get("обязательно_сослаться") or []
    missed = [m for m in must if m.lower() not in low
              and not any(m.lower() in k.lower() for k in known)]
    if must:
        checks["обязательные источники названы"] = not missed
        if missed:
            problems.append("не сослался на: " + ", ".join(missed))

    # --- запрещённые формулировки
    forbidden = [f for f in (case.get("не_должно_прозвучать") or []) if f.lower() in low]
    checks["нет запрещённых утверждений"] = not forbidden
    if forbidden:
        problems.append("прозвучало недопустимое: " + ", ".join(forbidden))

    # --- эскалация
    if case.get("ожидается_обращение"):
        ok = bool(result.get("ticket"))
        checks["обращение в поддержку"] = ok
        if not ok:
            problems.append("расхождение не превращено в обращение в поддержку")

    return CaseResult(case_id=case["id"], группа=case["группа"], вопрос=case["вопрос"],
                      ожидание={"намерение": expected_intent, "статус": allowed,
                                "источники": sorted(required)},
                      факт={"намерение": result.get("intent"), "статус": status,
                            "источники": sorted(seen)},
                      проверки=checks, нарушения=problems)


def summarize(results: list[CaseResult]) -> dict:
    """Сводные метрики по всем кейсам."""
    if not results:
        return {}

    # Качество считаем по состоявшимся прогонам. Иначе первая же недоступность
    # провайдера превращает отчёт в измерение чужого аптайма.
    infra = [r for r in results if r.infra]
    scored = [r for r in results if not r.infra] or results

    def share(key: str) -> float | None:
        vals = [r.проверки[key] for r in scored if key in r.проверки]
        return round(sum(vals) / len(vals), 3) if vals else None

    durations = [r.факт.get("длительность_мс") or (r.факт.get("метрики") or {}).get("duration_ms", 0)
                 for r in scored]
    durations = [d for d in durations if d]
    costs = [(r.факт.get("метрики") or {}).get("cost_usd", 0) for r in results]

    out = {
        "кейсов": len(results),
        "прогонов состоялось": len(scored),
        "пройдено": sum(r.passed for r in scored),
        "точность маршрута": share("намерение"),
        "полнота источников": share("источники"),
        "корректность статуса": share("статус"),
        "доказательность": share("ссылки только на прочитанное"),
        "обязательные источники названы": share("обязательные источники названы"),
        "нет запрещённых утверждений": share("нет запрещённых утверждений"),
        "эскалация при расхождении": share("обращение в поддержку"),
        "латентность P50, с": round(statistics.median(durations) / 1000, 1) if durations else None,
        "латентность P95, с": (round(sorted(durations)[int(len(durations) * 0.95) - 1] / 1000, 1)
                               if len(durations) >= 2 else None),
        "стоимость всего, $": round(sum(costs), 4) if costs else None,
    }
    if infra:
        out["СОРВАНО ПРОВАЙДЕРОМ"] = len(infra)
        out["метрики качества считаны по"] = f"{len(scored)} из {len(results)} кейсов"
    return out
