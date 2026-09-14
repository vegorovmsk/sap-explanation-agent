# -*- coding: utf-8 -*-
"""Ошибки инструментов.

Инструменты не бросают исключения наружу: реестр ловит их и превращает в
результат со статусом. Различаем три класса, потому что реагировать на них надо
по-разному:

  * ToolInputError    — агент передал неверные аргументы. Повтор бессмысленен,
                        нужно исправить вызов;
  * ToolNotFound      — источник прочитан, но искомого в нём нет. Это не сбой,
                        а факт: отсутствие записи — тоже доказательство;
  * ToolAccessError   — файл недоступен, занят или лежит вне разрешённых путей.
                        Имеет смысл один повтор.
"""
from __future__ import annotations


class ToolError(RuntimeError):
    """Базовая ошибка инструмента."""
    status = "error"
    retryable = False

    def __init__(self, message: str, hint: str | None = None,
                 source: str | None = None):
        super().__init__(message)
        self.hint = hint
        # Координата того, ГДЕ искали. Нужна прежде всего отказу «не найдено»:
        # отсутствие записи — полноценный факт, но проверить его можно, только
        # зная, какой файл открывали. Живой прогон 14.09: агент установил, что
        # заказа Z-0856 нет ни в задании, ни в расчёте, но факт пришёл без
        # координаты, был отброшен как неподтверждённый, и пользователю
        # предложили назвать номер заказа — который он только что назвал.
        self.source = source


class ToolInputError(ToolError):
    status = "invalid_args"
    retryable = False


class ToolNotFound(ToolError):
    status = "not_found"
    retryable = False


class ToolAccessError(ToolError):
    status = "error"
    retryable = True
