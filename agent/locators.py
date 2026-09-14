# -*- coding: utf-8 -*-
"""
Сверка процитированных координат с доказательной базой.

Правило проекта простое: сослаться можно только на то, что действительно
прочитано. Но буквальное сравнение строк ошибается в обе стороны, и одна из
ошибок обидная: модель, сославшись на две соседние строки таблицы, пишет
«строки 3 и 7» вместо двух отдельных координат — смысл верный, текст новый.
Зарубить за это честный ответ — значит наказать за аккуратность изложения.

Поэтому координата разбирается на две части: адрес источника и номера строк.
Адрес обязан совпасть точно — именно он отвечает за то, откуда взят факт.
Номера же достаточно подтвердить: все процитированные строки должны быть
среди прочитанных. Ссылка на пункт регламента, который вообще не открывали,
при этом остаётся неподтверждённой, как и должно быть.
"""
from __future__ import annotations

import re

ROWS_RE = re.compile(r"(строк\w*)", re.IGNORECASE)
NUM_RE = re.compile(r"\d+")


def split_locator(locator: str) -> tuple[str, frozenset[str]]:
    """Адрес источника и номера строк внутри него."""
    parts = ROWS_RE.split(str(locator).strip(), maxsplit=1)
    head = re.sub(r"\s+", " ", parts[0]).strip(" ,;").lower()
    rows = frozenset(NUM_RE.findall(parts[2])) if len(parts) > 2 else frozenset()
    return head, rows


def index(known: set[str] | list[str]) -> dict[str, set[str]]:
    """Адрес источника → все прочитанные в нём номера строк."""
    out: dict[str, set[str]] = {}
    for locator in known:
        head, rows = split_locator(locator)
        out.setdefault(head, set()).update(rows)
    return out


def is_known(cited: str, known_index: dict[str, set[str]]) -> bool:
    head, rows = split_locator(cited)
    if head in known_index:
        return rows <= known_index[head]
    # запасной вариант: адрес записан иначе, но однозначно узнаётся
    for known_head in known_index:
        if head and (head in known_head or known_head in head):
            return rows <= known_index[known_head]
    return False


def unknown(cited: list[str], known: set[str] | list[str]) -> list[str]:
    idx = index(known)
    return [c for c in cited if not is_known(c, idx)]


def known_only(cited: list[str], known: set[str] | list[str]) -> list[str]:
    idx = index(known)
    return [c for c in cited if is_known(c, idx)]

# --- координаты внутри текста ответа -----------------------------------------
# Промпт требует ставить координату в квадратных скобках прямо в тексте, поэтому
# текст можно проверить так же механически, как и список ссылок. Это важнее, чем
# проверка списка: вычеркнуть выдуманную координату из списка мало, если фраза,
# которая на неё опирается, осталась в объяснении.
BRACKET_RE = re.compile(r"\[([^\[\]]{3,300})\]")
SENTENCE_END = ".!?"


def in_text(text: str) -> list[str]:
    """Все координаты, процитированные в тексте (в квадратных скобках)."""
    seen, out = set(), []
    for m in BRACKET_RE.finditer(text or ""):
        value = m.group(1).strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def sentences(text: str) -> list[str]:
    """Делит текст на фразы, не разрезая координаты.

    Наивное деление по точке здесь не годится: точка стоит внутри самих
    координат — «п. 3.1», «табл. 18». Поэтому граница фразы ищется только вне
    квадратных скобок и только там, где следом идёт заглавная буква.
    """
    out, start, depth = [], 0, 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch in SENTENCE_END and depth == 0:
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            if j > i + 1 and (j >= n or text[j].isupper()):
                out.append(text[start:j].strip())
                start = j
                i = j
                continue
        i += 1
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def strike(text: str, bad: list[str]) -> tuple[str, list[str]]:
    """Убирает из текста фразы, опирающиеся на неподтверждённые координаты.

    Крайняя мера: применяется, только если модель и после прямой просьбы оставила
    ссылку на непрочитанный источник. Удалить утверждение честнее, чем оставить
    его без основания, и честнее, чем забраковать весь верный разбор.
    """
    kept, dropped = [], []
    for sentence in sentences(text or ""):
        if any(b in sentence for b in bad):
            dropped.append(sentence.strip())
        else:
            kept.append(sentence)
    return " ".join(p for p in kept if p.strip()).strip(), dropped
