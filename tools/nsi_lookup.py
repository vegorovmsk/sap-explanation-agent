# -*- coding: utf-8 -*-
"""
lookup_nsi — точечный запрос к нормативно-справочной информации.

SOP. Таблицы НСИ — структурированные данные, поэтому доступ к ним точечный, а не
через векторный поиск: агент должен получать значение норматива, а не «вспоминать»
его приблизительно. Инструмент отвечает на вопросы вида «допустима ли линия ЛП2
для Флексо-4», «какой минимальный цветовой блок у Синтекс Ск», «есть ли норматив
выработки для калибра 120».

Отдельная обязанность — **диагностика промаха ключа**. Если строка не нашлась,
инструмент не ограничивается ответом «нет данных»: он ищет визуально совпадающего
кандидата и показывает посимвольное различие с кодовыми точками. Регламенты прямо
требуют контролировать латиницу и кириллицу в обозначениях (ТР-ЭКС п. 6.4,
ТР-ПЕЧ п. 5.3), а глазами такое расхождение неразличимо.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from core.config import Config
from tools.base import (ToolResult, clean, excel_row, key_difference, normalize_key,
                        read_sheet, record)
from tools.errors import ToolInputError, ToolNotFound

# Названия приложений взяты из разделов «Приложения» трёх регламентов: именно так
# норма ссылается на таблицу, и по этому же названию агент связывает пункт с НСИ.
# Человеческих названий таблиц здесь больше нет. Они есть в стенде — в разделе
# «Приложения» регламентов, колонка «Наименование приложения» — и собираются при
# индексации в memory/store/tables.json вместе с координатой, по которой их можно
# проверить. Раньше они были переписаны сюда руками: агент показывал пользователю
# мои формулировки как вокабуляр стенда (и, например, называл табл. 6
# «Исключения переходов по виду и типу» там, где регламент пишет «по виду/типу»).

_NAME_RE = re.compile(r"^(\d+)_(.+)\.xlsx$")


@lru_cache(maxsize=8)
def _titles(store_dir: str) -> dict[str, dict]:
    """Названия таблиц, собранные из приложений регламентов при индексации."""
    path = Path(store_dir) / "tables.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=8)
def _files_from_config(config_file: str, root: str) -> dict[str, str]:
    """Номер таблицы → путь, как их перечисляет конфиг СТЕНДА.

    Конфиг — источник правды о составе НСИ: он называет каждый файл и даёт ему
    ключ. Обход каталога глобом это знание воспроизводит, а значит может с ним
    и разойтись — например, если в папке лежит файл, которого в конфиге нет.
    """
    try:
        with open(config_file, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return {}
    # Пути в конфиге стенда даны относительно data_folder, а не корня.
    base = Path(root) / str(raw.get("data_folder") or "")
    out: dict[str, str] = {}
    for rel in (raw.get("input_files") or {}).values():
        m = _NAME_RE.match(Path(str(rel)).name)
        if not m:
            continue
        path = base / str(rel)
        out[m.group(1)] = str(path if path.exists() else Path(root) / str(rel))
    return out


@lru_cache(maxsize=8)
def _catalogue(params_dir: str, store_dir: str = "", config_file: str = "",
               root: str = "") -> dict[str, dict]:
    """Каталог таблиц стенда: номер → файл, слаг, название.

    Состав берётся из конфига стенда, названия — из приложений регламентов.
    Глоб по каталогу остаётся запасным путём: конфига может не быть.
    """
    titles = _titles(store_dir) if store_dir else {}
    from_cfg = _files_from_config(config_file, root) if config_file else {}

    paths: list[Path] = [Path(p) for p in from_cfg.values()]
    if not paths:
        paths = sorted(Path(params_dir).glob("*.xlsx"))

    out: dict[str, dict] = {}
    for path in sorted(paths, key=lambda p: p.name):
        if path.name.startswith("~$") or not path.exists():
            continue
        m = _NAME_RE.match(path.name)
        if not m:
            continue
        number, slug = m.group(1), m.group(2)
        title = (titles.get(number) or {}).get("название") or slug
        out[number] = {"number": number, "slug": slug, "file": path.name,
                       "title": title, "path": str(path),
                       "источник_названия": (titles.get(number) or {}).get("координата", "")}
    return out


def catalogue(cfg) -> dict[str, dict]:
    """Каталог таблиц для текущего стенда."""
    mem = cfg.settings["memory"]["store_dir"]
    return _catalogue(str(cfg.stand.params_dir), str(cfg.root / mem),
                      str(cfg.stand.config_file), str(cfg.stand.root))


def _column_locations(cfg: Config, column: str) -> list[str]:
    """В каких таблицах есть колонка с таким именем."""
    key = normalize_key(column)
    found = []
    for v in sorted(catalogue(cfg).values(), key=lambda x: int(x["number"])):
        try:
            cols = read_sheet(Path(v["path"])).columns
        except Exception:  # noqa: BLE001
            continue
        if any(normalize_key(c) == key or key in normalize_key(c) for c in cols):
            found.append(f"табл. {v['number']} «{v['title']}»")
    return found


def _value_locations(cfg: Config, value: Any, limit: int = 4) -> list[str]:
    """Где во всей НСИ встречается такое значение. Превращает тупик в указатель."""
    key = normalize_key(value)
    if not key or len(key) < 2:
        return []
    found = []
    for v in sorted(catalogue(cfg).values(), key=lambda x: int(x["number"])):
        try:
            df = read_sheet(Path(v["path"]))
        except Exception:  # noqa: BLE001
            continue
        for col in df.columns:
            if df[col].astype(str).map(normalize_key).eq(key).any():
                found.append(f"табл. {v['number']} «{v['title']}», колонка «{col}»")
                break
        if len(found) >= limit:
            break
    return found


def list_nsi_tables(cfg: Config, *, table: str | None = None) -> ToolResult:
    """Каталог таблиц НСИ: номер, название и СОСТАВ КОЛОНОК.

    Колонки отдаются сразу, а не по отдельному запросу, по практической причине:
    без них модель придумывает имена фильтров («Цвет», «НомерЗаказа») и тратит
    итерации на вызовы, которые не могут сработать в принципе.
    """
    cat = catalogue(cfg)
    chosen = [_resolve(cfg, table)] if table else sorted(cat.values(),
                                                         key=lambda x: int(x["number"]))
    items = []
    for v in chosen:
        try:
            columns = [str(c) for c in read_sheet(Path(v["path"])).columns]
        except Exception:  # noqa: BLE001 — каталог не должен падать из-за одного файла
            columns = []
        items.append({"таблица": v["number"], "название": v["title"],
                      "колонки": columns, "файл": v["file"]})
    return ToolResult(tool="list_nsi_tables", payload={"таблицы": items},
                      source="каталог НСИ стенда")


def _resolve(cfg: Config, table: str) -> dict:
    cat = catalogue(cfg)
    t = str(table).strip()
    m = re.search(r"\d+", t)
    if m and m.group(0) in cat:
        return cat[m.group(0)]
    key = normalize_key(t)
    for v in cat.values():
        if key in (normalize_key(v["slug"]), normalize_key(v["title"])):
            return v
    for v in cat.values():
        if key and key in normalize_key(v["title"]):
            return v
    # Номер приложения и номер таблицы — разные нумерации, и регламенты ссылаются
    # именно на приложения: «с учётом приложения 5». Прогон 15.09, кейс B1: агент
    # прочитал это как «табл. 5», трижды спросил несуществующую таблицу и вывел
    # «таблицы 5 в системе нет — значит, нет и нормативной группировки калибров».
    # Ложный вывод из собственной ошибки адресации. Соответствие лежит в
    # регламентах, в колонке «Приложение», и подсказка обязана его назвать.
    by_appendix = _appendix_hint(cfg, table)
    if by_appendix:
        # Приложения нумеруются внутри каждого документа, поэтому «приложение 5»
        # есть и у ТР-ЭКС, и у ТР-ПЕЧ. Выбрать за модель нельзя — она знает, о
        # каком этапе спрашивает, а инструмент нет. Называем все и даём выбрать.
        variants = "; ".join(f"{doc}, приложение {table} — это табл. {num}"
                             for doc, num in by_appendix)
        raise ToolInputError(
            f"Таблицы с номером «{table}» нет. {variants}",
            hint="Регламенты ссылаются на приложения, а не на номера таблиц. "
                 "Повторите вызов с номером таблицы того документа, о котором "
                 "идёт речь.",
        )
    raise ToolInputError(
        f"Неизвестная таблица НСИ: «{table}»",
        hint="Доступные номера: " + ", ".join(sorted(cat, key=int)),
    )


def _appendix_hint(cfg: Config, asked: str) -> list[tuple[str, str]]:
    """Таблицы, лежащие в приложении с таким номером: (документ, номер таблицы).

    Соответствие не выдумано: оно читается из колонки «Приложение» регламентов
    при сборке памяти и лежит в tables.json рядом с названиями таблиц.
    """
    m = re.search(r"\d+", str(asked))
    if not m:
        return []
    wanted = m.group(0)
    store = str(cfg.root / cfg.settings["memory"]["store_dir"])
    out = []
    for number, info in (_titles(store) or {}).items():
        if str(info.get("приложение") or "").strip() == wanted:
            out.append((str(info.get("документ") or "регламент"), str(number)))
    return sorted(out)


# Вычисляемые колонки результата расчёта. В НСИ их нет и быть не может, но
# выглядят они как справочные, и модель регулярно спрашивает их у lookup_nsi.
PLAN_COLUMNS = {
    "допустимыелинии": "ДопустимыеЛинии", "причина": "Причина", "линия": "Линия",
    "порядокпп": "ПорядокПП", "этап": "Этап", "годготовности": "ГодГотовности",
    "опоздание": "Опоздание, дн", "потери": "Потери, кг",
    "объёмцветовогоблока": "ОбъёмЦветовогоБлока", "естьблок": "ЕстьБлок",
}


def _plan_column_hint(name: str) -> str:
    key = normalize_key(name).replace(" ", "").replace(",", "")
    for marker, column in PLAN_COLUMNS.items():
        if key.startswith(marker):
            return (f" Колонка «{column}» относится к результату расчёта, а не к НСИ: "
                    f"её читает read_plan.")
    return ""


def _match_column(df: pd.DataFrame, name: str) -> str:
    key = normalize_key(name)
    for c in df.columns:
        if normalize_key(c) == key:
            return c
    for c in df.columns:
        if key and key in normalize_key(c):
            return c
    raise ToolInputError(
        f"В таблице нет колонки «{name}»",
        hint="Колонки таблицы: " + ", ".join(map(str, df.columns)) + _plan_column_hint(name),
    )


def _apply_caliber(df: pd.DataFrame, caliber: int) -> tuple[pd.DataFrame, str]:
    """Калибр в НСИ задаётся то точным значением, то диапазоном «от/до»."""
    cols = {normalize_key(c): c for c in df.columns}
    frm = cols.get(normalize_key("Калибр от")) or cols.get(normalize_key("Калибр с"))
    to = cols.get(normalize_key("Калибр до")) or cols.get(normalize_key("Калибр по"))
    exact = cols.get(normalize_key("Калибр"))
    if frm and to:
        return df[(df[frm] <= caliber) & (df[to] >= caliber)], f"{frm} ≤ {caliber} ≤ {to}"
    if exact:
        return df[df[exact] == caliber], f"{exact} = {caliber}"
    raise ToolInputError("В этой таблице нет колонки калибра",
                         hint="Колонки: " + ", ".join(map(str, df.columns)))


def _diagnose(df: pd.DataFrame, filters: dict) -> list[dict]:
    """Почему ключ не нашёлся: чужой алфавит, регистр, пробелы или значения нет вовсе."""
    report = []
    for name, value in filters.items():
        try:
            col = _match_column(df, name)
        except ToolInputError:
            continue
        values = df[col].dropna().astype(str).unique().tolist()
        if str(value) in values:
            continue
        target = normalize_key(value)
        twins = [v for v in values if normalize_key(v) == target]
        item: dict[str, Any] = {"колонка": col, "запрошено": str(value)}
        if twins:
            item["вердикт"] = ("значение в таблице визуально совпадает с запрошенным, "
                               "но записано другими символами")
            item["различие"] = key_difference(value, twins[0])
        else:
            item["вердикт"] = "такого значения в колонке нет"
            item["значения_в_колонке"] = values[:15]
            if len(values) > 15:
                item["значения_в_колонке"].append(f"… ещё {len(values) - 15}")
        report.append(item)
    return report


def lookup_nsi(cfg: Config, *, table: str, filters: dict | None = None,
               caliber: int | None = None, columns: list[str] | None = None,
               limit: int = 20) -> ToolResult:
    meta = _resolve(cfg, table)
    path = Path(meta["path"])
    df = read_sheet(path)
    source = f"табл. {meta['number']} «{meta['title']}»"
    applied: list[str] = []

    sub = df
    for name, value in (filters or {}).items():
        col = _match_column(df, name)
        if isinstance(value, (list, tuple, set)):
            wanted = [str(v) for v in value]
            mask = sub[col].astype(str).isin(wanted)
            applied.append(f"{col} ∈ {sorted(wanted)}")
        elif isinstance(value, str):
            mask = sub[col].astype(str) == value
            applied.append(f"{col} = «{value}»")
        else:
            mask = sub[col] == value
            applied.append(f"{col} = {value}")
        # astype(bool) обязателен: на пустом кадре маска приходит с dtype object,
        # и pandas принимает её за выбор колонок, а не за фильтр строк
        sub = sub[mask.astype(bool)]

    if caliber is not None:
        if sub.empty:
            applied.append(f"калибр = {int(caliber)}")
        else:
            sub, expr = _apply_caliber(sub, int(caliber))
            applied.append(expr)

    if sub.empty:
        diagnosis = _diagnose(df, filters or {})
        payload = {"таблица": meta["number"], "название": meta["title"],
                   "фильтры": applied, "строк": 0, "диагностика_ключа": diagnosis}
        # Перенаправляем только при ПРОМАХЕ ЗАПРОСА — когда значения нет в его
        # колонке вовсе. Если значения в колонке есть, а совпадения по их
        # сочетанию нет, это и есть доказательство отсутствия: «ЛЭ3 не производит
        # Демолон Дк калибра 60» — правильная таблица и законный вывод, уводить
        # отсюда в другие таблицы нельзя.
        homoglyph = any("различие" in d for d in diagnosis)
        elsewhere: list[str] = []
        for item in diagnosis:
            if "различие" in item:          # гомоглиф — это дефект НСИ, а не не та таблица
                continue
            elsewhere += [x for x in _value_locations(cfg, item["запрошено"])
                          if f"табл. {meta['number']} " not in x]
        elsewhere = list(dict.fromkeys(elsewhere))[:4]
        if elsewhere and not homoglyph:
            payload["искать_в"] = elsewhere
            return ToolResult(
                tool="lookup_nsi", status="not_found", payload=payload, source=source,
                locators=[f"{source} (совпадений нет)"],
                error=f"В табл. {meta['number']} нет строк по фильтрам: "
                      f"{'; '.join(applied) or '—'}",
                hint="Похоже, спрошена не та таблица.",
                suggest="Эти значения есть в: " + "; ".join(elsewhere)
                        + ". Отсутствие строки в неподходящей таблице ничего не доказывает.")
        hint = (
            "Ключ в таблице записан другими символами — это расхождение НСИ, "
            "а не отсутствие норматива. Сошлитесь на ТР-ЭКС п. 6.4 или ТР-ПЕЧ п. 5.3 "
            "и готовьте обращение в поддержку."
            if homoglyph else
            "Норматива для такого ключа в таблице нет. Отсутствие записи — тоже "
            "доказательство: проверьте, не блокирует ли это запуск по регламенту."
        )
        return ToolResult(
            tool="lookup_nsi", status="not_found", payload=payload, source=source,
            # Координата — человеческое обозначение таблицы, а не имя файла НСИ.
            # Имя файла утекало в доказательства и в текст ответа: агент писал
            # «18_print_equipment_sets.xlsx» там, где пользователь ждёт
            # «табл. 18 «Печатное оборудование»», и золотой набор справедливо
            # считал, что на таблицу не сослались.
            locators=[f"{source} (совпадений нет)"],
            error=f"В табл. {meta['number']} нет строк по фильтрам: {'; '.join(applied) or '—'}",
            hint=hint)

    total = len(sub)
    sub = sub.head(limit)
    # Фильтры строгие, проекция — нет. Фильтр меняет смысл запроса, и молча
    # проглоченная опечатка в нём исказила бы результат. А `columns` только
    # сокращает выдачу: ронять из-за него уже правильный запрос — расточительство.
    # На живом прогоне ровно это и произошло: верный фильтр по табл. 18 был
    # отвергнут из-за лишнего имени в списке колонок.
    cols, unknown = list(df.columns), []
    if columns:
        chosen = []
        for c in columns:
            try:
                chosen.append(_match_column(df, c))
            except ToolInputError:
                unknown.append(str(c))
        # Если хоть одно имя не опознано, проекцию не применяем вовсе и отдаём
        # таблицу целиком: список колонок построен на неверном представлении о
        # ней, и урезанная по нему выдача теряет как раз то, ради чего запрос
        # делался. Справочники маленькие — лишние колонки ничего не стоят.
        cols = list(df.columns) if unknown else chosen
    locators = [f"{source} / {path.name}, строка {excel_row(i)}" for i in sub.index]
    rows = [record(r, l, cols) for (_, r), l in zip(sub.iterrows(), locators)]
    payload = {"таблица": meta["number"], "название": meta["title"], "фильтры": applied,
               "строк": total, "показано": len(rows), "строки": rows}
    if unknown:
        payload["пропущенные_колонки"] = unknown
        payload["примечание_колонок"] = (
            "таких колонок в этой таблице нет, они пропущены: "
            + ", ".join(unknown) + "."
            + "".join(_plan_column_hint(c) for c in unknown))
    return ToolResult(tool="lookup_nsi", payload=payload, source=source, locators=locators)
