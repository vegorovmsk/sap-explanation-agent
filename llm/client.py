# -*- coding: utf-8 -*-
"""
Единый клиент моделей.

OpenRouter и Ollama говорят по одному протоколу (OpenAI Chat Completions), поэтому
на оба провайдера хватает одного кода: меняются только base_url, ключ и заголовки.
Это и есть механика профилей local / hybrid / cloud — переключение контура одной
строкой в конфиге, без правок кода агента.

Что берёт на себя клиент:
  * выбор реализации роли (M_fast / M_balanced / M_reason / M_code);
  * function calling и структурированный вывод по JSON-схеме,
    с деградацией до json_object для моделей без поддержки json_schema;
  * повтор при временной ошибке провайдера и размыкание цепи после серии отказов;
  * учёт токенов, стоимости и латентности, запись всего этого в трассу;
  * потолок стоимости одного прогона.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from core.config import Config, ModelSpec, get_config

try:
    from openai import OpenAI
    from openai import (APIConnectionError, APIStatusError, APITimeoutError,
                        AuthenticationError, BadRequestError, RateLimitError)
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Не установлен пакет openai. Выполните: pip install -r requirements.txt") from exc

try:
    from jsonschema import ValidationError, validate as _validate
except ImportError:  # pragma: no cover
    _validate = None
    ValidationError = ValueError

RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError)

# Формулировки OpenRouter, означающие «сейчас некому обслужить», а не «запрос плох».
UNAVAILABLE_MARKERS = (
    "no endpoints found",
    "no allowed providers",
    "temporarily rate-limited",
    "rate-limited upstream",
    "no instances available",
)


def _is_unavailable(text: str) -> bool:
    low = str(text).lower()
    return any(m in low for m in UNAVAILABLE_MARKERS)


class LLMError(RuntimeError):
    """Провайдер не смог обслужить запрос."""


class CircuitOpen(LLMError):
    """Цепь разомкнута: провайдер отказывал подряд слишком много раз."""


class BudgetExceeded(LLMError):
    """Достигнут потолок стоимости прогона."""


class StructuredOutputError(LLMError):
    """Модель не вернула валидный JSON по схеме даже после повтора."""


class EmptyResponse(LLMError):
    """Ответ без вариантов: провайдер вернул ошибку в теле успешного ответа.

    OpenRouter при отказе апстрима отвечает HTTP 200 и кладёт причину в поле
    `error`, а `choices` оставляет пустым. SDK разбирает это в обычный объект
    ответа, и наивное `raw.choices[0]` падало с `TypeError: 'NoneType' object is
    not subscriptable` — ошибкой, по которой ничего не понять. Настоящая причина
    (модель недоступна, лимит, нет провайдера) лежит рядом, и её надо показать.
    """


class ProviderUnavailable(LLMError):
    """Модель временно недоступна у провайдера — не ошибка запроса.

    Отличается от обычной ошибки тем, что лечится другой моделью. Прогон
    золотого набора 13.09 дал два таких случая подряд: «404 No endpoints found
    that support tool use» (у OpenRouter не нашлось провайдера с поддержкой
    инструментов для qwen-2.5-72b) и «429 rate-limited upstream» у deepseek.
    Обе — про доступность, а не про запрос, и обе роняли весь прогон.
    """


class LLMTimeout(LLMError):
    """Запрос перешагнул жёсткий дедлайн по стенным часам.

    Таймаут SDK считает паузы между байтами, поэтому провайдер, который медленно
    но безостановочно пишет ответ, его не задевает: живой прогон дал вызов на
    332 секунды при ``request_timeout_s: 60``. Этот класс — результат отдельного
    ограничителя, который смотрит на суммарное время запроса.
    """


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    text: str = ""
    data: dict | None = None          # разобранный JSON, если запрашивалась схема
    tool_calls: list[ToolCall] = field(default_factory=list)
    role: str = ""
    provider: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    retries: int = 0
    finish_reason: str = ""


class _Breaker:
    """Простой circuit breaker на провайдера."""

    def __init__(self, max_failures: int, cooldown_s: int):
        self.max_failures = max_failures
        self.cooldown_s = cooldown_s
        self._failures: dict[str, int] = {}
        self._opened_until: dict[str, float] = {}

    def check(self, provider: str) -> None:
        until = self._opened_until.get(provider, 0.0)
        if until > time.time():
            raise CircuitOpen(
                f"Провайдер «{provider}» временно отключён после "
                f"{self.max_failures} отказов подряд; повтор через "
                f"{round(until - time.time())} с"
            )

    def ok(self, provider: str) -> None:
        self._failures[provider] = 0

    def fail(self, provider: str) -> None:
        n = self._failures.get(provider, 0) + 1
        self._failures[provider] = n
        if n >= self.max_failures:
            self._opened_until[provider] = time.time() + self.cooldown_s


class LLMClient:
    """Клиент моделей. Недоступность моделей запоминается на весь процесс.

    Мёртвая модель — свойство парка провайдера, а не одного вопроса: прогон
    золотого набора 14.09 сходил за `qwen-2.5-coder-32b-instruct` 47 раз и все
    47 получил «no endpoints found». Клиент создаётся на каждый кейс (так
    считается стоимость), поэтому память о недоступности живёт на классе, иначе
    она обнуляется вместе с клиентом и агент снова стучится в закрытую дверь.
    Circuit breaker здесь не помогает: он ведёт счёт по ПРОВАЙДЕРУ, и разомкнуть
    цепь на openrouter из-за одной модели значило бы отключить и живые.
    """

    # модель -> до какого времени считаем её недоступной
    _unavailable: dict[str, float] = {}

    @classmethod
    def reset_availability(cls) -> None:
        """Забыть о недоступных моделях. Нужно тестам: память на классе иначе
        протекает между проверками и делает их зависимыми от порядка."""
        cls._unavailable.clear()

    def __init__(self, config: Config | None = None, trace=None):
        self.cfg = config or get_config()
        self.trace = trace
        limits = self.cfg.limits
        self.retries = int(limits.get("llm_retries", 2))
        self.timeout = float(limits.get("request_timeout_s", 60))
        # Жёсткий дедлайн поверх таймаута SDK: тот считает молчание канала, а не
        # общее время запроса, и зависший вызов держал граф больше пяти минут.
        self.hard_timeout = float(limits.get("hard_timeout_s", 0) or self.timeout * 2)
        self.timeout_retries = int(limits.get("llm_timeout_retries", 1))
        # У эмбеддингов свой таймаут: пачка считается на сервере последовательно,
        # и мерить её мерками одного интерактивного вызова неправильно.
        self.embed_timeout = float(limits.get("embed_timeout_s", 0) or self.timeout * 5)
        self.embed_retries = int(limits.get("embed_retries", 2))
        self.embed_batch = int(limits.get("embed_batch", 8))
        self.max_cost = float(limits.get("max_cost_usd", 0) or 0)
        self.breaker = _Breaker(int(limits.get("circuit_breaker_failures", 3)),
                                int(limits.get("circuit_breaker_cooldown_s", 60)))
        self.unavailable_cooldown_s = float(
            limits.get("model_unavailable_cooldown_s", 900))
        self._clients: dict[str, OpenAI] = {}
        self.spent_usd = 0.0

    # ------------------------------------------------------------- провайдеры
    def _client(self, spec: ModelSpec) -> OpenAI:
        key = f"{spec.provider}|{spec.base_url}"
        if key not in self._clients:
            self._clients[key] = OpenAI(
                base_url=spec.base_url,
                api_key=spec.api_key or "not-needed",
                default_headers=spec.headers or None,
                timeout=self.timeout,
                max_retries=0,          # повторами управляем сами, чтобы считать их в трассе
            )
        return self._clients[key]

    # ------------------------------------------------------------- бюджет
    def _charge(self, amount: float) -> None:
        self.spent_usd += amount
        if self.max_cost and self.spent_usd > self.max_cost:
            raise BudgetExceeded(
                f"Потолок стоимости прогона исчерпан: "
                f"${self.spent_usd:.4f} при лимите ${self.max_cost:.2f}"
            )

    # ------------------------------------------------------------- основной вызов
    def chat(self, role: str, messages: list[dict], *, purpose: str = "",
             tools: list[dict] | None = None, tool_choice: Any = None,
             json_schema: dict | None = None, schema_name: str = "result",
             temperature: float | None = None, max_tokens: int | None = None,
             model_ref: str | None = None) -> LLMResponse:
        """Один вызов модели в роли ``role``. Возвращает нормализованный ответ.

        `model_ref` адресует конкретную модель из models.yaml мимо профиля —
        этим пользуются замеры, сравнивающие кандидатов между собой.
        """
        chain = ([self.cfg.model_by_ref(model_ref)] if model_ref
                 else self.cfg.models_for(role))
        # Модели, про которые уже известно, что их нет, уходят в конец очереди,
        # а не выбрасываются: если весь список окажется мёртвым, попробовать
        # всё равно надо — вдруг парк провайдера уже вернулся.
        chain = self._by_availability(chain)
        last: Exception | None = None
        for position, spec in enumerate(chain):
            try:
                return self._chat_with(spec, role, messages, purpose=purpose, tools=tools,
                                       tool_choice=tool_choice, json_schema=json_schema,
                                       schema_name=schema_name, temperature=temperature,
                                       max_tokens=max_tokens)
            except (ProviderUnavailable, CircuitOpen) as exc:
                # Недоступность лечится другой моделью той же роли, ошибка
                # запроса — нет. Поэтому переключаемся только на этом классе.
                last = exc
                first_time = self._mark_unavailable(spec)
                nxt = chain[position + 1] if position + 1 < len(chain) else None
                if self.trace and first_time:
                    # Событие пишется один раз на модель: 47 одинаковых записей
                    # в трассе прошлого прогона скрывали, что беда системная.
                    self.trace.event("llm.fallback", role=role, purpose=purpose,
                                     unavailable=spec.model,
                                     next_model=nxt.model if nxt else None,
                                     error=str(exc)[:200])
                if nxt is None:
                    raise
        raise last if last else LLMError(f"Роль {role} не дала ни одной модели")

    # ------------------------------------------------------------- доступность
    def _is_unavailable(self, spec: ModelSpec) -> bool:
        return type(self)._unavailable.get(spec.model, 0.0) > time.time()

    def _mark_unavailable(self, spec: ModelSpec) -> bool:
        """Запомнить, что модели сейчас нет. True — если узнали об этом впервые."""
        first_time = not self._is_unavailable(spec)
        type(self)._unavailable[spec.model] = time.time() + self.unavailable_cooldown_s
        return first_time

    def _by_availability(self, chain: list[ModelSpec]) -> list[ModelSpec]:
        live = [s for s in chain if not self._is_unavailable(s)]
        dead = [s for s in chain if self._is_unavailable(s)]
        return live + dead

    def _chat_with(self, spec: ModelSpec, role: str, messages: list[dict], *,
                   purpose: str = "", tools=None, tool_choice=None,
                   json_schema: dict | None = None, schema_name: str = "result",
                   temperature: float | None = None,
                   max_tokens: int | None = None) -> LLMResponse:
        """Один заход конкретной моделью. Выбор модели — забота chat()."""
        self.breaker.check(spec.provider)

        payload: dict[str, Any] = {
            "model": spec.model,
            "messages": list(messages),
            "temperature": spec.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or spec.max_tokens,
        }
        if tools:
            if not spec.supports_tools:
                raise LLMError(
                    f"Модель {spec.model} (роль {role}) объявлена без поддержки инструментов, "
                    f"но вызвана с tools. Поправьте supports_tools в config/models.yaml"
                )
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        if json_schema:
            payload = self._with_schema(payload, spec, json_schema, schema_name)

        resp = self._request(spec, payload, purpose=purpose, role=role)

        if json_schema:
            resp = self._parse_structured(resp, spec, payload, json_schema, schema_name,
                                          purpose=purpose, role=role)
        return resp

    # ------------------------------------------------------------- схема
    @staticmethod
    def _schema_hint(schema: dict) -> str:
        return ("Ответь СТРОГО одним JSON-объектом без пояснений и без markdown-ограды. "
                "Объект обязан соответствовать схеме:\n"
                + json.dumps(schema, ensure_ascii=False))

    def _with_schema(self, payload: dict, spec: ModelSpec, schema: dict, name: str) -> dict:
        if spec.supports_json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": True},
            }
        else:
            # деградация: json_object + описание схемы в системном сообщении
            payload["response_format"] = {"type": "json_object"}
            payload["messages"] = payload["messages"] + [
                {"role": "system", "content": self._schema_hint(schema)}
            ]
        return payload

    def _parse_structured(self, resp: LLMResponse, spec: ModelSpec, payload: dict,
                          schema: dict, name: str, *, purpose: str, role: str) -> LLMResponse:
        text = (resp.text or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text.split("\n", 1)[-1] if "\n" in text else text
            text = text.rsplit("```", 1)[0]
        try:
            data = json.loads(text)
            if _validate is not None:
                _validate(instance=data, schema=schema)
        except (json.JSONDecodeError, ValidationError) as first_err:
            # Обрыв по лимиту токенов лечится не тем же, чем ошибка схемы: модель
            # не ошиблась в формате, она не успела дописать. Живой прогон показал
            # ровно это — рассуждения на 4096 токенов, 332 секунды и обрезанный
            # JSON. Поэтому в починку идёт прямое требование краткости, а сам
            # обрыв попадает в трассу отдельным событием.
            truncated = resp.finish_reason == "length"
            if truncated and self.trace:
                self.trace.event("llm.truncated", role=role, provider=spec.provider,
                                 model=spec.model, purpose=purpose,
                                 max_tokens=payload.get("max_tokens"))
            demand = ("Предыдущий ответ обрезан по лимиту токенов. Верни ТОЛЬКО "
                      "JSON-объект по схеме, без рассуждений; текстовые поля — "
                      "не длиннее двух фраз."
                      if truncated else
                      f"Ответ не соответствует схеме: {first_err}. "
                      f"Верни только исправленный JSON-объект по схеме.")
            repair = dict(payload)
            if truncated:
                # Просить краткости, не подняв потолок, — половина меры: модель
                # упёрлась в лимит, и на том же лимите второй заход упирается
                # снова. Прогон 14.09 потерял так два кейса целиком: обрыв →
                # починка на том же max_tokens → обрыв → падение прогона.
                ceiling = int(payload.get("max_tokens") or spec.max_tokens or 1024)
                repair["max_tokens"] = min(ceiling * 2, 4096)
            repair["messages"] = payload["messages"] + [
                {"role": "assistant", "content": (resp.text or "")[:2000]},
                {"role": "user", "content": demand},
            ]
            retry = self._request(spec, repair, purpose=f"{purpose}:repair", role=role)
            resp.retries += retry.retries + 1
            resp.tokens_in += retry.tokens_in
            resp.tokens_out += retry.tokens_out
            resp.cost_usd += retry.cost_usd
            resp.latency_ms += retry.latency_ms
            try:
                data = json.loads((retry.text or "").strip())
                if _validate is not None:
                    _validate(instance=data, schema=schema)
            except (json.JSONDecodeError, ValidationError) as second_err:
                raise StructuredOutputError(
                    f"Модель {spec.model} (роль {role}) не вернула валидный JSON по схеме "
                    f"«{name}»: {second_err}"
                ) from second_err
            resp.text = retry.text
        resp.data = data
        return resp

    # ------------------------------------------------------------- транспорт
    def _create(self, spec: ModelSpec, payload: dict):
        """Один запрос к провайдеру под жёстким дедлайном.

        Запрос уходит в отдельный поток-демон, а основной ждёт результат не
        дольше ``hard_timeout``. Поток нельзя прервать извне, но демон не мешает
        процессу завершиться, а socket закроется по таймауту SDK. Без этого
        зависший запрос останавливает весь граф: SDK ждёт молчания канала, а не
        общего времени ответа.
        """
        client = self._client(spec)
        box: queue.Queue = queue.Queue(maxsize=1)

        def work() -> None:
            try:
                box.put(("ok", client.chat.completions.create(**payload)))
            except BaseException as exc:                       # noqa: BLE001
                box.put(("err", exc))                          # переносим в основной поток

        threading.Thread(target=work, daemon=True,
                         name=f"llm-{spec.provider}").start()
        try:
            status, value = box.get(timeout=self.hard_timeout)
        except queue.Empty:
            raise LLMTimeout(
                f"Модель {spec.model} не ответила за {self.hard_timeout:.0f} с "
                f"(жёсткий дедлайн hard_timeout_s)"
            ) from None
        if status == "err":
            raise value
        return value

    def _request(self, spec: ModelSpec, payload: dict, *, purpose: str, role: str) -> LLMResponse:
        attempt, last_exc, timeouts = 0, None, 0
        while attempt <= self.retries:
            t0 = time.perf_counter()
            try:
                raw = self._create(spec, payload)
            except LLMTimeout as exc:
                # Дедлайн считаем отдельно от временных отказов: повторять зависший
                # запрос дорого, поэтому у него свой, более скупой лимит.
                timeouts += 1
                last_exc = exc
                self.breaker.fail(spec.provider)
                if self.trace:
                    self.trace.event("llm.timeout", role=role, provider=spec.provider,
                                     model=spec.model, purpose=purpose,
                                     hard_timeout_s=self.hard_timeout, attempt=timeouts)
                if timeouts > self.timeout_retries:
                    raise
                attempt += 1
                continue
            except RETRYABLE as exc:
                last_exc, attempt = exc, attempt + 1
                self.breaker.fail(spec.provider)
                if self.trace:
                    self.trace.event("llm.retry", role=role, provider=spec.provider,
                                     model=spec.model, attempt=attempt,
                                     error=f"{type(exc).__name__}: {exc}")
                if attempt > self.retries:
                    break
                time.sleep(min(2 ** attempt, 8))
                continue
            except (AuthenticationError, BadRequestError) as exc:
                self.breaker.fail(spec.provider)
                raise LLMError(f"{type(exc).__name__} у провайдера «{spec.provider}»: {exc}") from exc
            except APIStatusError as exc:
                if 500 <= getattr(exc, "status_code", 0) < 600 and attempt < self.retries:
                    last_exc, attempt = exc, attempt + 1
                    self.breaker.fail(spec.provider)
                    time.sleep(min(2 ** attempt, 8))
                    continue
                self.breaker.fail(spec.provider)
                # 404 «нет провайдера с поддержкой инструментов» — про
                # доступность, а не про запрос: ту же работу сделает другая
                # модель роли, поэтому наверх уходит отдельный класс ошибки.
                if _is_unavailable(exc):
                    raise ProviderUnavailable(
                        f"Модель {spec.model} сейчас недоступна у «{spec.provider}»: {exc}"
                    ) from exc
                raise LLMError(f"Провайдер «{spec.provider}» вернул ошибку: {exc}") from exc

            latency_ms = round((time.perf_counter() - t0) * 1000)
            try:
                resp = self._normalize(raw, spec, role, latency_ms, retries=attempt)
            except EmptyResponse as exc:
                # Отказ апстрима у OpenRouter приходит с кодом 200 и обычно
                # временный: нет свободного провайдера, сработал лимит. Терять
                # из-за него прогон золотого набора на третьем кейсе незачем.
                last_exc, attempt = exc, attempt + 1
                self.breaker.fail(spec.provider)
                if self.trace:
                    self.trace.event("llm.empty_response", role=role,
                                     provider=spec.provider, model=spec.model,
                                     purpose=purpose, attempt=attempt,
                                     error=str(exc)[:300])
                if attempt > self.retries:
                    raise
                time.sleep(min(2 ** attempt, 8))
                continue
            self.breaker.ok(spec.provider)
            self._charge(resp.cost_usd)
            if self.trace:
                self.trace.llm_call(role=role, model=spec.model, provider=spec.provider,
                                    purpose=purpose, tokens_in=resp.tokens_in,
                                    tokens_out=resp.tokens_out, cost_usd=resp.cost_usd,
                                    latency_ms=latency_ms, retries=attempt,
                                    tool_calls=[t.name for t in resp.tool_calls] or None)
            return resp

        self.breaker.fail(spec.provider)
        raise ProviderUnavailable(
            f"Модель {spec.model} недоступна у «{spec.provider}» после "
            f"{self.retries + 1} попыток: {last_exc}"
        ) from last_exc

    @staticmethod
    def _provider_error(raw) -> str:
        """Причина отказа из тела ответа, как её отдаёт OpenRouter."""
        err = getattr(raw, "error", None)
        if err is None:
            extra = getattr(raw, "model_extra", None) or {}
            err = extra.get("error")
        if isinstance(err, dict):
            parts = [str(err.get(k)) for k in ("message", "code", "type") if err.get(k)]
            meta = err.get("metadata")
            if isinstance(meta, dict) and meta.get("raw"):
                parts.append(str(meta["raw"])[:200])
            return "; ".join(parts) or str(err)[:200]
        return str(err)[:200] if err else ""

    @staticmethod
    def _normalize(raw, spec: ModelSpec, role: str, latency_ms: int, retries: int) -> LLMResponse:
        if not getattr(raw, "choices", None):
            reason = LLMClient._provider_error(raw)
            raise EmptyResponse(
                f"Модель {spec.model} (роль {role}) не вернула ни одного варианта ответа"
                + (f": {reason}" if reason else
                   " и не сообщила причину. Обычно это значит, что апстрим "
                   "провайдера отказал: модель недоступна, исчерпан лимит или "
                   "нет свободного провайдера.")
                + f". Проверьте идентификатор на openrouter.ai/models или "
                  f"замените модель роли {role} в config/models.yaml")
        choice = raw.choices[0]
        msg = choice.message
        calls: list[ToolCall] = []
        for tc in (getattr(msg, "tool_calls", None) or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"__raw__": tc.function.arguments}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        usage = getattr(raw, "usage", None)
        t_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        t_out = int(getattr(usage, "completion_tokens", 0) or 0)
        return LLMResponse(
            text=msg.content or "",
            tool_calls=calls,
            role=role,
            provider=spec.provider,
            model=spec.model,
            tokens_in=t_in,
            tokens_out=t_out,
            cost_usd=spec.cost(t_in, t_out),
            latency_ms=latency_ms,
            retries=retries,
            finish_reason=getattr(choice, "finish_reason", "") or "",
        )

    # ------------------------------------------------------------- эмбеддинги
    def embed(self, texts: Iterable[str], role: str = "embeddings") -> list[list[float]]:
        """Эмбеддинги одной пачки. Таймаут свой, повторы свои.

        Сборка индекса — единственная длинная пакетная операция в проекте, и
        падение на ней обходится дорого: на процессоре корпус кодируется
        минутами, а половина индекса остаётся несобранной. Поэтому здесь
        отдельный щедрый таймаут и повтор: заминка сервера не должна стоить
        всей сборки.
        """
        spec = self.cfg.model_for(role)
        self.breaker.check(spec.provider)
        items = list(texts)
        attempt, last_exc = 0, None
        while attempt <= self.embed_retries:
            t0 = time.perf_counter()
            try:
                raw = self._client(spec).embeddings.create(
                    model=spec.model, input=items, timeout=self.embed_timeout)
            except RETRYABLE as exc:
                last_exc, attempt = exc, attempt + 1
                if self.trace:
                    self.trace.event("embeddings.retry", role=role, provider=spec.provider,
                                     model=spec.model, count=len(items), attempt=attempt,
                                     error=f"{type(exc).__name__}: {exc}")
                if attempt > self.embed_retries:
                    break
                time.sleep(min(2 ** attempt, 8))
                continue
            if self.trace:
                self.trace.event("embeddings", role=role, provider=spec.provider,
                                 model=spec.model, count=len(items),
                                 latency_ms=round((time.perf_counter() - t0) * 1000))
            self.breaker.ok(spec.provider)
            return [d.embedding for d in raw.data]

        self.breaker.fail(spec.provider)
        raise LLMError(
            f"Эмбеддер {spec.model} не ответил за {self.embed_timeout:.0f} с "
            f"на пачку из {len(items)} фрагментов ({self.embed_retries + 1} попыток): "
            f"{last_exc}. Уменьшите limits.embed_batch или поднимите "
            f"limits.embed_timeout_s в config/settings.yaml"
        ) from last_exc

    # ------------------------------------------------------------- самопроверка
    def selftest(self, roles: list[str] | None = None) -> list[dict]:
        """Пингует каждую роль коротким запросом. Полезно перед демонстрацией."""
        results = []
        for role in roles or [r for r in self.cfg.roles() if r != "embeddings"]:
            row = {"role": role, "provider": "—", "model": "—"}
            try:
                # роль может быть не настроена вовсе (нет ключа, нет модели) —
                # это тоже результат самопроверки, а не повод падать
                spec = self.cfg.model_for(role)
                row.update(provider=spec.provider, model=spec.model)
                r = self.chat(role, [{"role": "user", "content": "Ответь одним словом: готов"}],
                              purpose="selftest", max_tokens=16)
                row.update(status="ok", latency_ms=r.latency_ms,
                           cost_usd=round(r.cost_usd, 6), answer=(r.text or "").strip()[:40])
            except Exception as exc:
                row.update(status="error", error=f"{type(exc).__name__}: {exc}")
            results.append(row)
        return results
