# -*- coding: utf-8 -*-
"""
Чтение конфига стенда как ИСТОЧНИКА, а не как настройки агента.

Зачем отдельный инструмент. Служебные годы готовности, псевдо-линии, состав НСИ
и маппинг колонок — это устройство конкретного стенда. Раньше агент получал их
в готовом виде: «2070 — блок не набран» стояло прямо в промпте разбора запроса.
Так агент знал ответ, не читая источника, и на другом стенде уверенно ошибался
бы теми же словами.

Здесь конфиг возвращается как документ с координатами строк. Смысл служебного
года лежит в комментарии над ключом — в данных его нет, при разборе YAML он
теряется, — поэтому вместе с разобранными значениями отдаётся и сырой фрагмент
файла. Тогда «2099 — заказ отложен» становится фактом с координатой
«config.yaml, строки 106-113», а не утверждением из промпта.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from core.config import Config
from tools.base import ToolResult
from tools.errors import ToolAccessError, ToolNotFound

# Раньше здесь лежал список из девяти разделов с моими пояснениями: «служебные
# годы готовности», «псевдо-линии неразмещённых партий». Два изъяна, и оба
# принципиальные. Во-первых, пояснения уходили модели как «о чём» — то есть
# агент показывал пользователю МОИ формулировки вместо того, что сказала о себе
# сама система. Во-вторых, список работал как белый: раздела, которого я не
# предусмотрел, для агента не существовало, и на другом стенде он ослеп бы на
# всё незнакомое.
#
# Разделы теперь берутся из самого файла, а объяснение — из комментария, который
# стенд написал над ключом. Если комментария нет, честнее показать пустоту, чем
# мою догадку: пустое поле видно, а выдуманное объяснение выглядит как знание.

CONTEXT_LINES = 6      # сколько строк комментария над ключом попадает в выдачу


def _own_comment(lines: list[str], key: str) -> str:
    """Что стенд сам написал о разделе — комментарий над ключом, без моих слов."""
    start = next((i for i, ln in enumerate(lines) if ln.startswith(f"{key}:")), None)
    if start is None:
        return ""
    # Только то, что написано НАД ключом: комментарии внутри блока поясняют
    # отдельные строки, а не раздел целиком, и в сводке выглядят бессмыслицей.
    said: list[str] = []
    i = start - 1
    while i >= 0 and lines[i].lstrip().startswith("#") and start - i <= CONTEXT_LINES:
        text = lines[i].lstrip("# ").strip()
        if text and not set(text) <= {"=", "-", "─"}:      # рамки-разделители
            said.insert(0, text)
        i -= 1
    return " ".join(said)[:200]


def _block(lines: list[str], key: str) -> tuple[int, int, str] | None:
    """Фрагмент файла вокруг ключа верхнего уровня, вместе с комментарием над ним."""
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            start = i
            break
    if start is None:
        return None
    head = start
    while head > 0 and lines[head - 1].lstrip().startswith("#") \
            and start - head < CONTEXT_LINES:
        head -= 1
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.startswith((" ", "\t")):        # тело раздела
            end += 1
            continue
        if not line.strip():                    # пустая строка — возможно, конец
            nxt = next((l for l in lines[end + 1:] if l.strip()), "")
            if nxt.startswith("#"):             # дальше комментарий СЛЕДУЮЩЕГО раздела
                break
            end += 1
            continue
        break
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return head + 1, end, "\n".join(lines[head:end])


def read_stand_config(cfg: Config, *, section: str | None = None) -> ToolResult:
    """Раздел конфига стенда: разобранные значения и сам фрагмент файла."""
    path = cfg.stand.config_file
    if not path.exists():
        raise ToolAccessError(f"Конфиг стенда не найден: {path.name}",
                              hint="Проверьте stand.config_file в config/settings.yaml")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ToolAccessError(f"Конфиг стенда не разбирается: {exc}") from exc

    if not section:
        available = [{"раздел": k, "о чём": _own_comment(lines, k)}
                     for k in data if isinstance(k, str)]
        return ToolResult(
            tool="read_stand_config",
            payload={"файл": path.name, "разделы": available,
                     "примечание": "Запросите раздел, чтобы увидеть значения и "
                                   "комментарий стенда к ним."},
            source=f"конфиг стенда {path.name}",
            locators=[f"{path.name}, строка 1"])

    if section not in data:
        raise ToolNotFound(
            f"В конфиге стенда нет раздела «{section}»",
            hint="Доступные разделы: " + ", ".join(str(k) for k in data))

    found = _block(lines, section)
    first, last, fragment = found if found else (1, len(lines), "")
    locator = f"{path.name}, строки {first}-{last}" if found else f"{path.name}"
    return ToolResult(
        tool="read_stand_config",
        payload={"файл": path.name, "раздел": section,
                 "о чём": _own_comment(lines, section),
                 "значения": data[section],
                 # Смысл значений стенд объясняет комментарием, а YAML-разбор
                 # комментарии теряет. Поэтому отдаём и сам фрагмент файла.
                 "фрагмент_файла": fragment,
                 "_locator": locator},
        source=f"конфиг стенда {path.name}",
        locators=[locator])
