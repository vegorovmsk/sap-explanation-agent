# -*- coding: utf-8 -*-
"""
Дымовой тест каркаса без обращения к моделям.

Проверяет то, что должно работать даже без ключей и без запущенного Ollama:
конфигурация читается, пути стенда существуют, роутинг покрывает все намерения,
трасса пишется и обрезает секреты, узел classify корректно разбирает ответ модели.

Запуск: python -m tests.test_smoke   (из корня sap_agent)
"""
from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.deps import Deps                        # noqa: E402
from agent.nodes.classify import classify           # noqa: E402
from agent.prompts import classify as prompt        # noqa: E402
from agent.state import AgentState, Evidence        # noqa: E402
from core.config import ModelSpec, get_config        # noqa: E402
from llm.client import LLMClient, LLMTimeout         # noqa: E402
from observability.trace import Trace               # noqa: E402

FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if condition else 'СБОЙ'}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILED.append(name)


class _SleepingProvider:
    """Провайдер, который держит соединение: так проверяется жёсткий дедлайн."""

    def __init__(self, delay_s: float):
        outer = self

        class _Completions:
            @staticmethod
            def create(**_kw):
                time.sleep(outer.delay_s)
                return "ответ"

        class _Chat:
            completions = _Completions()

        self.delay_s = delay_s
        self.chat = _Chat()


class FakeLLM:
    """Подставная модель: возвращает заранее заданный JSON вместо вызова провайдера."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    def chat(self, role, messages, **kw):
        self.calls += 1
        self.last = {"role": role, "messages": messages, "kw": kw}
        self.spent_usd = 0.0

        class R:
            data = self.payload
            text = json.dumps(self.payload, ensure_ascii=False)
        return R()


def main() -> int:
    cfg = get_config()
    # трассы тестов пишем во временный каталог, чтобы не сорить в observability/runs
    runs = Path(tempfile.mkdtemp(prefix="sap-agent-test-"))

    print("\nКонфигурация")
    check("профиль загружен", cfg.profile in cfg.models_cfg["profiles"], cfg.profile)
    check("каталог стенда существует", cfg.stand.root.exists(), str(cfg.stand.root))
    check("задание по умолчанию на месте", cfg.stand.task_for(cfg.stand.default_task).exists())
    check("результат расчёта на месте", cfg.stand.result_for(cfg.stand.default_task).exists())
    check("лог прогона на месте", cfg.stand.log_for(cfg.stand.default_task).exists())
    check("регламенты на месте", len(list(cfg.stand.reglaments_dir.glob("Регламент*.docx"))) == 3)
    check("таблицы НСИ на месте", len(list(cfg.stand.params_dir.glob("*.xlsx"))) == 16)
    check("allow-list отсекает путь вне стенда",
          not cfg.stand.is_allowed(Path.home() / "секрет.txt"))

    print("\nПрофили моделей")
    for name in cfg.models_cfg["profiles"]:
        mapping = cfg.models_cfg["profiles"][name]
        refs = []
        for value in mapping.values():          # роль указывает модель или цепочку
            refs.extend([value] if isinstance(value, str) else list(value))
        unknown = [r for r in refs if r not in cfg.models_cfg["models"]]
        check(f"профиль {name}: все роли ссылаются на существующие модели", not unknown, str(unknown))

    print("\nЗапасная модель роли")
    cloud = get_config(profile="cloud", reload=True)
    chain = cloud.models_for("M_balanced")
    check("у роли есть запасная модель", len(chain) >= 2,
          " → ".join(s.model for s in chain))
    check("запасная — другой модели, а не тот же провайдер апстрима",
          chain[0].model != chain[1].model, str([s.model for s in chain]))

    from llm.client import LLMClient as _LC2, LLMResponse, ProviderUnavailable  # noqa: E402
    _LC2.reset_availability()     # память о недоступности живёт на классе
    client = _LC2(cloud)
    tried = []

    def _one(spec, role, messages, **kw):
        tried.append(spec.model)
        if spec.model == chain[0].model:
            raise ProviderUnavailable("404 No endpoints found that support tool use")
        return LLMResponse(text="готов", role=role, provider=spec.provider, model=spec.model)

    client._chat_with = _one                                    # noqa: SLF001
    answer = client.chat("M_balanced", [{"role": "user", "content": "?"}], purpose="t")
    check("недоступность основной модели переключает на запасную",
          tried == [chain[0].model, chain[1].model], " → ".join(tried))
    check("ответ пришёл от запасной", answer.model == chain[1].model, answer.model)

    def _all_bad(spec, role, messages, **kw):
        raise ProviderUnavailable("429 rate-limited upstream")

    client._chat_with = _all_bad                                # noqa: SLF001
    try:
        client.chat("M_balanced", [{"role": "user", "content": "?"}], purpose="t")
        raised = False
    except ProviderUnavailable:
        raised = True
    check("если недоступны все — ошибка, а не тишина", raised)

    # Мёртвую модель спрашивают один раз. Прогон 14.09: роль M_code сходила за
    # `qwen-2.5-coder-32b-instruct` 47 раз и все 47 получила «no endpoints
    # found» — клиент создаётся на каждый кейс, и память о недоступности
    # обнулялась вместе с ним. Circuit breaker тут не поможет: он считает отказы
    # по ПРОВАЙДЕРУ, и разомкнуть цепь на openrouter из-за одной модели значило
    # бы отключить заодно живые.
    _LC2.reset_availability()
    dead, order = chain[0].model, []

    def _remember(spec, role, messages, **kw):
        order.append(spec.model)
        if spec.model == dead:
            raise ProviderUnavailable("404 No endpoints found that support tool use")
        return LLMResponse(text="готов", role=role, provider=spec.provider, model=spec.model)

    fresh = _LC2(cloud)
    fresh._chat_with = _remember                                # noqa: SLF001
    for _ in range(4):
        fresh.chat("M_balanced", [{"role": "user", "content": "?"}], purpose="t")
    check("мёртвую модель спрашивают один раз, а не каждый ход",
          order.count(dead) == 1, f"{order.count(dead)} обращений: {order}")
    check("остальные ходы сразу идут к живой",
          order.count(chain[1].model) == 4, str(order))

    later = _LC2(cloud)          # новый клиент — как новый кейс в прогоне
    later._chat_with = _remember                                # noqa: SLF001
    later.chat("M_balanced", [{"role": "user", "content": "?"}], purpose="t")
    check("память переживает смену клиента", order.count(dead) == 1, str(order))
    _LC2.reset_availability()

    get_config(reload=True)      # вернуть профиль по умолчанию следующим проверкам
    check("в профиле local все модели локальные",
          all(cfg.models_cfg["models"][r]["provider"] == "ollama"
              for r in cfg.models_cfg["profiles"]["local"].values()))

    print("\nРоутинг")
    intents = cfg.intents()
    check("намерений не меньше десяти", len(intents) >= 10, str(len(intents)))
    bad_role, bad_source = [], []
    for i in intents:
        r = cfg.route_for(i)
        if r["role"] not in ("M_fast", "M_balanced", "M_reason", "M_code"):
            bad_role.append(i)
        for s in r["required_sources"]:
            if s not in cfg.routing["sources"]:
                bad_source.append(f"{i}:{s}")
    check("у каждого намерения известная роль", not bad_role, str(bad_role))
    check("каждый обязательный источник имеет инструмент", not bad_source, str(bad_source))
    check("неизвестное намерение уходит в общий класс",
          cfg.route_for("НЕТ_ТАКОГО")["intent"] == "GENERAL_LOGIC_EXPLANATION")
    check("ORDER_LOOKUP идёт в дешёвую модель", cfg.route_for("ORDER_LOOKUP")["role"] == "M_fast")
    check("сверка документации идёт в сильную модель",
          cfg.route_for("DOC_CODE_CONSISTENCY_CHECK")["role"] == "M_reason")
    check("объяснение кода reason требует исходников",
          "code" in cfg.route_for("CONSTRAINT_EXPLANATION")["required_sources"])

    print("\nСхема разбора запроса")
    schema = prompt.schema_for(cfg.routing["intents"])
    check("enum намерений подставлен в схему",
          schema["properties"]["intent"]["enum"] == list(prompt.classifiable(
              cfg.routing["intents"])),
          "в enum идут только классы вопросов, без внутренних ветвей графа")
    check("схема закрыта от лишних полей", schema["additionalProperties"] is False)

    print("\nТрасса")
    trace = Trace(runs_dir=runs, question="тест", task_file="t.xlsx",
                  max_field_chars=40, console=False, profile=cfg.profile)
    trace.event("probe", api_key="sk-or-v1-СЕКРЕТ", long="я" * 200)
    with trace.step("nop"):
        pass
    trace.tool_call(tool="read_plan", args={"order": "Z-1060"}, status="ok",
                    source="Все_ПП, строка 42", latency_ms=3)
    trace.llm_call(role="M_fast", model="m", provider="p", purpose="t",
                   tokens_in=10, tokens_out=5, cost_usd=0.0001, latency_ms=7)
    summary = trace.summary(status="ok")
    lines = [json.loads(x) for x in trace.path.read_text(encoding="utf-8").splitlines()]
    probe = next(x for x in lines if x["kind"] == "probe")
    check("файл трассы создан", trace.path.exists())
    check("ключ доступа вырезан", probe["api_key"] == "***")
    check("длинное поле обрезано", len(probe["long"]) < 80, f"{len(probe['long'])} симв.")
    check("шаг записан парой start/end",
          sum(1 for x in lines if x["kind"].startswith("step.")) == 2)
    check("счётчики собраны", summary["tool_calls"] == 1 and summary["tokens_in"] == 10)
    check("источник попал в сводку", "Все_ПП, строка 42" in summary["sources"])

    print("\nУзел classify")
    state = AgentState(question="Почему заказ Z-1060 не поставлен на линию ЛП2?",
                       task_file=cfg.stand.default_task)
    fake = FakeLLM({
        "intent": "ORDER_EQUIPMENT_EXPLANATION",
        "entities": {"order_number": "Z-1060", "line": "ЛП2", "stage": "печать",
                     "left_neighbor": None, "right_neighbor": None, "kind": None,
                     "sort": None, "caliber": None, "color": None, "print_type": None,
                     "nsi_table": None, "due_date": None},
        "ambiguity": {"is_ambiguous": False, "question": None},
        "reason_summary": "спрашивают о выборе оборудования для конкретного заказа",
    })
    t2 = Trace(runs_dir=runs, question=state.question,
               task_file=state.task_file, console=False)
    state = dataclasses.replace(state, **classify(state, Deps(cfg=cfg, client=fake, trace=t2)))
    check("роль M_fast вызвана один раз", fake.calls == 1 and fake.last["role"] == "M_fast")
    check("запрошен структурированный вывод", "json_schema" in fake.last["kw"])
    check("намерение распознано", state.intent == "ORDER_EQUIPMENT_EXPLANATION")
    check("пустые сущности отброшены", set(state.entities) == {"order_number", "line", "stage"})
    check("маршрут проставлен", state.role == "M_balanced")
    check("источники маршрута проставлены",
          set(state.required_sources) == {"plan", "task", "nsi"})
    from tools.registry import tools_for_sources
    check("инструменты выведены из источников",
          "read_plan" in tools_for_sources(state.required_sources))

    state2 = AgentState(question="Почему заказ не там стоит?", task_file="t.xlsx")
    fake2 = FakeLLM({
        "intent": "ORDER_POSITION_EXPLANATION",
        "entities": {k: None for k in prompt.SCHEMA["properties"]["entities"]["required"]},
        "ambiguity": {"is_ambiguous": True, "question": "Укажите номер заказа."},
        "reason_summary": "нет номера заказа",
    })
    t3 = Trace(runs_dir=runs, question=state2.question,
               task_file="t.xlsx", console=False)
    state2 = dataclasses.replace(state2, **classify(state2, Deps(cfg=cfg, client=fake2, trace=t3)))
    check("неоднозначный запрос уходит в ветку уточнения", state2.status == "clarify")
    check("сформулирован ровно один уточняющий вопрос",
          state2.answer == "Укажите номер заказа.")

    print("\nЖёсткий дедлайн запроса к модели")
    spec = ModelSpec(ref="fake", role="M_reason", provider="fake", model="fake-1",
                     base_url="http://127.0.0.1:1", api_key="none")
    slow = LLMClient(cfg)
    slow.hard_timeout, slow.timeout_retries = 0.2, 0
    slow._client = lambda _spec: _SleepingProvider(5.0)          # noqa: SLF001
    t_dl = time.perf_counter()
    try:
        slow._create(spec, {"model": "fake-1", "messages": []})   # noqa: SLF001
        timed_out = False
    except LLMTimeout:
        timed_out = True
    spent = time.perf_counter() - t_dl
    check("зависший запрос обрывается по дедлайну", timed_out)
    check("основной поток не ждёт дольше дедлайна", spent < 2.0, f"{spent:.2f} с")

    fast = LLMClient(cfg)
    fast.hard_timeout = 5.0
    fast._client = lambda _spec: _SleepingProvider(0.0)           # noqa: SLF001
    check("обычный запрос дедлайном не задет",
          fast._create(spec, {"model": "fake-1", "messages": []}) == "ответ")

    print("\nЗатирание секретов не съедает метрики")
    from observability.trace import _is_secret                    # noqa: E402
    check("ключи и токены доступа затираются",
          all(_is_secret(k) for k in ("api_key", "access_token", "authorization",
                                      "OPENROUTER_API_KEY")))
    check("счётчики токенов остаются видны",
          not any(_is_secret(k) for k in ("tokens_in", "tokens_out", "max_tokens")),
          "памятка просит логировать количество токенов")
    t_sec = Trace(runs_dir=runs, question="тест", task_file="t.xlsx", console=False)
    rec = t_sec.event("llm", tokens_in=120, tokens_out=45, api_key="sk-secret-value")
    check("в записи трассы токены — числа, ключ — «***»",
          rec["tokens_in"] == 120 and rec["api_key"] == "***", str(rec)[:90])

    print("\nЗнание о стенде живёт в конфиге, а не в коде агента")
    check("служебные годы прочитаны из конфига стенда",
          set(cfg.stand.service_years) == {2070, 2099, 2100},
          str(cfg.stand.service_years))
    facts = prompt.stand_facts(cfg.stand)
    check("годы подставлены в промпт, а не вписаны в него", "2070" in facts, facts[:70])
    check("толкования года в промпте НЕТ — оно читается из стенда",
          "блок не набран" not in facts and "отложен" not in facts, facts[-160:])
    import agent.prompts.classify as _cl
    check("в тексте промпта чисел стенда нет",
          "2070" not in _cl.SYSTEM and "Без блока" not in _cl.SYSTEM)

    class _OtherStand:
        pseudo_lines = ["Резерв"]
        service_years = {2088: "special"}
    other = prompt.stand_facts(_OtherStand())
    check("другой стенд даёт другие факты",
          "2088" in other and "2070" not in other, other[:70])

    from tools.registry import execute as _exec                  # noqa: E402
    r = _exec("read_stand_config", {"section": "service_years"}, cfg=cfg)
    check("конфиг стенда читается инструментом", r.ok, r.error or "")
    frag = (r.payload or {}).get("фрагмент_файла", "")
    check("смысл года взят из комментария стенда", "блок не набран" in frag,
          frag[:60])
    check("у фрагмента есть координата",
          "строки" in (r.payload or {}).get("_locator", ""),
          str((r.payload or {}).get("_locator")))
    check("раскладка кода этапов тоже из конфига",
          cfg.stand.stage_code.get("кольцевание") == ["demo/ringing.py"],
          str(cfg.stand.stage_code))

    print("\nВнутренние ветки не предлагаются классификатору")
    offered = prompt.schema_for(cfg.routing["intents"])["properties"]["intent"]["enum"]
    check("список классов не пуст", len(offered) >= 8, str(len(offered)))
    check("ветка обращения в поддержку исключена",
          "SUPPORT_TICKET_GENERATION" not in offered, str(offered))
    check("она же не печатается в промпте",
          "SUPPORT_TICKET_GENERATION" not in
          prompt.build_messages("вопрос", "t.xlsx", cfg.routing["intents"])[0]["content"])
    check("маршрут для неё при этом остался",
          cfg.route_for("SUPPORT_TICKET_GENERATION")["role"] == "M_balanced")

    print("\nОтказ провайдера с кодом 200")
    from llm.client import EmptyResponse, LLMClient as _LC       # noqa: E402

    class _Raw:
        def __init__(self, choices, error=None):
            self.choices = choices
            if error is not None:
                self.error = error

    spec_b = cfg.model_by_ref("or_balanced")
    try:
        _LC._normalize(_Raw(None, {"message": "No allowed providers", "code": 502}),
                       spec_b, "M_balanced", 10, 0)                # noqa: SLF001
        detail, caught = "исключения не было", False
    except EmptyResponse as exc:
        detail, caught = str(exc), True
    check("пустой ответ распознан, а не уронил TypeError", caught, detail[:70])
    check("причина от провайдера показана", "No allowed providers" in detail)
    check("сказано, где менять модель", "config/models.yaml" in detail)
    try:
        _LC._normalize(_Raw([]), spec_b, "M_balanced", 10, 0)      # noqa: SLF001
        empty_ok = False
    except EmptyResponse:
        empty_ok = True
    check("пустой список вариантов тоже не проходит", empty_ok)

    print("\nПроверка длины контекста")
    from app.runner import CONTEXT_MARKER, _probe_context      # noqa: E402,SLF001

    class _Echo:
        """Модель, которая видит весь промпт и повторяет метку."""

        def __init__(self, answer: str):
            self.answer = answer
            self.spent_usd = 0.0

        def chat(self, role, messages, **kw):
            self.seen = "\n".join(m.get("content", "") for m in messages)

            class R:
                text = self.answer
                data = None
            return R()

    status, detail = _probe_context(_Echo(f"Кодовое слово: {CONTEXT_MARKER}"), "M_balanced")
    check("целый промпт признан целым", status == "ok", detail)
    status, detail = _probe_context(_Echo("Кодового слова в задании нет"), "M_balanced")
    check("обрезанный промпт замечен", status == "обрезан", detail)
    check("в сообщении названа причина и лечение",
          "OLLAMA_CONTEXT_LENGTH" in detail, detail)
    probe = _Echo(f"{CONTEXT_MARKER}")
    _probe_context(probe, "M_balanced")
    check("метка стоит в начале промпта, а вопрос в конце",
          probe.seen.index(CONTEXT_MARKER) < len(probe.seen) // 10,
          f"позиция {probe.seen.index(CONTEXT_MARKER)} из {len(probe.seen)}")

    print("\nЗамыкание уточняющего вопроса")
    # Ветка clarify задавала вопрос и на этом прогон заканчивался: пользователь
    # отвечал «Z-1060», начинался новый прогон, и он уже не знал, о чём сам
    # спрашивал. Склейка замыкает круг — но переносит ТОЛЬКО формулировку.
    from app import followup                                    # noqa: E402
    merged = followup.merge("Почему заказ не туда встал?", "Z-1060")
    check("ответ склеен с исходным вопросом",
          "Почему заказ не туда встал?" in merged and "Z-1060" in merged, merged)
    check("пустой ответ не портит вопрос",
          followup.merge("Где заказ Z-1001?", "   ") == "Где заказ Z-1001?")
    check("вопрос без ответа остаётся собой",
          followup.merge("", "Z-1060") == "Z-1060")
    check("ждущий ответа вопрос виден только после clarify",
          followup.pending({"status": "clarify", "question": "Почему заказ не туда встал?"})
          == "Почему заказ не туда встал?")
    check("после обычного ответа ничего не ждём",
          followup.pending({"status": "confirmed", "question": "x"}) is None
          and followup.pending(None) is None)
    check("круг уточнений ровно один", followup.MAX_ROUNDS == 1,
          str(followup.MAX_ROUNDS))
    # Главное ограничение: между ходами переносится вопрос, но не доказательства.
    # Унаследованный факт не имеет координаты в доказательной базе нового прогона,
    # и механические проверки этого прогона его не видели.
    import inspect                                              # noqa: E402
    source = inspect.getsource(followup)
    check("модуль склейки не трогает факты и источники",
          not any(w in source.replace("не вправе", "") for w in
                  ("evidence", "locator", "sources_seen", "state.")),
          "в модуле не должно быть работы с доказательной базой")
    check("склеенный вопрос — обычная строка, проходящая входной шлюз",
          isinstance(merged, str))

    print("\nСостояние агента")
    st = AgentState(required_sources=["plan", "nsi", "code"])
    st.add_evidence(Evidence(claim="заказ на ЛП1", source="plan", locator="Все_ПП, строка 42"))
    check("источник отмечен как просмотренный", st.sources_seen == ["plan"])
    check("недостающие источники видны", st.missing_sources() == ["nsi", "code"])

    print()
    if FAILED:
        print(f"Провалено проверок: {len(FAILED)} — {', '.join(FAILED)}")
        return 1
    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
