# -*- coding: utf-8 -*-
"""
Единый конверт результата инструмента и общие утилиты чтения стенда.

Главное правило проекта живёт здесь: у любого прочитанного факта есть
**координата** — не имя файла, а место внутри него: лист и номер строки,
номер пункта регламента, функция и диапазон строк. Факт без координаты в ответ
не попадёт, поэтому инструменты обязаны её проставлять.
"""
from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from tools.errors import ToolAccessError

Status = Literal["ok", "not_found", "invalid_args", "error"]


@dataclass
class ToolResult:
    """То, что инструмент возвращает агенту и что попадает в трассу."""
    tool: str
    status: Status = "ok"
    payload: Any = None
    source: str = ""                       # человекочитаемый источник: «Все_ПП», «табл. 27»
    locators: list[str] = field(default_factory=list)
    error: str | None = None
    hint: str | None = None                # что делать дальше — подсказка агенту, не пользователю
    suggest: str | None = None             # перенаправление: искомое есть, но в другом месте
    latency_ms: int = 0
    # Обращение к векторной памяти или структурный проход по индексу. Лимит
    # обращений существует, чтобы агент не переформулировал запрос бесконечно;
    # выборка по графу связей и фильтр по метаданным переформулировок не знают,
    # и списывать за них квоту — значит наказывать за точный способ поиска.
    used_retrieval: bool | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_model(self) -> dict:
        """Компактное представление для передачи модели как результата tool call."""
        d: dict[str, Any] = {"status": self.status, "source": self.source}
        if self.payload is not None:
            d["payload"] = self.payload
        if self.locators:
            d["locators"] = self.locators
        if self.error:
            d["error"] = self.error
        if self.hint:
            d["hint"] = self.hint
        if self.suggest:
            d["suggest"] = self.suggest
        return d


# --------------------------------------------------------------------- чтение
_CACHE: dict[tuple, pd.DataFrame] = {}


def read_sheet(path: Path, sheet: str | int | None = None) -> pd.DataFrame:
    """Читает лист Excel с кэшем по времени изменения файла.

    Кэш нужен не ради скорости ради скорости: за один вопрос агент обращается к
    одному и тому же результату расчёта по нескольку раз, и без кэша каждый вызов
    заново распаковывал бы xlsx.
    """
    if not path.exists():
        raise ToolAccessError(
            f"Файл не найден: {path.name}",
            hint="Проверьте, что расчёт выполнен: python main.py в каталоге стенда",
        )
    try:
        key = (str(path), sheet, path.stat().st_mtime_ns)
    except OSError as exc:
        raise ToolAccessError(f"Не удалось прочитать {path.name}: {exc}") from exc
    if key not in _CACHE:
        try:
            _CACHE[key] = pd.read_excel(path, sheet_name=sheet if sheet is not None else 0)
        except PermissionError as exc:
            raise ToolAccessError(
                f"Файл {path.name} занят другим приложением",
                hint="Закройте файл в Excel и повторите",
            ) from exc
        except ValueError as exc:
            raise ToolAccessError(f"В файле {path.name} нет листа «{sheet}»: {exc}") from exc
    return _CACHE[key]


def read_text(path: Path, encoding: str = "utf-8") -> list[str]:
    if not path.exists():
        raise ToolAccessError(f"Файл не найден: {path.name}")
    try:
        return path.read_text(encoding=encoding, errors="replace").splitlines()
    except OSError as exc:
        raise ToolAccessError(f"Не удалось прочитать {path.name}: {exc}") from exc


def excel_row(index: int) -> int:
    """Номер строки в Excel: нумерация с единицы плюс строка заголовка."""
    return int(index) + 2


def clean(value: Any) -> Any:
    """Приводит значения pandas к JSON-совместимым: NaT/NaN → None, Timestamp → строка."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S" if (value.hour or value.minute) else "%Y-%m-%d")
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):
            pass
    return value


def record(row: pd.Series, locator: str, columns: list[str] | None = None) -> dict:
    """Строка таблицы в виде словаря с обязательной координатой."""
    cols = columns or list(row.index)
    out = {c: clean(row[c]) for c in cols if c in row.index}
    out["_locator"] = locator
    return out


# ------------------------------------------------------- сравнение ключей НСИ
# Визуально неразличимые пары: латиница ↔ кириллица. Регламенты прямо требуют
# контролировать это при сверке обозначений (ТР-ЭКС п. 6.4, ТР-ПЕЧ п. 5.3).
CONFUSABLES = {
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У", "I": "І", "3": "З",
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
}


def normalize_key(value: Any) -> str:
    """Нормализует значение ключа: регистр, пробелы и латиница-двойники."""
    s = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    s = " ".join(s.split())
    upper = unicodedata.normalize("NFKC", str(value)).strip()
    mapped = "".join(CONFUSABLES.get(ch, ch) for ch in upper)
    return " ".join(mapped.casefold().split()) or s


def codepoints(value: Any) -> list[str]:
    """Посимвольная раскладка значения: то, чего не видно глазами."""
    out = []
    for ch in str(value):
        try:
            name = unicodedata.name(ch)
        except ValueError:
            name = "БЕЗ ИМЕНИ"
        out.append(f"{ch} U+{ord(ch):04X} {name}")
    return out


def key_difference(requested: Any, candidate: Any) -> dict:
    """Где именно расходятся два визуально одинаковых значения."""
    a, b = str(requested), str(candidate)
    positions = []
    for i in range(max(len(a), len(b))):
        ca = a[i] if i < len(a) else ""
        cb = b[i] if i < len(b) else ""
        if ca != cb:
            positions.append({
                "позиция": i + 1,
                "в запросе": codepoints(ca)[0] if ca else "—",
                "в таблице": codepoints(cb)[0] if cb else "—",
            })
    return {
        "запрошено": a,
        "в таблице": b,
        "визуально совпадают": normalize_key(a) == normalize_key(b),
        "различия": positions,
    }
