# -*- coding: utf-8 -*-
"""
Реестр инструментов: единственная точка, через которую агент трогает стенд.

Что здесь обеспечивается независимо от того, что решила модель:
  * allow-list — вызвать можно только зарегистрированный инструмент;
  * валидация аргументов по JSON-схеме до обращения к файлам;
  * лимит вызовов на один вопрос и один повтор при временной ошибке;
  * запись каждого вызова в трассу: инструмент, безопасные аргументы, статус,
    источник, длительность, повторы;
  * режим только для чтения — записывающих инструментов в реестре нет.

Ошибка инструмента не роняет прогон: она превращается в результат со статусом и
подсказкой, по которой агент выбирает следующее действие.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from core.config import Config
from tools import (code_search, log_reader, nsi_lookup, plan_reader, stand_config,
                   regulation_search, schemas, task_reader)
from tools.base import ToolResult
from tools.errors import ToolError, ToolInputError

try:
    from jsonschema import ValidationError, validate as _validate
except ImportError:  # pragma: no cover
    _validate = None
    ValidationError = ValueError


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., ToolResult]
    schema: dict
    reads: str            # какой источник закрывает: task | plan | nsi | logs | regulations | code
    needs_client: bool = False   # инструменту нужен клиент моделей (эмбеддинги)
    retrieval: bool = False      # обращение к векторной памяти, считается отдельным лимитом


REGISTRY: dict[str, ToolSpec] = {
    "read_task": ToolSpec("read_task", task_reader.read_task, schemas.READ_TASK, "task"),
    "list_tasks": ToolSpec("list_tasks", task_reader.list_tasks, schemas.LIST_TASKS, "task"),
    "read_plan": ToolSpec("read_plan", plan_reader.read_plan, schemas.READ_PLAN, "plan"),
    "lookup_nsi": ToolSpec("lookup_nsi", nsi_lookup.lookup_nsi, schemas.LOOKUP_NSI, "nsi"),
    "list_nsi_tables": ToolSpec("list_nsi_tables", nsi_lookup.list_nsi_tables,
                                schemas.LIST_NSI_TABLES, "nsi"),
    "read_logs": ToolSpec("read_logs", log_reader.read_logs, schemas.READ_LOGS, "logs"),
    "read_stand_config": ToolSpec("read_stand_config", stand_config.read_stand_config,
                                  schemas.READ_STAND_CONFIG, "config"),
    "search_regulations": ToolSpec("search_regulations", regulation_search.search_regulations,
                                   schemas.SEARCH_REGULATIONS, "regulations",
                                   needs_client=True, retrieval=True),
    "search_code": ToolSpec("search_code", code_search.search_code, schemas.SEARCH_CODE,
                            "code", needs_client=True, retrieval=True),
}


class ToolLimitExceeded(RuntimeError):
    """Исчерпан лимит вызовов инструментов на один вопрос."""


def openai_tools(names: list[str] | None = None) -> list[dict]:
    """Описания функций в формате function calling — то, что уходит модели."""
    specs = [REGISTRY[n] for n in (names or REGISTRY) if n in REGISTRY]
    return [{"type": "function", "function": s.schema} for s in specs]


def tools_for_sources(sources: list[str]) -> list[str]:
    """Инструменты, закрывающие перечисленные источники маршрута."""
    wanted = set(sources)
    names = [s.name for s in REGISTRY.values() if s.reads in wanted]
    # каталоги отдаём вместе с основными инструментами: без них модель гадает
    if "nsi" in wanted and "list_nsi_tables" not in names:
        names.append("list_nsi_tables")
    return names


def _coerce_args(spec: ToolSpec, args: dict) -> dict:
    """Приводит скаляры к типу из схемы там, где это ничего не теряет.

    Модели путают «1» и 1: номер таблицы уходит числом, и вызов отвергается с
    «1 is not of type string». Живой прогон потратил на это целую итерацию из
    пяти — при том что разницы между «1» и 1 для номера таблицы не существует.
    Приведение делается только для простых типов и только когда оно обратимо.
    """
    props = (spec.schema.get("parameters") or {}).get("properties") or {}
    out = dict(args)
    for key, value in args.items():
        want = (props.get(key) or {}).get("type")
        if want == "string" and isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = str(int(value) if float(value).is_integer() else value)
        elif want == "integer" and isinstance(value, str) and value.strip().isdigit():
            out[key] = int(value.strip())
        elif want == "number" and isinstance(value, str):
            try:
                out[key] = float(value.replace(",", "."))
            except ValueError:
                pass
    return out


def _validate_args(spec: ToolSpec, args: dict) -> None:
    if _validate is None:
        return
    try:
        _validate(instance=args, schema=spec.schema["parameters"])
    except ValidationError as exc:
        raise ToolInputError(
            f"Аргументы {spec.name} не соответствуют схеме: {exc.message}",
            hint="Проверьте обязательные поля и типы в описании инструмента",
        ) from exc


def execute(name: str, args: dict | None = None, *, cfg: Config, trace=None,
            client=None) -> ToolResult:
    """Вызывает инструмент по имени. Наружу исключения не выходят."""
    # Модели при function calling часто заполняют ВСЕ поля схемы, подставляя null
    # в неиспользуемые: search_code(query=…, clause=None, table=None, symbol=None).
    # Схема требует строку — вызов отвергался, и живой прогон потерял на этом
    # целый кейс: код не был прочитан ни разу за пять итераций. Пустое значение
    # означает «аргумент не передан», и здесь он просто убирается.
    args = {k: v for k, v in (args or {}).items() if v is not None}
    limit = int(cfg.limits.get("max_tool_calls", 10))
    if trace is not None and trace.tool_calls >= limit:
        trace.limit_hit("max_tool_calls", limit)
        raise ToolLimitExceeded(
            f"Исчерпан лимит вызовов инструментов на один вопрос: {limit}"
        )

    spec = REGISTRY.get(name)
    if spec is None:
        result = ToolResult(
            tool=name, status="invalid_args",
            error=f"Инструмент «{name}» не зарегистрирован",
            hint="Доступны: " + ", ".join(sorted(REGISTRY)),
        )
        if trace is not None:
            trace.tool_call(tool=name, args=args, status=result.status, error=result.error)
        return result

    # обращения к векторной памяти ограничены отдельно: без этого агент
    # переформулирует запрос бесконечно вместо того, чтобы признать пробел
    if spec.retrieval and trace is not None:
        rlimit = int(cfg.limits.get("max_retrieval_queries", 2))
        if trace.retrieval_queries >= rlimit:
            trace.limit_hit("max_retrieval_queries", rlimit)
            result = ToolResult(
                tool=spec.name, status="error",
                error=f"Исчерпан лимит обращений к векторной памяти: {rlimit}",
                hint="Работайте с уже собранными фактами. Если нормы среди них нет — "
                     "зафиксируйте пробел, а не достраивайте её.")
            trace.tool_call(tool=spec.name, args=args, status=result.status, error=result.error)
            return result

    if spec.needs_client and client is not None:
        args.setdefault("client", client)

    retries_allowed = int(cfg.limits.get("tool_retries", 1))
    attempt = 0
    t0 = time.perf_counter()

    while True:
        try:
            payload_args = _coerce_args(spec, {k: v for k, v in args.items()
                                                if k != "client"})
            _validate_args(spec, payload_args)
            if "client" in args:
                payload_args["client"] = args["client"]
            result = spec.fn(cfg, **payload_args)
            result.latency_ms = round((time.perf_counter() - t0) * 1000)
            break
        except ToolError as exc:
            if exc.retryable and attempt < retries_allowed:
                attempt += 1
                time.sleep(0.4 * attempt)
                continue
            result = ToolResult(tool=spec.name, status=exc.status, error=str(exc),
                                hint=exc.hint,
                                latency_ms=round((time.perf_counter() - t0) * 1000))
            break
        except TypeError as exc:
            result = ToolResult(
                tool=spec.name, status="invalid_args",
                error=f"Неверный набор аргументов: {exc}",
                hint="Сверьтесь с описанием инструмента",
                latency_ms=round((time.perf_counter() - t0) * 1000))
            break
        except Exception as exc:  # noqa: BLE001 — сбой инструмента не должен ронять прогон
            result = ToolResult(
                tool=spec.name, status="error",
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=round((time.perf_counter() - t0) * 1000))
            break

    if trace is not None:
        safe_args = {k: v for k, v in args.items() if k != "client"}
        # Инструмент может сказать, что векторного поиска не было: перечисление
        # пунктов этапа и переход по графу связей — это фильтр по индексу.
        spent_retrieval = (spec.retrieval and result.status != "invalid_args"
                           and result.used_retrieval is not False)
        if spent_retrieval:
            # Считаем неудачные обращения тоже: лимит существует ровно для того,
            # чтобы агент не переформулировал запрос бесконечно. Но вызов,
            # отвергнутый проверкой аргументов, до памяти не дошёл — списывать
            # за него квоту значит наказывать за опечатку в схеме.
            payload = result.payload or {}
            trace.retrieval(query=str(payload.get("запрос") or payload.get("таблица")
                                      or payload.get("пункт") or safe_args),
                            hits=result.locators[:10], status=result.status)
        trace.tool_call(tool=spec.name, args=safe_args, status=result.status,
                        source=result.source or None, latency_ms=result.latency_ms,
                        error=result.error, retries=attempt)
    return result
