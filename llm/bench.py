# -*- coding: utf-8 -*-
"""
Замеры моделей: чем обосновывается выбор в config/models.yaml.

    python -m llm.bench                        # все модели доступных провайдеров
    python -m llm.bench --models or_fast,or_balanced
    python -m llm.bench --repeat 2 --out docs/bench

Меряются не общие способности, а ровно те, от которых зависит работа агента:

  * **разбор запроса** — доля верно определённых намерений и извлечённых номеров
    заказов при строгом выводе по JSON-схеме. Здесь модели ломаются чаще всего:
    либо возвращают не тот класс, либо вовсе не держат схему;
  * **вызов инструмента** — предлагает ли модель нужный инструмент и с
    осмысленными ли аргументами. Без этого агент не соберёт ни одного факта;
  * **латентность и стоимость** — по ним решается, какая роль кому достанется.

Итог — таблица в Markdown рядом с отчётом evals. Она и есть ответ на вопрос
«почему в конфиге именно эти модели»: не «показались лучше», а измерено.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.prompts import classify as classify_prompt   # noqa: E402
from core.config import ConfigError, get_config         # noqa: E402
from llm.client import LLMClient                        # noqa: E402
from tools.registry import openai_tools                 # noqa: E402

# Вопросы с заранее известным разбором. Взяты из золотого набора, чтобы замер
# мерил то же, что потом проверяют evals.
РАЗБОР = [
    ("Почему заказ Z-1060 не поставлен на линию ЛП2?",
     "ORDER_EQUIPMENT_EXPLANATION", "Z-1060"),
    ("Где находится заказ Z-1001?", "ORDER_LOOKUP", "Z-1001"),
    ("Почему заказ Z-1040 готов позже плановой даты?",
     "ORDER_DELAY_EXPLANATION", "Z-1040"),
    ("Соответствует ли порядок переходов по калибру регламенту?",
     "DOC_CODE_CONSISTENCY_CHECK", None),
    ("Почему у заказа Z-1061 замечание НСИ по срокам хранения?",
     "DATA_VALIDATION", "Z-1061"),
]

ИНСТРУМЕНТЫ = [
    ("Найди заказ Z-1060 в результате расчёта.", {"read_plan"}),
    ("На каких линиях допускается вид печати Флексо-4?", {"lookup_nsi", "list_nsi_tables"}),
]
ДОСТУПНЫЕ_ИНСТРУМЕНТЫ = ["read_task", "read_plan", "lookup_nsi", "list_nsi_tables"]


def _probe_parse(client: LLMClient, ref: str, cfg) -> dict:
    intents = cfg.routing["intents"]
    schema = classify_prompt.schema_for(intents)
    верно_намерение = верно_заказ = валидный_json = 0
    задержки: list[int] = []
    стоимость = 0.0
    ошибки: list[str] = []

    for вопрос, намерение, заказ in РАЗБОР:
        try:
            r = client.chat("M_fast", classify_prompt.build_messages(
                вопрос, cfg.stand.default_task, intents),
                purpose="bench:parse", json_schema=schema, schema_name="intent",
                model_ref=ref, max_tokens=512)
        except Exception as exc:  # noqa: BLE001
            ошибки.append(f"{type(exc).__name__}: {exc}")
            continue
        задержки.append(r.latency_ms)
        стоимость += r.cost_usd
        data = r.data or {}
        if data:
            валидный_json += 1
        верно_намерение += data.get("intent") == намерение
        извлечён = (data.get("entities") or {}).get("order_number")
        верно_заказ += (извлечён == заказ) if заказ else (извлечён in (None, ""))

    всего = len(РАЗБОР)
    return {"json": валидный_json / всего, "намерение": верно_намерение / всего,
            "заказ": верно_заказ / всего, "задержки": задержки,
            "стоимость": стоимость, "ошибки": ошибки}


def _probe_tools(client: LLMClient, ref: str) -> dict:
    попаданий = вызовов = 0
    задержки: list[int] = []
    стоимость = 0.0
    ошибки: list[str] = []
    примеры: list[str] = []

    for вопрос, ожидаемые in ИНСТРУМЕНТЫ:
        try:
            r = client.chat("M_balanced", [
                {"role": "system", "content": "Ты читаешь данные системы планирования "
                                              "производства. Пользуйся инструментами, "
                                              "не отвечай по памяти."},
                {"role": "user", "content": вопрос}],
                purpose="bench:tools", tools=openai_tools(ДОСТУПНЫЕ_ИНСТРУМЕНТЫ),
                model_ref=ref, max_tokens=512)
        except Exception as exc:  # noqa: BLE001
            ошибки.append(f"{type(exc).__name__}: {exc}")
            continue
        задержки.append(r.latency_ms)
        стоимость += r.cost_usd
        if r.tool_calls:
            вызовов += 1
            имена = {c.name for c in r.tool_calls}
            попаданий += bool(имена & ожидаемые)
            примеры.append(f"{r.tool_calls[0].name}"
                           f"({json.dumps(r.tool_calls[0].arguments, ensure_ascii=False)[:50]})")

    всего = len(ИНСТРУМЕНТЫ)
    return {"вызвал": вызовов / всего, "верный": попаданий / всего,
            "задержки": задержки, "стоимость": стоимость,
            "ошибки": ошибки, "примеры": примеры}


def bench(cfg, refs: list[str], repeat: int = 1) -> list[dict]:
    client = LLMClient(cfg)
    rows = []
    for ref in refs:
        spec = cfg.model_by_ref(ref)
        if spec.kind == "embedding":
            continue
        print(f"  {ref} ({spec.provider}/{spec.model})", flush=True)
        parse = {"json": 0.0, "намерение": 0.0, "заказ": 0.0, "задержки": [],
                 "стоимость": 0.0, "ошибки": []}
        tools = {"вызвал": 0.0, "верный": 0.0, "задержки": [], "стоимость": 0.0,
                 "ошибки": [], "примеры": []}
        for _ in range(repeat):
            p = _probe_parse(client, ref, cfg)
            t = _probe_tools(client, ref)
            for key in ("json", "намерение", "заказ"):
                parse[key] += p[key] / repeat
            for key in ("вызвал", "верный"):
                tools[key] += t[key] / repeat
            for d, src in ((parse, p), (tools, t)):
                d["задержки"] += src["задержки"]
                d["стоимость"] += src["стоимость"]
                d["ошибки"] += src["ошибки"]
            tools["примеры"] += t["примеры"]

        задержки = parse["задержки"] + tools["задержки"]
        доступна = bool(задержки)
        rows.append({
            "модель": ref, "провайдер": spec.provider, "идентификатор": spec.model,
            "доступна": доступна,
            "строгий JSON": round(parse["json"], 2),
            "верное намерение": round(parse["намерение"], 2),
            "верный номер заказа": round(parse["заказ"], 2),
            "вызвала инструмент": round(tools["вызвал"], 2),
            "верный инструмент": round(tools["верный"], 2),
            "задержка медиана, мс": round(statistics.median(задержки)) if задержки else None,
            "задержка максимум, мс": max(задержки) if задержки else None,
            "стоимость замера, $": round(parse["стоимость"] + tools["стоимость"], 5),
            "пример вызова": (tools["примеры"] or [""])[0],
            "ошибки": (parse["ошибки"] + tools["ошибки"])[:2],
        })
        итог = ("недоступна" if not доступна else
                f'JSON {rows[-1]["строгий JSON"]:.0%} · намерение '
                f'{rows[-1]["верное намерение"]:.0%} · инструмент '
                f'{rows[-1]["верный инструмент"]:.0%} · '
                f'{rows[-1]["задержка медиана, мс"]} мс')
        print(f"      {итог}")
    return rows


КОЛОНКИ = ["модель", "провайдер", "идентификатор", "строгий JSON", "верное намерение",
           "верный номер заказа", "вызвала инструмент", "верный инструмент",
           "задержка медиана, мс", "стоимость замера, $"]


def render_markdown(rows: list[dict], profile: str) -> str:
    lines = ["# Замеры моделей", "",
             f"Профиль на момент замера: `{profile}`  ·  дата: {datetime.now():%Y-%m-%d %H:%M}",
             "",
             "Меряются способности, от которых зависит работа агента: строгий вывод по "
             "JSON-схеме при разборе запроса, точность определения намерения и номера "
             "заказа, вызов инструмента через function calling, задержка и стоимость.",
             "", "| " + " | ".join(КОЛОНКИ) + " |",
             "|" + "---|" * len(КОЛОНКИ)]
    for r in rows:
        if not r["доступна"]:
            lines.append(f"| {r['модель']} | {r['провайдер']} | {r['идентификатор']} | "
                         + "недоступна | " * (len(КОЛОНКИ) - 3) + "")
            continue
        lines.append("| " + " | ".join(str(r[c]) for c in КОЛОНКИ) + " |")
    ошибки = [(r["модель"], r["ошибки"]) for r in rows if r["ошибки"]]
    if ошибки:
        lines += ["", "## Отказы", ""]
        for модель, тексты in ошибки:
            lines += [f"**{модель}**"] + [f"- {t}" for t in тексты] + [""]
    lines += ["", "## Как читать", "",
              "Роль `M_fast` требует строгого JSON и верного намерения — остальное "
              "для неё второстепенно. Роли `M_balanced` и `M_code` бессмысленны без "
              "уверенного вызова инструментов. Для `M_reason` решающими остаются "
              "длинный контекст и устойчивость к выдумкам: их этот замер не ловит, "
              "они проверяются прогоном золотого набора (`evals/report.md`).", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="llm.bench", description="Замеры моделей-кандидатов")
    p.add_argument("--models", help="Ключи моделей из models.yaml через запятую")
    p.add_argument("--repeat", type=int, default=1, help="Повторов каждого замера")
    p.add_argument("--out", default="docs/bench", help="Префикс файлов отчёта")
    args = p.parse_args(argv)

    try:
        cfg = get_config()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    refs = ([x.strip() for x in args.models.split(",")] if args.models
            else list(cfg.models_cfg["models"]))
    print(f"Замер {len(refs)} моделей, повторов: {args.repeat}\n")
    t0 = time.perf_counter()
    rows = bench(cfg, refs, repeat=args.repeat)

    out = cfg.root / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".md").write_text(render_markdown(rows, cfg.profile), encoding="utf-8")
    out.with_suffix(".json").write_text(
        json.dumps({"дата": datetime.now().isoformat(timespec="seconds"),
                    "профиль": cfg.profile, "модели": rows}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"\nГотово за {time.perf_counter() - t0:.0f} с. Отчёт: {out.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
