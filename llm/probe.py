# -*- coding: utf-8 -*-
"""
Проверка кандидатов на роль: отвечает ли модель и умеет ли вызывать инструменты.

Зачем отдельный модуль. Прогон золотого набора 13.09 потерял 14 кейсов из 19
не по качеству, а потому что у OpenRouter не нашлось провайдера с поддержкой
инструментов для объявленной модели. Прогон 14.09 состоялся, но роль M_code все
47 раз падала в запасную модель — то есть модель в конфиге была мёртвой, и
узнали мы об этом только из трассы, постфактум.

Вывод: перед прогоном набора состав ролей нужно ПРОВЕРЯТЬ, а не предполагать.
Модуль делает ровно один настоящий вызов на кандидата — с объявленным
инструментом и принудительным выбором, — и говорит, годится модель или нет.

    python -m llm.probe                         # кандидаты по умолчанию
    python -m llm.probe --models qwen/qwen3-coder,google/gemini-2.0-flash-001
    python -m llm.probe --config                # модели, объявленные в models.yaml

Ключ берётся из .env тем же способом, что и в рабочем прогоне: в чат, в аргументы
и в вывод он не попадает.
"""
from __future__ import annotations

import argparse
import time
from dataclasses import replace

from core.config import get_config
from llm.client import LLMClient, LLMError

# Инструмент нарочно примитивный: проверяем не сообразительность, а способность
# провайдера вообще принять tools и вернуть tool_calls.
PROBE_TOOL = [{
    "type": "function",
    "function": {
        "name": "lookup_norm",
        "description": "Вернуть норматив выработки для линии",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["line"],
            "properties": {"line": {"type": "string", "description": "Код линии"}},
        },
    },
}]

PROBE_MESSAGES = [
    {"role": "system", "content": "Ты вызываешь инструменты. Не отвечай текстом."},
    {"role": "user", "content": "Какой норматив выработки у линии ЛЭ2? Вызови инструмент."},
]

# Кандидаты на роль M_code: модель должна уметь инструменты и разбирать исходники.
DEFAULT_CANDIDATES = [
    "qwen/qwen3-coder",
    "google/gemini-2.0-flash-001",
    "anthropic/claude-3.5-haiku",
    "mistralai/mistral-small-3.1-24b-instruct",
    "deepseek/deepseek-chat",
    "qwen/qwen-2.5-coder-32b-instruct",   # текущая в конфиге — ожидаем отказ
]


def _spec_for(model: str):
    """Кандидат, которого нет в models.yaml, описывается поверх or_fast.

    ModelSpec заморожен — и правильно: подменять поля описания модели по ходу
    прогона нельзя, иначе в трассе окажется одно, а в запросе другое. Поэтому
    здесь создаётся копия с новым именем модели, а не правится оригинал.
    """
    base = get_config().model_by_ref("or_fast")
    return replace(base, ref=f"probe:{model}", model=model,
                   max_tokens=256, temperature=0, price_in=0.0, price_out=0.0)


def probe(model: str, timeout_note: str = "") -> dict:
    started = time.time()
    try:
        # Всё внутри try: одна неудачная модель не должна обрывать таблицу —
        # ради полной картины проверка и затевается.
        client = LLMClient(config=get_config())
        resp = client._chat_with(_spec_for(model), "probe", PROBE_MESSAGES,
                                 purpose="probe", tools=PROBE_TOOL)
    except LLMError as exc:
        return {"model": model, "ok": False, "tools": False,
                "ms": int((time.time() - started) * 1000), "note": str(exc)[:160]}
    except Exception as exc:                        # noqa: BLE001 — диагностика
        return {"model": model, "ok": False, "tools": False,
                "ms": int((time.time() - started) * 1000),
                "note": f"{type(exc).__name__}: {str(exc)[:140]}"}
    return {"model": model, "ok": True, "tools": bool(resp.tool_calls),
            "ms": int((time.time() - started) * 1000),
            "cost": resp.cost_usd,
            "note": "" if resp.tool_calls else "ответила текстом, инструмент не вызван"}


def prices(models: list[str]) -> dict[str, tuple[float, float]]:
    """Цены моделей ($/1M токенов), прочитанные у провайдера.

    В `config/models.yaml` цены нужны для подсчёта стоимости прогона, и
    выдуманные цифры делают весь учёт декорацией. Провайдер отдаёт их сам —
    значит, их надо взять, а не прикинуть.
    """
    base = get_config().model_by_ref("or_fast")
    out: dict[str, tuple[float, float]] = {}
    try:
        import httpx

        headers = {"Authorization": f"Bearer {base.api_key}"}
        data = httpx.get(f"{base.base_url}/models", headers=headers,
                         timeout=30).json().get("data", [])
    except Exception:                                    # noqa: BLE001
        return out
    wanted = set(models)
    for item in data:
        if item.get("id") not in wanted:
            continue
        pricing = item.get("pricing") or {}
        try:                     # провайдер отдаёт цену за ОДИН токен, строкой
            out[item["id"]] = (float(pricing.get("prompt", 0)) * 1_000_000,
                               float(pricing.get("completion", 0)) * 1_000_000)
        except (TypeError, ValueError):
            continue
    return out


def _config_models() -> list[str]:
    cfg = get_config()
    seen, out = set(), []
    for ref in sorted(cfg.models_cfg.get("models", {})):
        spec = cfg.model_by_ref(ref)
        if spec.provider != "openrouter" or spec.model in seen:
            continue
        seen.add(spec.model)
        out.append(spec.model)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="llm.probe",
                                description="Живы ли модели и умеют ли инструменты")
    p.add_argument("--models", help="Идентификаторы моделей через запятую")
    p.add_argument("--config", action="store_true",
                   help="Проверить модели, объявленные в models.yaml")
    args = p.parse_args(argv)

    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    elif args.config:
        models = _config_models()
    else:
        models = DEFAULT_CANDIDATES

    print(f"Проверяю {len(models)} моделей. Один вызов с инструментом на каждую.\n")
    print(f"{'модель':44} {'ответ':7} {'tools':7} {'мс':>7}  примечание")
    print("-" * 100)
    good: list[str] = []
    for model in models:
        r = probe(model)
        mark = "да" if r["ok"] else "НЕТ"
        tmark = "да" if r["tools"] else ("—" if r["ok"] else "")
        print(f"{model:44} {mark:7} {tmark:7} {r['ms']:>7}  {r['note']}")
        if r["ok"] and r["tools"]:
            good.append(model)

    print()
    if good:
        table = prices(good)
        print("Годятся для ролей с инструментами:")
        for m in good:
            p_in, p_out = table.get(m, (None, None))
            money = (f"  price_in: {p_in:.4g}  price_out: {p_out:.4g}"
                     if p_in is not None else "  цена не прочитана")
            print(f"  · {m}\n   {money}")
    else:
        print("Ни одна модель не вызвала инструмент — проверьте ключ и доступ.")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
