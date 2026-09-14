# -*- coding: utf-8 -*-
"""
Трассировка прогона.

На каждый вопрос заводится request_id и файл observability/runs/<request_id>.jsonl,
по строке на событие. Записывается то, что памятка курса называет безопасным:
шаг, выбранный маршрут и модель, краткое структурированное обоснование выбора,
вызванный инструмент и его безопасные аргументы, статус, длительность, токены,
стоимость, факт обращения к памяти и найденные источники, число повторов.

НЕ записывается: скрытая цепочка рассуждений модели, ключи доступа, полные тексты
документов и промптов. Длинные поля обрезаются, секреты вырезаются.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SECRET_KEYS = re.compile(
    r"(api[_-]?key|authorization|token|secret|password|bearer)", re.IGNORECASE
)
SECRET_VALUE = re.compile(r"\b(sk-[A-Za-z0-9\-_]{8,}|Bearer\s+\S+)")

# Исключения из затирания. Слово «token» встречается не только в секретах:
# счётчики tokens_in / tokens_out — это метрика, которую памятка курса прямо
# просит логировать, а затирались они вместе с ключами, и в трассе стояло «***».
# Наглядный пример того, как слишком широкая защита съедает нужные данные.
SAFE_KEYS = re.compile(r"^(tokens?_(in|out|total)|token_count|max_tokens)$", re.IGNORECASE)


def _is_secret(key: str) -> bool:
    return bool(SECRET_KEYS.search(key)) and not SAFE_KEYS.match(key)


def new_request_id() -> str:
    return f"req-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


class Trace:
    """Пишет события прогона в JSONL и держит счётчики лимитов."""

    def __init__(self, runs_dir: Path, question: str, task_file: str,
                 request_id: str | None = None, max_field_chars: int = 800,
                 console: bool = True, profile: str = ""):
        self.request_id = request_id or new_request_id()
        self.max_field_chars = max_field_chars
        self.console = console
        self.started = time.perf_counter()
        self.path = Path(runs_dir) / f"{self.request_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0

        # счётчики, по которым срабатывают ограничения
        self.iterations = 0
        self.tool_calls = 0
        self.retrieval_queries = 0
        self.retries = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.sources: list[str] = []

        self.event("run.start", question=question, task_file=task_file, profile=profile)

    # ------------------------------------------------------------- обрезка
    def _clean(self, value: Any) -> Any:
        if isinstance(value, str):
            value = SECRET_VALUE.sub("***", value)
            if len(value) > self.max_field_chars:
                return value[: self.max_field_chars] + f"… (+{len(value) - self.max_field_chars} симв.)"
            return value
        if isinstance(value, dict):
            return {
                k: ("***" if _is_secret(str(k)) else self._clean(v))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            items = [self._clean(v) for v in value[:50]]
            if len(value) > 50:
                items.append(f"… ещё {len(value) - 50}")
            return items
        return value

    # ------------------------------------------------------------- запись
    def event(self, kind: str, **fields) -> dict:
        self._seq += 1
        rec = {
            "request_id": self.request_id,
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_ms": round((time.perf_counter() - self.started) * 1000),
            "kind": kind,
        }
        rec.update(self._clean(fields))
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if self.console:
            self._print(rec)
        return rec

    def _print(self, rec: dict) -> None:
        kind = rec["kind"]
        tail = ""
        for key in ("node", "role", "model", "tool", "intent", "status", "reason_summary"):
            if key in rec:
                tail += f" {key}={rec[key]}"
        print(f"[{rec['elapsed_ms']:>6} ms] {kind}{tail}")

    # ------------------------------------------------------------- шаги
    @contextmanager
    def step(self, node: str, **fields):
        """Смысловой шаг: пишет начало, конец, длительность и статус."""
        t0 = time.perf_counter()
        self.event("step.start", node=node, **fields)
        try:
            yield self
        except Exception as exc:
            self.event("step.end", node=node, status="error",
                       error=f"{type(exc).__name__}: {exc}",
                       duration_ms=round((time.perf_counter() - t0) * 1000))
            raise
        else:
            self.event("step.end", node=node, status="ok",
                       duration_ms=round((time.perf_counter() - t0) * 1000))

    # ------------------------------------------------------------- события
    def decision(self, node: str, action: str, reason_summary: str, **fields) -> None:
        """Краткое структурированное решение вместо скрытого reasoning."""
        self.event("decision", node=node, action=action, reason_summary=reason_summary, **fields)

    def llm_call(self, *, role: str, model: str, provider: str, purpose: str,
                 tokens_in: int, tokens_out: int, cost_usd: float,
                 latency_ms: int, status: str = "ok", retries: int = 0, **fields) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.cost_usd += cost_usd
        self.retries += retries
        self.event("llm", role=role, model=model, provider=provider, purpose=purpose,
                   tokens_in=tokens_in, tokens_out=tokens_out,
                   cost_usd=round(cost_usd, 6), latency_ms=latency_ms,
                   status=status, retries=retries, **fields)

    def tool_call(self, *, tool: str, args: dict, status: str,
                  source: str | None = None, latency_ms: int = 0,
                  error: str | None = None, retries: int = 0) -> None:
        self.tool_calls += 1
        self.retries += retries
        if source:
            self.sources.append(source)
        self.event("tool", tool=tool, args=args, status=status, source=source,
                   latency_ms=latency_ms, error=error, retries=retries)

    def retrieval(self, *, query: str, hits: list[str], status: str = "ok") -> None:
        self.retrieval_queries += 1
        self.sources.extend(hits)
        self.event("retrieval", query=query, hits=hits, hits_count=len(hits), status=status)

    def iteration(self, n: int, reason_summary: str) -> None:
        self.iterations = n
        self.event("iteration", iteration=n, reason_summary=reason_summary)

    def limit_hit(self, limit: str, value: Any) -> None:
        self.event("limit", limit=limit, value=value, status="stopped")

    # ------------------------------------------------------------- итог
    def summary(self, status: str = "ok", **fields) -> dict:
        data = {
            "request_id": self.request_id,
            "status": status,
            "duration_ms": round((time.perf_counter() - self.started) * 1000),
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "retrieval_queries": self.retrieval_queries,
            "retries": self.retries,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
            "sources": sorted(set(self.sources)),
            "trace_file": str(self.path),
        }
        data.update(fields)
        self.event("run.end", **{k: v for k, v in data.items() if k != "request_id"})
        return data
