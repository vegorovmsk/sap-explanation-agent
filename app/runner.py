# -*- coding: utf-8 -*-
"""
Headless-запуск агента: вопрос → ответ + трасса.

    python -m app.runner "Почему заказ Z-1060 не поставлен на линию ЛП2?"
    python -m app.runner --task input_task_2.xlsx --profile local "Где заказ B-3007?"
    python -m app.runner --selftest
    python -m app.runner --tool read_plan --args '{"order_number": "Z-1060"}'

Этим же скриптом гоняются evals: он не задаёт вопросов, ничего не ждёт от
пользователя и всегда оставляет после себя файл трассы.

Прогон идёт по графу LangGraph (agent/graph.py): ingest → classify → plan_sources →
act → observe → check_consistency → answer либо ticket → verify, с ветвлениями на
уточняющий вопрос и честный отказ.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import graph as agent_graph                        # noqa: E402
from agent.deps import Deps                                   # noqa: E402
from agent.state import AgentState                            # noqa: E402
from core.config import ConfigError, get_config               # noqa: E402
from llm.client import LLMClient, LLMError                    # noqa: E402
from observability.trace import Trace                         # noqa: E402
from tools.registry import REGISTRY, execute, openai_tools    # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="app.runner",
        description="Агент-объяснитель решений САП: один вопрос — один прогон с трассой",
    )
    p.add_argument("question", nargs="?", help="Вопрос пользователя")
    p.add_argument("--task", help="Файл задания в стенде (по умолчанию из settings.yaml)")
    p.add_argument("--profile", help="Профиль моделей: local | hybrid | cloud")
    p.add_argument("--json", action="store_true", help="Печатать только итоговый JSON")
    p.add_argument("--quiet", action="store_true", help="Не дублировать шаги трассы в stdout")
    p.add_argument("--selftest", action="store_true",
                   help="Проверка готовности: окружение, стенд, память, модели, "
                        "строгий JSON и вызов инструментов")
    p.add_argument("--shallow", action="store_true",
                   help="К --selftest: не тратить токены на пробные вызовы")
    p.add_argument("--tool", help="Вызвать инструмент напрямую, без модели "
                                  f"({', '.join(sorted(REGISTRY))})")
    p.add_argument("--args", default="{}", help="Аргументы инструмента одним JSON-объектом")
    return p


# ---------------------------------------------------------------- проверка готовности
PROBE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["stage", "line"],
    "properties": {
        "stage": {"type": "string", "description": "Этап производства из вопроса"},
        "line": {"type": "string", "description": "Обозначение линии из вопроса"},
    },
}
PROBE_QUESTION = "Почему заказ Z-1060 не поставлен на линию ЛП2 на этапе печати?"


def _probe_structured(client, role: str) -> tuple[str, str]:
    """Умеет ли модель отдать строгий JSON по схеме. Здесь ломается чаще всего."""
    try:
        r = client.chat(role, [
            {"role": "system", "content": "Извлеки сущности из вопроса."},
            {"role": "user", "content": PROBE_QUESTION}],
            purpose="selftest:structured", json_schema=PROBE_SCHEMA,
            schema_name="probe", max_tokens=128)
        data = r.data or {}
        ok = str(data.get("line", "")).upper().startswith("ЛП")
        return ("ok" if ok else "странный ответ"), f"{data.get('stage')} / {data.get('line')}"
    except Exception as exc:  # noqa: BLE001
        return "сбой", f"{type(exc).__name__}: {exc}"


def _probe_tools(client, role: str) -> tuple[str, str]:
    """Вызывает ли модель инструмент через function calling и с какими аргументами."""
    try:
        r = client.chat(role, [
            {"role": "system", "content": "Ты читаешь данные системы планирования. "
                                          "Пользуйся инструментами, не отвечай по памяти."},
            {"role": "user", "content": "Найди заказ Z-1060 в результате расчёта."}],
            purpose="selftest:tools",
            tools=openai_tools(["read_plan", "list_nsi_tables"]), max_tokens=256)
        if not r.tool_calls:
            return "инструменты не вызваны", (r.text or "")[:60]
        call = r.tool_calls[0]
        return "ok", f"{call.name}({json.dumps(call.arguments, ensure_ascii=False)[:60]})"
    except Exception as exc:  # noqa: BLE001
        return "сбой", f"{type(exc).__name__}: {exc}"


# Промпты агента доходят до 3–6 тысяч токенов: каталог таблиц НСИ, карта этапа,
# сводка фактов. Ollama обрезает промпт до num_ctx МОЛЧА, и модель получает
# огрызок задания, ничем это не выдавая. Проверка функциональная: метка кладётся
# в САМОЕ НАЧАЛО длинного промпта, вопрос задаётся в конце. Не дошла метка —
# значит начало промпта отрезано.
CONTEXT_MARKER = "КОДОВОЕ-СЛОВО-ОБСИДИАН-7419"
CONTEXT_FILLER_TOKENS = 4000


def _probe_context(client, role: str) -> tuple[str, str]:
    """Доходит ли начало длинного промпта до модели."""
    # ~4 токена на строку, слова разные, чтобы модель не сочла текст мусором
    filler = "\n".join(f"Строка наполнителя номер {i}: сведения о партии и линии."
                        for i in range(CONTEXT_FILLER_TOKENS // 4))
    try:
        r = client.chat(role, [
            {"role": "system", "content": f"Кодовое слово задания: {CONTEXT_MARKER}. "
                                          f"Запомни его и повтори по запросу."},
            {"role": "user", "content": f"{filler}\n\nПовтори кодовое слово задания "
                                        f"одним словом, без пояснений."}],
            purpose="selftest:context", max_tokens=64)
        text = (r.text or "").strip()
        if CONTEXT_MARKER in text:
            return "ok", f"начало промпта дошло (~{CONTEXT_FILLER_TOKENS} токенов наполнителя)"
        return ("обрезан",
                f"модель не увидела начало промпта: «{text[:50]}». "
                f"Поднимите контекст: OLLAMA_CONTEXT_LENGTH=8192")
    except Exception as exc:  # noqa: BLE001
        return "сбой", f"{type(exc).__name__}: {exc}"


def _check_live_embedder(cfg, built_with: str) -> int:
    """Доступен ли сейчас тот эмбеддер, которым собран индекс."""
    try:
        from memory.embeddings import get_embedder
        from llm.client import LLMClient
        live, reason = get_embedder(cfg, LLMClient(cfg))
    except Exception as exc:                                   # noqa: BLE001
        print(f"         [!] не удалось проверить эмбеддер: {type(exc).__name__}: {exc}")
        return 1
    if live.name == built_with:
        return 0
    print(f"         [СБОЙ] индекс собран эмбеддером {built_with}, а сейчас "
          f"доступен только {live.name}"
          + (f" ({reason})" if reason else ""))
    print("                Поиск по регламентам и коду откажет. Запустите Ollama "
          "(ollama serve) — пересобирать индекс не нужно.")
    return 1


def run_selftest(cfg, deep: bool = True) -> int:
    """Проверка готовности к живому прогону: окружение, стенд, память, модели."""
    print(f"Профиль моделей: {cfg.profile}")
    print(f"Стенд: {cfg.stand.root}")
    problems = 0

    print("\nОкружение")
    env_file = cfg.root / ".env"
    print(f"  [{'OK  ' if env_file.exists() else 'НЕТ '}] файл .env"
          + ("" if env_file.exists() else "  — скопируйте .env.example в .env"))
    problems += not env_file.exists()

    print("\nСтенд")
    for label, path in (("задание", cfg.stand.task_for(cfg.stand.default_task)),
                        ("результат расчёта", cfg.stand.result_for(cfg.stand.default_task)),
                        ("лог прогона", cfg.stand.log_for(cfg.stand.default_task))):
        print(f"  [{'OK  ' if path.exists() else 'НЕТ '}] {label}: {path.name}")
        problems += not path.exists()

    print("\nВекторная память")
    needs_live: set[str] = set()
    try:
        from memory.vector_store import open_store, resolve_location
        where = (resolve_location(cfg) if cfg.settings["memory"]["backend"] == "qdrant"
                 else str(cfg.root / cfg.settings["memory"]["store_dir"]))
        print(f"  бэкенд {cfg.settings['memory']['backend']}, хранилище {where}")
        for name in ("regulations", "code"):
            store = open_store(cfg, name)
            if store.exists():
                info = store.info
                print(f"  [OK  ] {name}: {info['count']} фрагментов, эмбеддер "
                      f"{info['embedder']}, поиск "
                      f"{'гибридный' if info.get('hybrid') else 'плотный'}")
                # Зелёная отметка при запасном эмбеддере вводит в заблуждение:
                # индекс есть, но смысловой поиск не работает — находится только
                # то, что совпало буквально. Прогон это не блокирует, поэтому
                # предупреждение, а не проблема.
                if str(info.get("embedder", "")).startswith("hashing"):
                    print("         [!] это ЗАПАСНОЙ эмбеддер: смысловой поиск не "
                          "работает, находится только буквальное совпадение.")
                    print("             Лечится так: ollama pull bge-m3 && "
                          "python -m memory.build")
                else:
                    # Обратный случай: индекс собран настоящим эмбеддером, и
                    # теперь он НУЖЕН на каждом прогоне. Проверяем один раз
                    # после обхода индексов, а не по разу на каждый.
                    needs_live.add(info["embedder"])
            else:
                print(f"  [НЕТ ] {name}: индекс не собран — выполните python -m memory.build")
                problems += 1
    except Exception as exc:  # noqa: BLE001
        print(f"  [СБОЙ] память недоступна: {type(exc).__name__}: {exc}")
        problems += 1
    for built_with in sorted(needs_live):
        problems += _check_live_embedder(cfg, built_with)

    print("\nМодели")
    client = LLMClient(cfg)
    rows = client.selftest()
    width = max(len(r["role"]) for r in rows)
    for r in rows:
        mark = "OK  " if r["status"] == "ok" else "СБОЙ"
        tail = (f"{r['latency_ms']} мс, ${r['cost_usd']}" if r["status"] == "ok"
                else r.get("error", ""))
        print(f"  [{mark}] {r['role']:<{width}}  {r['provider']}/{r['model']}  —  {tail}")
        problems += r["status"] != "ok"

    alive = {r["role"] for r in rows if r["status"] == "ok"}
    if deep and alive:
        print("\nЧто у настоящих моделей ломается чаще всего")
        if "M_fast" in alive:
            status, detail = _probe_structured(client, "M_fast")
            print(f"  [{'OK  ' if status == 'ok' else 'СБОЙ'}] строгий JSON по схеме "
                  f"(M_fast)  —  {detail}")
            problems += status != "ok"
        if "M_balanced" in alive:
            status, detail = _probe_tools(client, "M_balanced")
            print(f"  [{'OK  ' if status == 'ok' else 'СБОЙ'}] вызов инструмента "
                  f"(M_balanced)  —  {detail}")
            problems += status != "ok"
        # Длину контекста проверяем только у локальных моделей: у облачных она
        # объявлена провайдером и промпт не режется молча, а замер стоит токенов.
        local_roles = [r["role"] for r in rows
                       if r["status"] == "ok" and r.get("provider") == "ollama"]
        if local_roles:
            role = "M_balanced" if "M_balanced" in local_roles else local_roles[0]
            status, detail = _probe_context(client, role)
            print(f"  [{'OK  ' if status == 'ok' else 'СБОЙ'}] длина контекста "
                  f"({role})  —  {detail}")
            problems += status != "ok"
        print(f"\n  израсходовано на проверку: ${client.spent_usd:.4f}")

    print()
    if problems:
        print(f"Готовность неполная: проблем — {problems}. Живой прогон запускать рано.")
    else:
        print("Всё на месте. Можно запускать живой прогон:")
        print('  python -m app.runner "Почему заказ Z-1060 не поставлен на линию ЛП2?"')
    return 1 if problems else 0


# ------------------------------------------------------------------ прямой вызов
def run_tool(cfg, name: str, raw_args: str, quiet: bool) -> int:
    """Отладка и демонстрация доказательной базы без обращения к модели."""
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        print(f"Аргументы должны быть JSON-объектом: {exc}", file=sys.stderr)
        return 2

    trace = Trace(runs_dir=cfg.root / cfg.obs["runs_dir"],
                  question=f"прямой вызов {name}", task_file=cfg.stand.default_task,
                  max_field_chars=cfg.obs["max_field_chars"],
                  console=cfg.obs["console"] and not quiet, profile=cfg.profile)
    result = execute(name, args, cfg=cfg, trace=trace)
    trace.summary(status=result.status)
    print(json.dumps(result.to_model(), ensure_ascii=False, indent=2))
    print(f"\nТрасса: {trace.path}", file=sys.stderr)
    return 0 if result.ok else 1


# --------------------------------------------------------------------- прогон
def run_question(cfg, question: str, task_file: str, quiet: bool,
                 make_client=None) -> dict:
    """Один прогон. `make_client` позволяет подменить клиент моделей —
    этим пользуется прогон evals в режиме самопроверки."""
    trace = Trace(
        runs_dir=cfg.root / cfg.obs["runs_dir"],
        question=question,
        task_file=task_file,
        max_field_chars=cfg.obs["max_field_chars"],
        console=cfg.obs["console"] and not quiet,
        profile=cfg.profile,
    )
    client = make_client(cfg, trace) if make_client else LLMClient(cfg, trace=trace)
    deps = Deps(cfg=cfg, client=client, trace=trace)

    try:
        state = agent_graph.run(question, task_file, deps)
    except (LLMError, ConfigError) as exc:
        state = AgentState(request_id=trace.request_id, question=question,
                           task_file=task_file, status="error",
                           error=f"{type(exc).__name__}: {exc}",
                           answer="Прогон остановлен: модель недоступна или конфигурация неполна.")
    except Exception as exc:  # noqa: BLE001 — сбой не должен оставить прогон без трассы
        state = AgentState(request_id=trace.request_id, question=question,
                           task_file=task_file, status="error",
                           error=f"{type(exc).__name__}: {exc}",
                           answer="Прогон прерван внутренней ошибкой, подробности в трассе.")
        trace.event("run.error", error=state.error)

    summary = trace.summary(
        status=state.status, intent=state.intent, role=state.role,
        required_sources=state.required_sources, confidence=round(state.confidence, 2),
        evidence=len(state.evidence), conflicts=len(state.conflicts),
        escalated=bool(state.ticket),
    )
    return {
        "request_id": trace.request_id,
        "status": state.status,
        "intent": state.intent,
        "entities": state.entities,
        "route": {"role": state.role, "companion_role": state.companion_role,
                  "required_sources": state.required_sources,
                  "sources_seen": state.sources_seen,
                  "planned_tools": state.planned_tools},
        "answer": state.answer,
        "ticket": state.ticket_text or None,
        "confidence": state.confidence_label,
        "evidence": [{"claim": e.claim, "source": e.source, "locator": e.locator}
                     for e in state.evidence],
        "verification": state.verification,
        "security_events": state.security_events,
        "error": state.error,
        "metrics": {k: summary[k] for k in
                    ("duration_ms", "tokens_in", "tokens_out", "cost_usd",
                     "tool_calls", "retrieval_queries", "iterations", "retries")},
        "trace_file": summary["trace_file"],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = get_config(args.profile)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    if args.selftest:
        return run_selftest(cfg, deep=not args.shallow)
    if args.tool:
        return run_tool(cfg, args.tool, args.args, quiet=args.quiet or args.json)
    if not args.question:
        build_parser().print_help()
        return 2

    task_file = args.task or cfg.stand.default_task
    if not cfg.stand.task_for(task_file).exists():
        print(f"Файл задания не найден: {cfg.stand.task_for(task_file)}", file=sys.stderr)
        return 2

    result = run_question(cfg, args.question, task_file, quiet=args.quiet or args.json)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        line = "─" * 72
        print("\n" + line)
        print(result["answer"])
        if result.get("ticket"):
            print("\n" + line + "\n" + result["ticket"])
        print("\n" + line)
        print(f"Намерение : {result['intent']}   Маршрут: {result['route']['role']}"
              + (f" + {result['route']['companion_role']}"
                 if result["route"]["companion_role"] else ""))
        print(f"Источники : {', '.join(result['route']['sources_seen']) or '—'}"
              f"   фактов: {len(result['evidence'])}")
        if result.get("security_events"):
            print(f"Внимание  : {'; '.join(result['security_events'])}")
        if result.get("error"):
            print(f"Ошибка    : {result['error']}")
        m = result["metrics"]
        print(f"Метрики   : {m['duration_ms']} мс · итераций {m['iterations']} · "
              f"инструментов {m['tool_calls']} · {m['tokens_in']}/{m['tokens_out']} токенов "
              f"· ${m['cost_usd']}")
        print(f"Трасса    : {result['trace_file']}")
        print(line)
    return 0 if result["status"] != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
