# -*- coding: utf-8 -*-
"""
read_plan — что система на самом деле сделала с заказом.

SOP. Инструмент отвечает на вопрос «каково фактическое решение САП». Единица
строки результата — производственная партия (ПП), то есть пара «заказ × этап»,
поэтому один заказ даёт столько записей, сколько этапов в его маршруте.

Три режима:
  * по заказу      — все его ПП по этапам, при необходимости с соседями по очереди;
  * по линии       — очередь партий на линии на выбранном этапе;
  * замечания НСИ  — строки листа «Ошибки НСИ» по заказу.

Готовых вердиктов «почему линия не выбрана» в результате нет и не должно быть:
вывод строит агент из НСИ и регламента. Колонка «ДопустимыеЛинии» — это факт
из справочника, а не объяснение.
"""
from __future__ import annotations

import pandas as pd

from core.config import Config
from tools.base import ToolResult, clean, excel_row, normalize_key, read_sheet, record
from tools.errors import ToolInputError, ToolNotFound

# Колонки решения: то, ради чего инструмент вызывают
DECISION_COLS = [
    "НомерЗаказа", "ПорядокПП", "Этап", "Линия",
    "ДатаНачалаПП", "ДатаОкончанияПП", "Длительность, ч", "ГодГотовности",
    "Потери, кг", "Отход печати, км", "ВремяВыдержки, сут", "ДопустимыеЛинии",
    "ОбъёмТиповогоБлока", "МинТиповойБлок", "ОбъёмЦветовогоБлока", "МинЦветовойБлок",
    "ЕстьБлок", "ЖелаемаяДатаГотовности", "РасчётнаяДатаГотовности",
    "Опоздание, дн", "Причина",
]
# Параметры продукции: дублируют задание, поэтому по умолчанию не отдаются
PRODUCT_COLS = ["Подразделение", "Вид", "Тип", "Калибр", "Цвет", "Толщина, мкм",
                "Услуги", "ВидПечати", "ДиаметрКольца", "Эксклюзивность", "Приоритет",
                "ЗаказанноеКоличество", "Объём, км"]


def _load(cfg: Config, task_file: str | None, sheet_key: str = "all_pp"):
    task_file = task_file or cfg.stand.default_task
    path = cfg.stand.result_for(task_file)
    sheet = cfg.stand.sheets[sheet_key] if sheet_key != "stages" else None
    df = read_sheet(path, sheet)
    return df, path, f"{path.name} / лист «{sheet}»"


def _loc(path_name: str, sheet: str, idx) -> str:
    return f"{path_name} / «{sheet}», строка {excel_row(idx)}"


def read_plan(cfg: Config, *, order_number: str | None = None, line: str | None = None,
              stage: str | None = None, task_file: str | None = None,
              with_neighbors: bool = True, with_nsi_errors: bool = True,
              full: bool = False, limit: int = 20) -> ToolResult:
    if not order_number and not line:
        raise ToolInputError(
            "Нужен либо номер заказа, либо линия",
            hint="Передайте order_number, чтобы найти партии заказа, "
                 "или line + stage, чтобы посмотреть очередь на линии",
        )

    df, path, source = _load(cfg, task_file)
    sheet = cfg.stand.sheets["all_pp"]
    cols = DECISION_COLS + (PRODUCT_COLS if full else [])

    if order_number:
        return _by_order(cfg, df, path, sheet, source, cols, order_number, stage,
                         with_neighbors, with_nsi_errors, task_file)
    return _by_line(df, path, sheet, source, cols, line, stage, limit)


# ------------------------------------------------------------------ по заказу
def _by_order(cfg, df, path, sheet, source, cols, order_number, stage,
              with_neighbors, with_nsi_errors, task_file) -> ToolResult:
    key = normalize_key(order_number)
    hits = df[df["НомерЗаказа"].map(normalize_key) == key]
    # Фильтр этапа НЕ сужает выдачу по заказу. Маршрут заказа состоит из нескольких
    # партий, и решающий факт часто лежит в соседней: вопрос «почему не на ЛП2»
    # про печать, а спрашивающий может запросить экструзию — и тогда допустимые
    # линии печати просто не попадут в ответ. Этап лишь помечается как запрошенный.
    asked_stage = normalize_key(stage) if stage else None
    if hits.empty:
        raise ToolNotFound(
            f"Заказа {order_number} нет в результате расчёта" + (f" на этапе «{stage}»" if stage else ""),
            hint="Проверьте, попал ли заказ во входное задание (read_task) — "
                 "исключённые до оптимизации заказы остаются в результате "
                 "с линией «Отложенные»",
        )

    hits = hits.sort_values("ПорядокПП")
    if asked_stage and not any(normalize_key(v) == asked_stage for v in hits["Этап"]):
        raise ToolNotFound(
            f"У заказа {order_number} нет партии на этапе «{stage}»",
            hint="Этапы этого заказа: "
                 + ", ".join(sorted({str(v) for v in hits["Этап"]})),
        )
    locators, items = [], []
    for idx, row in hits.iterrows():
        loc = _loc(path.name, sheet, idx)
        locators.append(loc)
        item = record(row, loc, cols)
        if with_neighbors:
            item["соседи_по_очереди"] = _neighbors(df, path, sheet, row)
        if asked_stage:
            item["запрошенный_этап"] = normalize_key(row["Этап"]) == asked_stage
        items.append(item)

    payload = {"заказ": str(order_number), "партии": items, "этапов": len(items),
               "маршрут": [i["Этап"] for i in items]}
    if stage:
        payload["примечание_этап"] = (
            f"запрошен этап «{stage}», но возвращены все партии заказа: решающий факт "
            f"часто лежит в соседней партии маршрута")
    placed = [i for i in items if i.get("Линия") not in cfg.stand.pseudo_lines]
    payload["размещён"] = bool(placed)
    if not placed:
        payload["примечание"] = (
            "Ни одна партия заказа не попала в расписание: линия служебная. "
            "Причина — в колонке «Причина», подтверждение искать в НСИ и регламенте."
        )

    if with_nsi_errors:
        errors = _nsi_errors(cfg, path, order_number)
        if errors:
            payload["замечания_НСИ"] = errors
            locators.extend(e["_locator"] for e in errors)

    return ToolResult(tool="read_plan", payload=payload, source=source, locators=locators)


def _neighbors(df, path, sheet, row) -> dict:
    """Предыдущая и следующая партия на той же линии того же этапа."""
    same = df[(df["Этап"] == row["Этап"]) & (df["Линия"] == row["Линия"])]
    same = same.dropna(subset=["ДатаНачалаПП"]).sort_values("ДатаНачалаПП")
    if same.empty or pd.isna(row.get("ДатаНачалаПП")):
        return {"предыдущая": None, "следующая": None,
                "примечание": "партия не поставлена в расписание, очереди нет"}
    order = list(same.index)
    if row.name not in order:
        return {"предыдущая": None, "следующая": None}
    pos = order.index(row.name)
    brief = ["НомерЗаказа", "Линия", "ДатаНачалаПП", "ДатаОкончанияПП",
             "Вид", "Тип", "Калибр", "Цвет", "Потери, кг"]

    def one(i):
        if i < 0 or i >= len(order):
            return None
        j = order[i]
        return record(same.loc[j], _loc(path.name, sheet, j), brief)

    return {"позиция_в_очереди": pos + 1, "всего_в_очереди": len(order),
            "предыдущая": one(pos - 1), "следующая": one(pos + 1)}


def _nsi_errors(cfg, result_path, order_number) -> list[dict]:
    sheet = cfg.stand.sheets["nsi_errors"]
    df = read_sheet(result_path, sheet)
    if df.empty:
        return []
    key = normalize_key(order_number)
    hits = df[df["Номер заказа"].map(normalize_key) == key]
    return [record(r, _loc(result_path.name, sheet, i), list(df.columns))
            for i, r in hits.iterrows()]


# ------------------------------------------------------------------- по линии
def _by_line(df, path, sheet, source, cols, line, stage, limit) -> ToolResult:
    hits = df[df["Линия"].map(normalize_key) == normalize_key(line)]
    if stage:
        hits = hits[hits["Этап"].map(normalize_key) == normalize_key(stage)]
    if hits.empty:
        lines = sorted(str(x) for x in df["Линия"].dropna().unique())
        raise ToolNotFound(
            f"На линии {line} нет партий" + (f" на этапе «{stage}»" if stage else ""),
            hint=f"Линии в результате: {', '.join(lines)}",
        )
    hits = hits.sort_values("ДатаНачалаПП", na_position="last")
    total = len(hits)
    hits = hits.head(limit)
    locators = [_loc(path.name, sheet, i) for i in hits.index]
    items = [record(r, l, cols) for (_, r), l in zip(hits.iterrows(), locators)]
    payload = {"линия": line, "этап": stage, "всего_партий": total,
               "показано": len(items), "очередь": items}
    return ToolResult(tool="read_plan", payload=payload, source=source, locators=locators)
