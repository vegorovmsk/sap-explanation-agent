# -*- coding: utf-8 -*-
"""
Проверка самого прогона evals — на сценарной модели.

Смысл: убедиться, что метрики ловят нарушения, а не только красиво печатают
сводку. Один и тот же кейс прогоняется дважды — с добросовестным поведением
модели и с недобросовестным, — и проверяется, что во втором случае каждое
нарушение названо.

Запуск: python -m tests.test_evals   (из корня sap_agent)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Тесты обязаны быть автономными. QDRANT_URL сбрасывается ЖЁСТКО, а не через
# setdefault: как только в .env появляется адрес сервера, он перебивает путь
# (resolve_location предпочитает url), и набор начинает требовать запущенный
# Docker. База в памяти ничего снаружи не ждёт и ничего не оставляет после себя.
os.environ["QDRANT_URL"] = ""
os.environ["QDRANT_PATH"] = ":memory:"

from core.config import get_config              # noqa: E402
from evals.metrics import summarize             # noqa: E402
from evals.run_evals import load_cases, render_markdown, run  # noqa: E402
from memory import build as memory_build        # noqa: E402
from memory.search import reset_cache           # noqa: E402
from tests.test_graph import ScriptedLLM        # noqa: E402

FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if condition else 'СБОЙ'}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILED.append(name)


ENTITIES = {k: None for k in
            ("order_number", "left_neighbor", "right_neighbor", "stage", "line", "kind",
             "sort", "caliber", "color", "print_type", "nsi_table", "due_date")}


def script(*, intent, tools, status, summary, explanation, cited, conflicts=(),
           verify=None):
    return {
        "intent+entities": {"intent": intent,
                            "entities": {**ENTITIES, "order_number": "Z-1060", "line": "ЛП2"},
                            "ambiguity": {"is_ambiguous": False, "question": None},
                            "reason_summary": "разбор"},
        "collect_evidence": tools,
        "evidence_sufficiency": {"enough": True, "missing_sources": [], "gaps": [],
                                 "reason_summary": "ок"},
        "cross_source_check": {"status": status, "confidence": 0.9,
                               "conflicts": list(conflicts), "reason_summary": "ок"},
        "generate_answer": {"summary": summary, "explanation": explanation,
                            "cited_locators": cited, "confidence": "высокая"},
        "draft_support_ticket": {"title": "t", "expected": "e", "actual": "a",
                                 "evidence_locators": cited, "impact": "i",
                                 "severity": "средняя", "reproduce": "r"},
        "verify_answer": verify or {"ok": True, "unsupported": [],
                                    "verdict_summary": "ок"},
    }


def main() -> int:
    cfg = get_config(reload=True)
    # Прогон evals пишет трассы туда, куда указывает конфиг. В тесте это должен
    # быть временный каталог: иначе сценарные прогоны попадают в observability/runs
    # вперемешку с живыми и мешают разбирать настоящие.
    cfg.obs["runs_dir"] = tempfile.mkdtemp(prefix="sap-agent-evals-test-")
    reset_cache()
    memory_build.build(cfg, force_fallback=True, quiet=True)
    case = load_cases(only=["A1"])[0]

    print("\nНабор кейсов")
    all_cases = load_cases()
    check("золотой набор загружается целиком", len(all_cases) >= 14, str(len(all_cases)))
    check("все группы представлены",
          {c["группа"] for c in all_cases} == {"A", "B", "C", "D", "N"},
          str(sorted({c["группа"] for c in all_cases})))
    check("у каждого кейса есть задание и допустимые статусы",
          all(c.get("задание") and c.get("статус") for c in all_cases))

    print("\nДобросовестный прогон кейса A1")
    good = script(
        intent="ORDER_EQUIPMENT_EXPLANATION",
        tools=[("read_task", {"order_number": "Z-1060"}),
               ("read_plan", {"order_number": "Z-1060"}),
               ("lookup_nsi", {"table": "18", "filters": {"Вид печати": "Флексо-4"}})],
        status="confirmed",
        summary="Флексо-4 допускается только на ЛП1 и ЛП3.",
        explanation="По табл. 18 вид печати Флексо-4 допускается на ЛП1 и ЛП3.",
        cited=[])
    results = run(cfg, [case], make_client=lambda c, t: ScriptedLLM(good))
    r = results[0]
    check("кейс пройден", r.passed, "; ".join(r.нарушения))
    check("намерение и статус совпали с эталоном",
          r.проверки.get("намерение") and r.проверки.get("статус"))
    check("обязательные источники прочитаны", r.проверки.get("источники"))
    check("обязательная координата прозвучала",
          r.проверки.get("обязательные источники названы"))

    print("\nНедобросовестный прогон того же кейса")
    bad = script(
        intent="ORDER_LOOKUP",                       # не тот маршрут
        tools=[("read_plan", {"order_number": "Z-1060"})],   # НСИ не читалась
        status="conflict",                            # статус вне допустимых
        summary="Линия ЛП2 была занята профилактикой.",      # запрещённая формулировка
        explanation="Согласно [табл. 99 «Графики ремонтов», строка 7] линия ЛП2 в ремонте.",
        cited=["табл. 99 «Графики ремонтов», строка 7"],
        conflicts=[{"subject": "s", "expected": "e", "expected_source": "выдумка",
                    "actual": "a", "actual_source": "выдумка", "severity": "medium"}],
        # Механическое нарушение — ссылка вне доказательной базы — снимает
        # вердикт в любой момент, и статус падает до insufficient: для кейса A1
        # это статус вне допустимых. Одно лишь мнение рецензента вердикт не
        # снимает, поэтому сценарий опирается именно на координату.
        verify={"ok": False, "unsupported": [],
                "verdict_summary": "ссылка вне доказательной базы"})
    results_bad = run(cfg, [case], make_client=lambda c, t: ScriptedLLM(bad))
    rb = results_bad[0]
    text = "; ".join(rb.нарушения)
    check("кейс не пройден", not rb.passed)
    check("замечен неверный маршрут", "намерение" in text, text[:60])
    check("замечен непрочитанный источник", "не просмотрены источники" in text)
    check("замечен недопустимый статус", "статус" in text)
    check("замечена запрещённая формулировка", "недопустимое" in text)
    check("замечена ссылка вне доказательной базы",
          "вне доказательной базы" in text or "не сослался" in text, text[:120])

    print("\nСводка и отчёт")
    summary = summarize(results + results_bad)
    check("сводка считает пройденные кейсы", summary["пройдено"] == 1, str(summary["пройдено"]))
    check("точность маршрута посчитана как доля",
          summary["точность маршрута"] == 0.5, str(summary["точность маршрута"]))
    check("латентность измерена", summary["латентность P50, с"] is not None)
    md = render_markdown(results + results_bad, summary, cfg.profile)
    check("отчёт содержит сводку и раздел нарушений",
          "## Сводка" in md and "## Нарушения" in md)
    check("в отчёте названы оба кейса", md.count("| A1 |") == 2, str(md.count("| A1 |")))

    print()
    if FAILED:
        print(f"Провалено проверок: {len(FAILED)}")
        for f in FAILED:
            print(f"  · {f}")
        return 1
    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
