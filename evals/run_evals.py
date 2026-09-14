# -*- coding: utf-8 -*-
"""
Прогон золотого набора.

    python -m evals.run_evals                      # все кейсы текущим профилем
    python -m evals.run_evals --profile cloud
    python -m evals.run_evals --only A1,B1         # выборочно
    python -m evals.run_evals --group B            # одна группа
    python -m evals.run_evals --out evals/report   # куда положить отчёт

Отчёт пишется в Markdown и JSON. Markdown идёт в репозиторий как доказательство
работоспособности, JSON — для сравнения прогонов между собой.

Каждый кейс — отдельный прогон со своей трассой, поэтому после запуска в
`observability/runs/` остаётся по файлу на кейс: по ним видно не только что
агент ответил, но и как он к этому шёл.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from app.runner import run_question               # noqa: E402
from core.config import ConfigError, get_config   # noqa: E402
from evals.metrics import CaseResult, evaluate, summarize  # noqa: E402

CASES_PATH = Path(__file__).resolve().parent / "cases.yaml"



def _relative_trace(cfg, path: str | None) -> str | None:
    """Путь к трассе — относительно корня проекта, а не абсолютный.

    Отчёт лежит в репозитории и читается не только на той машине, где прогон
    шёл: `C:\\Users\\<имя>\\...` там и не воспроизводится, и сообщает о владельце
    машины больше, чем нужно. Относительный путь открывается у любого, кто
    склонировал проект.
    """
    if not path:
        return path
    try:
        return str(Path(path).resolve().relative_to(Path(cfg.root).resolve())).replace("\\", "/")
    except (ValueError, OSError):
        return Path(path).name

def load_cases(only: list[str] | None = None, group: str | None = None) -> list[dict]:
    data = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    defaults = data.get("defaults") or {}
    cases = []
    for case in data["cases"]:
        merged = {**defaults, **case}
        merged["задание"] = case.get("задание", defaults.get("task_file"))
        if only and merged["id"] not in only:
            continue
        if group and merged["группа"] != group:
            continue
        cases.append(merged)
    return cases


def run(cfg, cases: list[dict], make_client=None, quiet: bool = True) -> list[CaseResult]:
    results = []
    for i, case in enumerate(cases, start=1):
        started = time.perf_counter()
        print(f"  [{i}/{len(cases)}] {case['id']}  {case['вопрос'][:58]}…", flush=True)
        try:
            outcome = run_question(cfg, case["вопрос"], case["задание"], quiet=quiet,
                                   make_client=make_client)
        except Exception as exc:  # noqa: BLE001 — один упавший кейс не срывает прогон
            outcome = {"status": "error", "error": f"{type(exc).__name__}: {exc}",
                       "answer": "", "route": {}, "evidence": [], "metrics": {}}
        outcome.setdefault("метрики", outcome.get("metrics", {}))
        verdict = evaluate(case, outcome)
        verdict.факт["метрики"] = outcome.get("metrics", {})
        verdict.факт["длительность_мс"] = round((time.perf_counter() - started) * 1000)
        verdict.факт["трасса"] = _relative_trace(cfg, outcome.get("trace_file"))
        verdict.факт["ответ"] = outcome.get("answer", "")
        verdict.факт["обращение"] = outcome.get("ticket")
        results.append(verdict)
        if verdict.infra:
            print(f"        [СОРВАН] прогон не состоялся: "
                  f"{str(verdict.факт.get('error'))[:110]}")
        else:
            mark = "OK  " if verdict.passed else "СБОЙ"
            print(f"        [{mark}] {'; '.join(verdict.нарушения) or 'все проверки пройдены'}")
    return results


def render_markdown(results: list[CaseResult], summary: dict, profile: str) -> str:
    lines = [
        "# Отчёт по золотому набору",
        "",
        f"Профиль моделей: `{profile}`  ·  дата: {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "## Сводка",
        "",
        "| Метрика | Значение |",
        "|---|---|",
    ]
    for key, value in summary.items():
        if value is not None:
            lines.append(f"| {key} | {value} |")
    lines += ["", "## Кейсы", "",
              "| Кейс | Гр. | Вопрос | Намерение | Статус | Источники | Итог |",
              "|---|---|---|---|---|---|---|"]
    for r in results:
        mark = "срыв провайдера" if r.infra else ("прошёл" if r.passed else "**сбой**")
        lines.append(
            f"| {r.case_id} | {r.группа} | {r.вопрос[:44].strip()} | "
            f"{r.факт['намерение'] or '—'} | {r.факт['статус']} | "
            f"{', '.join(r.факт['источники']) or '—'} | {mark} |")

    broken = [r for r in results if r.infra]
    if broken:
        lines += ["", "## Сорвано провайдером", "",
                  "Эти прогоны не состоялись: модель роли была временно недоступна. "
                  "Это отказ инфраструктуры, а не результат агента, поэтому в метрики "
                  "качества такие кейсы не входят.", ""]
        for r in broken:
            lines.append(f"- **{r.case_id}** — {str(r.факт.get('error'))[:160]}")
        lines.append("")

    failed = [r for r in results if r.нарушения and not r.infra]
    if failed:
        lines += ["", "## Нарушения", ""]
        for r in failed:
            lines.append(f"**{r.case_id}** — {r.вопрос}")
            lines += [f"- {p}" for p in r.нарушения]
            lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="evals.run_evals",
                                description="Прогон агента по золотому набору кейсов")
    p.add_argument("--profile", help="Профиль моделей: local | hybrid | cloud")
    p.add_argument("--only", help="Идентификаторы кейсов через запятую")
    p.add_argument("--group", help="Одна группа: A, B, C, D или N")
    p.add_argument("--out", default="evals/report", help="Префикс файлов отчёта")
    p.add_argument("--verbose", action="store_true", help="Показывать шаги прогонов")
    args = p.parse_args(argv)

    try:
        cfg = get_config(args.profile)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    only = [x.strip() for x in args.only.split(",")] if args.only else None
    cases = load_cases(only=only, group=args.group)
    if not cases:
        print("Под фильтр не попал ни один кейс", file=sys.stderr)
        return 2

    print(f"Профиль: {cfg.profile}   кейсов: {len(cases)}\n")
    results = run(cfg, cases, quiet=not args.verbose)
    summary = summarize(results)

    print("\nСводка")
    for key, value in summary.items():
        if value is not None:
            print(f"  {key:<32} {value}")

    out = cfg.root / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".md").write_text(
        render_markdown(results, summary, cfg.profile), encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(
        {"профиль": cfg.profile, "дата": datetime.now().isoformat(timespec="seconds"),
         "сводка": summary,
         "кейсы": [{"id": r.case_id, "группа": r.группа, "вопрос": r.вопрос,
                    "ожидание": r.ожидание, "факт": {k: v for k, v in r.факт.items()
                                                     if k != "ответ"},
                    "проверки": r.проверки, "нарушения": r.нарушения}
                   for r in results]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nОтчёт: {out.with_suffix('.md')}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
