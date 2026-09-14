# -*- coding: utf-8 -*-
"""
read_logs — события расчёта по заказу.

SOP. Лог отвечает на вопрос «что система делала с заказом по ходу расчёта и в
каком порядке». Это вспомогательный источник: он подтверждает факт и время
события, но не является нормой. Если лога нет или в нём пусто — это не ошибка,
а отсутствие подтверждения; агент обязан сказать об этом прямо, а не додумывать.

Выборка ограничена: инструмент возвращает строки по заказу или по шаблону, а не
файл целиком.
"""
from __future__ import annotations

import re

from core.config import Config
from tools.base import ToolResult, normalize_key, read_text
from tools.errors import ToolInputError, ToolNotFound

EVENT_RE = re.compile(r"INFO\s+(?P<event>[А-ЯЁ]+(?:\s+[А-ЯЁ]+)*)")


def read_logs(cfg: Config, *, order_number: str | None = None, pattern: str | None = None,
              task_file: str | None = None, limit: int = 40) -> ToolResult:
    if not order_number and not pattern:
        raise ToolInputError(
            "Нужен номер заказа или шаблон поиска",
            hint="Передайте order_number или pattern, например «СНЯТ ПО НСИ»",
        )

    task_file = task_file or cfg.stand.default_task
    path = cfg.stand.log_for(task_file)
    lines = read_text(path)
    source = f"лог прогона {path.name}"

    needles = []
    if order_number:
        needles.append(normalize_key(order_number))
    if pattern:
        needles.append(normalize_key(pattern))

    hits, locators = [], []
    for n, raw in enumerate(lines, start=1):
        key = normalize_key(raw)
        if all(needle in key for needle in needles):
            loc = f"{path.name}, строка {n}"
            m = EVENT_RE.search(raw)
            hits.append({"строка": n, "событие": m.group("event") if m else None,
                         "текст": raw.strip(), "_locator": loc})
            locators.append(loc)
            if len(hits) >= limit:
                break

    if not hits:
        raise ToolNotFound(
            f"В логе {path.name} нет записей по запросу",
            hint="Отсутствие записи — тоже факт: заказ мог не дойти до этапа, "
                 "на котором пишется событие. Проверьте read_plan и read_task.",
            source=path.name,
        )

    events = sorted({h["событие"] for h in hits if h["событие"]})
    payload = {"файл": path.name, "найдено": len(hits), "события": events, "строки": hits}
    return ToolResult(tool="read_logs", payload=payload, source=source, locators=locators)
