# -*- coding: utf-8 -*-
"""
read_task — параметры заказа во входном задании.

SOP. Инструмент отвечает на вопрос «что вообще просили сделать»: с какими
параметрами заказ поступил в расчёт, какой у него маршрут (услуги) и срок.
Вызывается первым в любом вопросе про конкретный заказ: если заказа нет в
задании, дальше искать нечего, и это само по себе доказательство.

Адаптер: в стенде задание лежит в Excel, в целевой системе — XML. Формат
скрыт за этой функцией, архитектура агента от него не зависит.
"""
from __future__ import annotations

from core.config import Config
from tools.base import ToolResult, clean, excel_row, normalize_key, read_sheet, record
from tools.errors import ToolInputError, ToolNotFound

COLUMNS = {
    "Номер заказа": "order_number",
    "Подразделение": "division",
    "Вид оболочки": "kind",
    "Тип оболочки": "sort",
    "Калибр": "caliber",
    "Цвет оболочки": "color",
    "Толщина оболочки, мкм": "thickness",
    "Заказанное количество": "order_volume",
    "Дата готовности": "due_date",
    "Услуги": "services",
    "Приоритет": "priority",
    "Эксклюзивность": "exclusivity",
    "Вид печати": "print_type",
    "Диаметр кольца": "ring_diameter",
}


def read_task(cfg: Config, *, order_number: str, task_file: str | None = None) -> ToolResult:
    if not order_number or not str(order_number).strip():
        raise ToolInputError("Не указан номер заказа",
                             hint="Передайте order_number, например Z-1060")

    task_file = task_file or cfg.stand.default_task
    path = cfg.stand.task_for(task_file)
    df = read_sheet(path)
    source = f"задание {path.name}"

    key = normalize_key(order_number)
    mask = df["Номер заказа"].map(normalize_key) == key
    hits = df[mask]

    if hits.empty:
        near = [str(v) for v in df["Номер заказа"].astype(str)
                if str(order_number).strip().upper() in str(v).upper()][:5]
        raise ToolNotFound(
            f"Заказа {order_number} нет в задании {path.name}",
            hint=(f"Похожие номера в задании: {', '.join(near)}" if near else
                  "Проверьте номер заказа или файл задания"),
            # Координата просмотренного источника: отсутствие записи проверяется
            # открытием того же файла, а без ссылки этот факт нечем подтвердить.
            source=source,
        )

    idx = hits.index[0]
    locator = f"{path.name}, строка {excel_row(idx)}"
    row = hits.loc[idx]
    payload = {en: clean(row[ru]) for ru, en in COLUMNS.items() if ru in row.index}
    payload["services_list"] = [s.strip() for s in str(payload.get("services") or "").split(";")
                                if s.strip()]
    payload["_locator"] = locator
    return ToolResult(tool="read_task", payload=payload, source=source, locators=[locator])


def list_tasks(cfg: Config) -> ToolResult:
    """Какие задания вообще есть в стенде — на случай, если пользователь не назвал файл."""
    files = sorted(p.name for p in cfg.stand.tasks_dir.glob("*.xlsx")
                   if not p.name.startswith("~$"))
    return ToolResult(tool="list_tasks", payload={"tasks": files, "default": cfg.stand.default_task},
                      source="каталог заданий")
