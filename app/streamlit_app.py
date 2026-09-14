# -*- coding: utf-8 -*-
"""
Интерфейс агента-объяснителя.

    streamlit run app/streamlit_app.py

Показывает не только ответ, но и то, как он получен: какие источники прочитаны,
на какие координаты опирается вывод, какие шаги прошёл граф и сколько это стоило.
Для объяснителя это не украшение — пользователь должен иметь возможность
проверить вывод, не заглядывая в код.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402

from app.runner import run_question                  # noqa: E402
from core.config import ConfigError, get_config      # noqa: E402

ПРИМЕРЫ = [
    "Почему заказ Z-1060 не поставлен на линию ЛП2?",
    "Почему заказ Z-1010 не попал в расписание и стоит с годом 2070?",
    "Почему у заказа Z-1061 замечание НСИ по срокам хранения до печати?",
    "Регламент требует переходить по калибру от большего к меньшему. Так ли это в системе?",
    "Куда делся заказ Z-1030, его нет в расписании?",
]

ИСТОЧНИКИ = {"task": "задание", "plan": "результат расчёта", "nsi": "НСИ",
             "regulations": "регламенты", "code": "код", "logs": "логи"}

СТАТУС = {
    "confirmed": ("✅", "Решение системы подтверждено источниками"),
    "conflict": ("⚠️", "Источники расходятся — подготовлено обращение в поддержку"),
    "insufficient": ("🔍", "Причина не подтверждена по доступным данным"),
    "clarify": ("❓", "Нужно уточнение"),
    "error": ("⛔", "Прогон прерван"),
}


def read_trace(path: str | None) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def sidebar(cfg):
    st.sidebar.header("Настройки прогона")
    profiles = list(cfg.models_cfg["profiles"])
    profile = st.sidebar.selectbox("Профиль моделей", profiles,
                                   index=profiles.index(cfg.profile))
    tasks = sorted(p.name for p in cfg.stand.tasks_dir.glob("*.xlsx")
                   if not p.name.startswith("~$"))
    task = st.sidebar.selectbox("Задание", tasks,
                                index=tasks.index(cfg.stand.default_task)
                                if cfg.stand.default_task in tasks else 0)
    st.sidebar.caption(
        f"Стенд: `{cfg.stand.root.name}`  \n"
        f"Память: `{cfg.settings['memory']['backend']}`  \n"
        f"Лимиты: {cfg.limits['max_iterations']} итераций, "
        f"{cfg.limits['max_tool_calls']} вызовов, "
        f"${cfg.limits['max_cost_usd']} на прогон")
    st.sidebar.markdown(
        "---\nАгент работает **только на чтение**: он объясняет решения системы "
        "планирования и ничего в ней не меняет.")
    return profile, task


def show_answer(result: dict):
    icon, caption = СТАТУС.get(result["status"], ("•", result["status"]))
    st.subheader(f"{icon} {caption}")
    st.markdown(result["answer"].replace("\n", "  \n"))
    if result.get("ticket"):
        with st.expander("Черновик обращения в поддержку", expanded=True):
            st.code(result["ticket"], language=None)
    for note in result.get("security_events") or []:
        st.warning(note)


def show_evidence(result: dict):
    evidence = result.get("evidence") or []
    if not evidence:
        st.info("Фактов собрать не удалось.")
        return
    st.caption(f"Каждое утверждение ответа опирается на одну из этих записей. "
               f"Всего фактов: {len(evidence)}")
    rows = [{"Источник": ИСТОЧНИКИ.get(e["source"], e["source"]),
             "Координата": e["locator"], "Факт": e["claim"]} for e in evidence]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def show_trace(events: list[dict]):
    if not events:
        st.info("Трасса недоступна.")
        return
    for e in events:
        kind = e["kind"]
        ms = e["elapsed_ms"]
        if kind == "decision":
            st.markdown(f"`{ms:>6} мс`  **{e['node']}** → `{e['action']}`  \n"
                        f"&nbsp;&nbsp;&nbsp;&nbsp;{e.get('reason_summary', '')}",
                        unsafe_allow_html=True)
        elif kind == "tool":
            mark = "✅" if e["status"] == "ok" else "⚠️"
            st.markdown(f"`{ms:>6} мс`  {mark} инструмент **{e['tool']}** — {e['status']}  \n"
                        f"&nbsp;&nbsp;&nbsp;&nbsp;`{json.dumps(e.get('args'), ensure_ascii=False)[:140]}`"
                        + (f"  \n&nbsp;&nbsp;&nbsp;&nbsp;{e['error']}" if e.get("error") else ""),
                        unsafe_allow_html=True)
        elif kind == "llm":
            st.markdown(f"`{ms:>6} мс`  🧠 {e['role']} · {e['model']} — {e['purpose']}, "
                        f"{e['latency_ms']} мс")
        elif kind == "limit":
            st.markdown(f"`{ms:>6} мс`  ⛔ сработало ограничение **{e['limit']}** = {e['value']}")
        elif kind == "security":
            st.markdown(f"`{ms:>6} мс`  🛡 событие безопасности: {e.get('event_type')}")


def show_metrics(result: dict):
    m = result.get("metrics") or {}
    c = st.columns(5)
    c[0].metric("Время", f"{m.get('duration_ms', 0) / 1000:.1f} с")
    c[1].metric("Итераций", m.get("iterations", 0))
    c[2].metric("Инструментов", m.get("tool_calls", 0))
    c[3].metric("Токенов", f"{m.get('tokens_in', 0)}/{m.get('tokens_out', 0)}")
    c[4].metric("Стоимость", f"${m.get('cost_usd', 0):.4f}")


def main():
    st.set_page_config(page_title="Объяснитель решений САП", page_icon="🏭", layout="wide")
    st.title("Почему система так спланировала")
    st.caption("Агент объясняет решения системы автоматического планирования "
               "и показывает, на каких источниках построен вывод")

    try:
        cfg = get_config()
    except ConfigError as exc:
        st.error(f"Конфигурация неполна: {exc}")
        st.stop()

    profile, task = sidebar(cfg)
    if profile != cfg.profile:
        cfg = get_config(profile, reload=True)

    example = st.selectbox("Примеры вопросов", ["— свой вопрос —"] + ПРИМЕРЫ)
    question = st.text_area(
        "Вопрос", value="" if example.startswith("—") else example, height=80,
        placeholder="Например: почему заказ Z-1060 не поставлен на линию ЛП2?")

    if st.button("Объяснить", type="primary", disabled=not question.strip()):
        with st.spinner("Агент читает задание, план, НСИ и регламенты…"):
            result = run_question(cfg, question.strip(), task, quiet=True)
        st.session_state["result"] = result

    result = st.session_state.get("result")
    if not result:
        return

    show_answer(result)
    st.divider()
    show_metrics(result)
    tabs = st.tabs(["Доказательная база", "Ход рассуждения", "Маршрут", "Трасса целиком"])
    with tabs[0]:
        show_evidence(result)
    with tabs[1]:
        show_trace(read_trace(result.get("trace_file")))
    with tabs[2]:
        route = result.get("route") or {}
        st.write({"Намерение": result.get("intent"),
                  "Роль модели": route.get("role"),
                  "Вспомогательная роль": route.get("companion_role"),
                  "Обязательные источники": [ИСТОЧНИКИ.get(s, s)
                                             for s in route.get("required_sources", [])],
                  "Просмотрено": [ИСТОЧНИКИ.get(s, s) for s in route.get("sources_seen", [])],
                  "Инструменты маршрута": route.get("planned_tools"),
                  "Сущности": result.get("entities")})
        if result.get("verification"):
            st.write("Проверка ответа:", result["verification"])
    with tabs[3]:
        st.code(result.get("trace_file", ""), language=None)
        st.json(read_trace(result.get("trace_file")))


if __name__ == "__main__":
    main()
