# -*- coding: utf-8 -*-
"""
Обход «этап → норма → таблица НСИ → код» по индексам, без участия модели.

Зачем узел, а не подсказка модели. Живые прогоны показали одно и то же: модель
угадывала номер таблицы НСИ, спрашивала не ту таблицу, четыре круга подряд
заявляла «нужна таблица, которую читает ExtrusionStage.run» — и всё это про
связи, которые уже лежат в индексе. Связь «пункт ↔ таблица ↔ функция» строится
при сборке памяти; спрашивать её у модели — значит заменять точное знание
догадкой. Поэтому карта источников этапа собирается кодом и печатается в
задание, а модель занимается тем, чего код не умеет: читает и объясняет.

Что обход НЕ делает. Он не подменяет доказательства. Карта — это указатели:
координаты пунктов, номера таблиц, координаты функций. Тексты по-прежнему
читаются инструментами, и только прочитанное становится фактом с координатой.

Двусторонность обхода — не украшение. Обход только «от кода» делает агента
слепым к расхождениям того вида, ради которых он и нужен: ТР-КОЛ п. 5.1 требует
норматив «для калибра И диаметра кольца», а demo/ringing.py читает табл. 20 по
одному калибру. От кода к таблице всё сходится — таблица та же, поэтому пропуска
не видно. Разница множеств и проверка покрытия параметров показывают его
механически.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from memory.links import LinkGraph
from memory.sparse import terms

# Какой код относится к этапу, какой к предобработке, а какой только выгружает
# результат — знание о конкретном стенде, поэтому оно лежит в config/settings.yaml
# (секция stand), а не здесь. Значения ниже запасные, на случай конфига без этих
# ключей.
#
# Разделение не косметическое. Предобработка входит в проверку покрытия
# параметров (параметр, проверенный там, в отборе участвует), но НЕ входит в
# сравнение множеств таблиц: InputData._load_tables читает все таблицы подряд,
# и сравнение с ним сходится всегда, то есть не значит ничего. А выгрузка
# результата исключена совсем: «ДиаметрКольца» в ней есть, а в отборе линии
# нет, и посчитай мы выгрузку кодом решения — расхождение D3 исчезло бы из виду.
FALLBACK_STAGE_FILES = {
    "экструзия": ("demo/extrusion.py",),
    "печать": ("demo/printing.py",),
    "кольцевание": ("demo/ringing.py",),
}
FALLBACK_SHARED = ("demo/input_data.py",)
FALLBACK_REPORT_ONLY = ("demo/output.py", "demo/synth_data.py")


def _stand_code(cfg) -> tuple[dict, tuple, tuple]:
    """Раскладка кода стенда: этап → файлы, общие файлы, файлы только для отчёта."""
    stand = cfg.stand
    stages = {k: tuple(v) for k, v in (getattr(stand, "stage_code", None) or {}).items()}
    shared = tuple(getattr(stand, "shared_code", None) or ())
    report = tuple(getattr(stand, "report_only_code", None) or ())
    return (stages or FALLBACK_STAGE_FILES,
            shared or FALLBACK_SHARED,
            report or FALLBACK_REPORT_ONLY)

# Фрагменты, которые таблицы перечисляют, а не используют: загрузчик читает всё
# подряд, описание модуля просто рассказывает. Если считать их использованием,
# сравнение множеств сходится всегда и не значит ничего — а описание модуля
# печати вообще упоминает табл. 16 и 17 ровно затем, чтобы сказать, что они
# СОЗНАТЕЛЬНО вырезаны.
ENUMERATING = ("InputData._load_tables", "описание модуля")

# Параметр отбора: как он называется в норме и как — в коде и задании. Слева
# стемы русских слов (нормы пишут «диаметра кольца», «калибра»), справа
# идентификаторы. Совпадение считается по стемам, иначе падежи всё ломают.
# Параметр отбора: как он называется в норме и как — в коде. Раньше здесь лежал
# словарь из одиннадцати записей, написанный руками. Он был лишним: стенд
# публикует это соответствие сам — `column_translation` в его config.yaml, 54
# записи вида «Диаметр кольца → ring_diameter».
#
# Цена рукописного словаря измерена. Он не содержал строки «Блок 3, км →
# min_caliber_block», и обход не видел, что ТР-ЭКС п. 3.5 требует формировать
# блоки по калибрам, табл. 12 даёт для этого колонку, а demo/extrusion.py читает
# только min_common_block и min_color_block. Это расхождение того же рода, что
# «диаметр кольца», и именно о нём спрашивает кейс B3 золотого набора — тот, что
# не проходил ни в одном прогоне. Словарь, написанный по памяти, сделал агента
# слепым к дефекту, на котором его же и проверяют.
#
# Значения ниже запасные — на случай стенда без `column_translation` в конфиге.
FALLBACK_PARAMS: dict[str, dict[str, tuple[str, ...]]] = {
    "калибр": {"норма": ("калибр",), "код": ("caliber", "калибр")},
    "диаметр кольца": {"норма": ("диаметр", "кольц"),
                       "код": ("ring_diameter", "диаметркольца")},
    "вид печати": {"норма": ("вид", "печ"), "код": ("print_type", "видпечати")},
    "объём заказа": {"норма": ("объем",), "код": ("order_volume", "volume", "объем")},
}

_PARAMS_CACHE: dict[str, dict] = {}
_NUMERIC_CACHE: dict[str, set] = {}
_COLUMN_TABLES: dict[str, dict] = {}


def columns_by_table(cfg) -> dict[str, set[str]]:
    """Колонка НСИ → номера таблиц, где она объявлена."""
    _ = numeric_columns(cfg)                 # обе карты строятся одним проходом
    return _COLUMN_TABLES.get(str(getattr(cfg.stand, "params_dir", "")) or "—", {})


def numeric_columns(cfg) -> set[str]:
    """Колонки НСИ с числовыми значениями — порогами, размерами, нормативами.

    Зачем отделять их от текстовых. Обход ищет параметры, которые норма называет,
    а код этапа не читает. Среди колонок есть и ярлыки: «Множество оборудования»
    со значениями «Набор-А», «Набор-Б» — это не условие расчёта, а имя группы, и
    код пользуется её содержимым, не обращаясь к самому имени. Пометив такую
    колонку как непокрытую, обход поднимал ложную тревогу — и не безобидно:
    кандидат в карте заставляет планировщик подключать поиск по нормам и коду на
    вопросах, где это не нужно (замерено на кейсе A1).

    Признак числового значения выбран как отсев, и это эвристика, а не закон.
    Числовой параметр — порог, диаметр, объём блока — норма называет как условие
    расчёта, и его отсутствие в коде означает несовпадение. Текстовый флаг тоже
    может быть условием, поэтому фильтр иногда промолчит там, где стоило бы
    сказать. Смещение выбрано сознательно: ложный кандидат меняет поведение
    агента на посторонних вопросах, пропущенный — лишь не подсказывает модели
    того, что она способна заметить и сама.
    """
    key = str(getattr(cfg.stand, "params_dir", "")) or "—"
    if key in _NUMERIC_CACHE:
        return _NUMERIC_CACHE[key]
    found: set[str] = set()
    where: dict[str, set[str]] = {}
    try:
        import pandas as pd
        from tools.nsi_lookup import catalogue

        for info in catalogue(cfg).values():
            try:
                frame = pd.read_excel(info["path"])
            except Exception:                                # noqa: BLE001
                continue
            for column in frame.columns:
                name = str(column).strip().lower()
                where.setdefault(name, set()).add(str(info["number"]))
                values = frame[column].dropna()
                if not values.empty and pd.api.types.is_numeric_dtype(values):
                    found.add(name)
    except Exception:                                        # noqa: BLE001
        pass
    _NUMERIC_CACHE[key] = found
    _COLUMN_TABLES[key] = where
    return found



def params_for(cfg) -> dict[str, dict[str, tuple[str, ...]]]:
    """Словарь параметров отбора, прочитанный из конфига наблюдаемой системы.

    Русское имя колонки даёт стемы, по которым параметр узнаётся в тексте нормы;
    идентификатор — имя, под которым его следует искать в коде. Обе половины
    берутся из одной строки конфига стенда, поэтому они не могут разойтись.
    """
    key = str(getattr(cfg.stand, "config_file", "")) or "—"
    if key in _PARAMS_CACHE:
        return _PARAMS_CACHE[key]

    table: dict[str, dict[str, tuple[str, ...]]] = {}
    try:
        import yaml

        # Имя раздела — допущение о наблюдаемой системе, и оно объявлено в
        # config/settings.yaml рядом с остальными. В коде агента такому знанию
        # не место: перенастройка на другую систему должна быть правкой конфига,
        # а не правкой исходников.
        section = getattr(cfg.stand, "column_map_section", "") or "column_translation"
        with open(cfg.stand.config_file, encoding="utf-8") as fh:
            translation = (yaml.safe_load(fh) or {}).get(section) or {}
    except (OSError, ValueError, AttributeError):
        translation = {}

    for human, ident in translation.items():
        human = str(human).strip()
        stems = tuple(t for t in terms(human.lower()) if len(t) >= 3)
        if not stems or not isinstance(ident, str):
            continue
        name = human.lower()
        # Одному идентификатору может соответствовать несколько человеческих
        # имён («Вид» и «Вид оболочки» → kind). Берём первое: для узнавания в
        # норме важны стемы, а они у синонимов пересекаются.
        table.setdefault(name, {"норма": stems,
                                "код": (ident, name.replace(" ", ""))})
    _PARAMS_CACHE[key] = table or FALLBACK_PARAMS
    return _PARAMS_CACHE[key]


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get("records", [])


def _norm_code(text: str) -> str:
    """Код для поиска идентификаторов: без подчёркиваний, пробелов и регистра."""
    return text.lower().replace("_", "").replace(" ", "")


def normalize_stage(value: Any) -> str | None:
    """«на экструзии», «Печать», «кольцевании» → ключ этапа."""
    if not value:
        return None
    low = " ".join(str(value).lower().split())
    for stage, marker in (("экструзия", "экструз"), ("печать", "печат"),
                          ("кольцевание", "кольцев")):
        if marker in low:
            return stage
    return None


def stage_from_question(question: str) -> str | None:
    """Этап по тексту вопроса. Слово называется прямо, угадывать нечего."""
    return normalize_stage(question)



def _reverse_translation(cfg) -> dict[str, tuple[str, ...]]:
    """Идентификатор поля расчёта → стемы его человеческого имени.

    Обратная сторона того же соответствия, что система объявляет в своём конфиге.
    Нужна, чтобы понять, о чём параметр: «caliber» → «калибр», и пункт со словом
    «калибр» оказывается тем самым, который этот параметр и требует.
    """
    out: dict[str, set[str]] = {}
    for human, spec in params_for(cfg).items():
        for alias in spec["код"]:
            for token in re.split(r"[_\s]+", str(alias).lower()):
                if len(token) >= 3:
                    out.setdefault(token, set()).update(terms(human))
    return {k: tuple(v) for k, v in out.items()}

def build_map(cfg, stage: str) -> dict:
    """Карта источников этапа: нормы, таблицы, код и кандидаты в расхождения."""
    stage_map_cfg, shared_files, report_only = _stand_code(cfg)
    store = Path(cfg.root) / cfg.settings["memory"]["store_dir"]
    regs = [r for r in _load(store / "regulations.json")
            if r["meta"].get("этап") == stage and r["meta"].get("к_планированию")]
    all_code = [c for c in _load(store / "code.json")
                if c["meta"].get("файл") not in report_only]

    def uses_tables(chunk: dict) -> bool:
        return chunk["meta"].get("полное_имя") not in ENUMERATING

    stage_files = tuple(stage_map_cfg.get(stage, ()))
    decision = [c for c in all_code
                if c["meta"].get("файл") in stage_files and uses_tables(c)]
    covering = decision + [c for c in all_code
                           if c["meta"].get("файл") in shared_files and uses_tables(c)]

    def tables_of(records: list[dict]) -> set[str]:
        out: set[str] = set()
        for r in records:
            out.update(str(t) for t in (r["meta"].get("таблицы_НСИ") or []))
        return out

    # Таблицы, которых в НСИ стенда физически нет, из карты убираются: номер,
    # упомянутый в тексте, ещё не источник. Иначе табл. 16 из описания модуля
    # печати попадает в «код читает, а нормы не ссылаются» — при том, что файла
    # такой таблицы не существует и код её не открывает.
    existing = {m.group(1) for path in Path(cfg.stand.params_dir).glob("*.xlsx")
                if not path.name.startswith("~$")
                for m in [re.match(r"^(\d+)_", path.name)] if m}
    tables_norm = tables_of(regs) & existing
    tables_code = tables_of(decision) & existing
    # Для сравнения «норма есть, кода нет» учитываем и предобработку: параметр,
    # проверенный там, в решении участвует.
    tables_shared = tables_of(covering) & existing

    def order(number: str) -> int:
        return int(number) if number.isdigit() else 999

    tables = []
    for number in sorted(tables_norm | tables_code, key=order):
        clauses = [f"{r['meta'].get('номер_документа')} п. {r['meta'].get('пункт')}"
                   for r in regs if number in tables_of([r])]
        funcs = [c["meta"]["координата"] for c in decision if number in tables_of([c])]
        tables.append({"таблица": number, "пункты": clauses[:6], "функции": funcs[:4]})

    # Проверка покрытия условий. Норма называет параметры отбора; код этапа
    # обязан их читать. Параметр, которого в коде решения нет ни под одним из
    # имён, — кандидат в расхождение, с координатами с обеих сторон.
    code_blob = _norm_code(" ".join(c["text"] for c in covering))
    numbers = numeric_columns(cfg)
    where = columns_by_table(cfg)
    uncovered: list[dict] = []

    # Пункты перебираются так, чтобы рабочая норма шла раньше определения
    # термина. Прогон 15.09, кейс B3: параметр «Блок 3, км» опознаётся по стему
    # «блок», а первым в документе идёт п. 1.1 «Основные термины: Блок —
    # совокупность заказов…». Кандидат ссылался на определение, агент читал
    # пункты про табл. 12 — и заземлить расхождение было нечем.
    #
    # Признак рабочего пункта не выдуман: пункт, ссылающийся на ТУ САМУЮ таблицу,
    # где объявлена колонка, говорит о её применении, а определение термина не
    # ссылается ни на что. Тот же признак делает обращение в поддержку точнее:
    # «ТР-ЭКС п. 3.5 требует формировать блоки по калибрам с учётом приложения 5»
    # проверяемо, «п. 1.1 определяет термин блок» — нет.
    # Ссылки на таблицу мало: пунктов, ссылающихся на одну таблицу, несколько, и
    # выполняются они по-разному. ТР-ЭКС п. 3.2 ссылается на табл. 12 и требует
    # проверять Блок 1 и Блок 2 — код это делает, нарушения нет. Нарушен п. 3.5:
    # «формирование блоков ПО КАЛИБРАМ с учётом приложения 5», а калибрового
    # блока код не читает. Обращение, сославшееся бы на п. 3.2, обвинило бы
    # систему в нарушении того, что она соблюдает, — с настоящей координатой, а
    # потому особенно убедительно и особенно неверно.
    #
    # Различить их можно по смыслу идентификатора: min_caliber_block содержит
    # «caliber», а обратный перевод того же конфига стенда даёт «Калибр» →
    # «калибр». Пункт, где этот стем есть, говорит именно об этом параметре.
    back = _reverse_translation(cfg)

    def sense_stems(spec: dict) -> set[str]:
        """Стемы, раскрывающие смысл параметра: из имени колонки и из кода."""
        out = set(spec["норма"])
        for alias in spec["код"]:
            for token in re.split(r"[_\s]+", str(alias).lower()):
                out.update(back.get(token, ()))
        return out

    def clause_rank(record: dict, tables_of_param: set[str], spec: dict) -> tuple:
        refs = {str(t) for t in (record["meta"].get("таблицы_НСИ") or [])}
        stems = set(terms(record["text"]))
        matched = len(sense_stems(spec) & stems)
        # сначала пункты со ссылкой на таблицу параметра, среди них — те, где
        # смысл параметра раскрыт полнее; при равенстве побеждает порядок
        # документа, поэтому выдача устойчива
        return (0 if refs & tables_of_param else 1, -matched)

    for name, spec in params_for(cfg).items():
        if any(alias in code_blob for alias in map(_norm_code, spec["код"])):
            continue
        if numbers and name not in numbers:
            # Ярлык, а не условие расчёта: код пользуется содержимым группы,
            # не обращаясь к имени колонки. См. numeric_columns().
            continue
        tables_of_param = where.get(name, set())
        matched = [r for r in regs if set(spec["норма"]).issubset(set(terms(r["text"])))]
        if not matched:
            continue
        r = min(matched, key=lambda rec: clause_rank(rec, tables_of_param, spec))
        uncovered.append({
            "параметр": name,
            "пункт": f"{r['meta'].get('номер_документа')} п. {r['meta'].get('пункт')}",
            "координата_нормы": r["meta"].get("координата") or "",
            "код_этапа": list(stage_files),
            # Координаты функций, а не только имя файла: чтобы заявить пропуск,
            # надо показать МЕСТО, где норме полагалось быть. «Параметра нет в
            # demo/ringing.py» проверить нельзя, «нет в RingingStage.run:30-74» —
            # можно, открыв эти строки.
            "координаты_кода": [c["meta"]["координата"] for c in decision],
        })

    return {
        "этап": stage,
        "пунктов": len(regs),
        "таблицы": tables,
        "код_этапа": [c["meta"]["координата"] for c in decision],
        "нормы_без_кода": sorted(tables_norm - tables_shared, key=order),
        "код_без_норм": sorted(tables_code - tables_norm, key=order),
        "непокрытые_условия": uncovered[:6],
    }


def graph_for(cfg) -> LinkGraph:
    """Граф связей стенда. Вынесено, чтобы узлы не собирали путь сами."""
    return LinkGraph(Path(cfg.root) / cfg.settings["memory"]["store_dir"] / "links.json")


# --- Откуда берётся этап, если слово не названо ------------------------------
#
# Прогон 14.09 показал главную дыру костяка-обхода: `stage_not_named` в 14
# кейсах из 19. Этап определялся ровно одним способом — буквальным словом в
# вопросе, — а спрашивают иначе: «почему Z-1070 кольцуется на ЛК2» (этап в коде
# линии), «используется ли калибровый блок из таблицы минимальных блоков» (этап
# в названии таблицы), «переход по калибру от большего к меньшему» (этап в
# лексике самой нормы). Во всех трёх случаях ответ уже лежит в индексах.
#
# Ни один из справочников ниже не выписан руками: документ→этап читается из
# индекса регламентов, таблица→этап — из tables.json через документ приложения,
# линия→этап — из самих файлов НСИ. Зашивать здесь «ЛЭ — это экструзия» значило
# бы снова подменять чтение стенда знанием о нём.

_HINTS_CACHE: dict[str, dict] = {}

# Колонки НСИ, в которых стоит код линии. Названия берутся из таблиц стенда, а
# не выдумываются: смотрим на заголовок и берём те, что называют оборудование.
_LINE_COLUMN_MARKERS = ("линия", "оборудован")


def _store_dir(cfg) -> Path:
    return Path(cfg.root) / cfg.settings["memory"]["store_dir"]


def _table_number_re() -> re.Pattern:
    return re.compile(r"(?:табл\w*\.?|приложени\w*)\s*№?\s*(\d{1,2})", re.IGNORECASE)


def stage_hints(cfg) -> dict:
    """Справочники «что к какому этапу относится», собранные из индексов стенда."""
    key = str(_store_dir(cfg))
    if key in _HINTS_CACHE:
        return _HINTS_CACHE[key]

    store = _store_dir(cfg)
    regs = _load(store / "regulations.json")

    # документ → этап: прямо из меты индекса регламентов
    by_document: dict[str, str] = {}
    for r in regs:
        meta = r["meta"]
        stage = meta.get("этап")
        if not stage:
            continue
        for name in (meta.get("документ"), meta.get("номер_документа")):
            if name:
                by_document[str(name).lower()] = stage

    # таблица → этап: приложение таблицы принадлежит документу, документ — этапу
    by_table: dict[str, str] = {}
    titles: dict[str, str] = {}
    try:
        with open(store / "tables.json", encoding="utf-8") as fh:
            tables = json.load(fh)
    except (OSError, ValueError):
        tables = {}
    for number, info in (tables or {}).items():
        doc = str(info.get("документ") or "").lower()
        stage = by_document.get(doc) or by_document.get(doc.split("-2")[0])
        if stage:
            by_table[str(number)] = stage
        title = (info.get("название") or "").strip().lower()
        if title:
            titles[title] = str(number)

    # пункт нормы → этап знает и таблица, на которую пункт ссылается: этим
    # покрываются таблицы, которых нет в tables.json (название не распозналось)
    for r in regs:
        stage = r["meta"].get("этап")
        for number in (r["meta"].get("таблицы_НСИ") or []):
            by_table.setdefault(str(number), stage)

    # линия → этап: коды линий читаются из самих таблиц НСИ
    by_line: dict[str, str] = {}
    try:
        from tools.nsi_lookup import catalogue          # локальный импорт: цикл
        import pandas as pd

        for number, info in catalogue(cfg).items():
            stage = by_table.get(str(number))
            if not stage:
                continue
            try:
                frame = pd.read_excel(info["path"])
            except Exception:                            # noqa: BLE001
                continue
            for column in frame.columns:
                if not any(m in str(column).lower() for m in _LINE_COLUMN_MARKERS):
                    continue
                for value in frame[column].dropna().unique():
                    for part in re.split(r"[;,/\s]+", str(value)):
                        part = part.strip()
                        # код линии — короткое обозначение с цифрой, не фраза
                        if part and len(part) <= 6 and re.search(r"\d", part):
                            by_line.setdefault(part.lower(), stage)
    except Exception:                                    # noqa: BLE001
        pass

    hints = {"документ": by_document, "таблица": by_table,
             "линия": by_line, "названия_таблиц": titles,
             "лексика": _stage_lexicon(regs)}
    _HINTS_CACHE[key] = hints
    return hints


def _stage_lexicon(regs: list[dict]) -> dict[str, str]:
    """Стемы, встречающиеся в нормах ровно одного этапа.

    Разделяющее слово — то, которое есть у одного этапа и нет у остальных.
    «Калибр» пишут все три регламента, и он не говорит ни о чём; «переход»,
    «намотка», «эксклюзивность» — признак экструзии. Считать это руками
    незачем: разница множеств по индексу даёт ровно тот же список и не устареет,
    когда регламенты поменяются.
    """
    per_stage: dict[str, set[str]] = {}
    for r in regs:
        stage = r["meta"].get("этап")
        if not stage:
            continue
        per_stage.setdefault(stage, set()).update(terms(r.get("text", "")))
    lexicon: dict[str, str] = {}
    for stage, stems in per_stage.items():
        others = set().union(*(v for k, v in per_stage.items() if k != stage)) \
            if len(per_stage) > 1 else set()
        for stem in stems - others:
            # Короткие обрубки шумят, а голые числа («2070», «120») разделяют
            # этапы случайно: год и калибр попадают в один регламент и не
            # попадают в другой просто потому, что пример был один. Номера
            # таблиц разбираются отдельным, точным правилом выше.
            if len(stem) >= 4 and not stem.isdigit():
                lexicon[stem] = stage
    return lexicon


def detect_stage(cfg, question: str, entities: dict | None = None) -> tuple[str | None, str]:
    """Этап вопроса и способ, которым он определён.

    Способ возвращается вместе с ответом нарочно: «агент решил, что это
    кольцевание, потому что ЛК2 — линия из табл. 20» проверяемо, а «агент решил»
    — нет. В трассе стоит именно эта фраза.
    """
    entities = entities or {}
    low = " ".join(str(question or "").lower().split())

    stage = normalize_stage(entities.get("stage"))
    if stage:
        return stage, "этап назван в вопросе"

    stage = stage_from_question(question)
    if stage:
        return stage, "этап назван в вопросе"

    hints = stage_hints(cfg)

    # 1. номер документа: «так ли это сделано по ТР-КОЛ-2026/03»
    for name, found in sorted(hints["документ"].items(), key=lambda x: -len(x[0])):
        if name in low:
            return found, f"вопрос ссылается на {name.upper()}"

    # 2. код линии: «почему кольцуется на ЛК2»
    candidates = [str(entities.get("line") or "")] + re.findall(r"[а-яa-z]{2}\d{1,2}", low)
    for token in candidates:
        found = hints["линия"].get(token.strip().lower())
        if found:
            return found, f"линия {token.strip().upper()} относится к этапу «{found}»"

    # 3. номер таблицы: «табл. 12»
    numbers = _table_number_re().findall(low) + [str(entities.get("nsi_table") or "")]
    for number in numbers:
        found = hints["таблица"].get(str(number).strip())
        if found:
            return found, f"табл. {number} относится к этапу «{found}»"

    # 4. название таблицы: «таблица минимальных блоков»
    query_stems = set(terms(low))

    # Сравниваем только значащие стемы: на коротких сравнение по префиксу
    # вырождается. «Норматив» начинается с «но», и предлог в вопросе делал
    # совпадение с названием любой таблицы — на кейсе B1 это увело вопрос про
    # переходы на экструзии в кольцевание.
    long_query = {q for q in query_stems if len(q) >= 4}

    def _matches(stem: str) -> bool:
        # Усечение окончаний не идеально: «минимальные» в названии даёт
        # «минимальн», а «минимальных» в вопросе остаётся как есть. Сравнение по
        # префиксу переживает эту разницу и не путает разные слова.
        return any(q.startswith(stem) or stem.startswith(q) for q in long_query)

    for title, number in hints["названия_таблиц"].items():
        title_stems = [t for t in set(terms(title)) if len(t) >= 4]
        if not title_stems:
            continue
        hit = [t for t in title_stems if _matches(t)]
        enough = len(hit) >= 2 or (len(title_stems) == 1 and hit)
        if enough and len(hit) / len(title_stems) >= 0.6:
            found = hints["таблица"].get(number)
            if found:
                return found, f"«{title}» — это табл. {number}, этап «{found}»"

    # 5. лексика норм: слово, которое встречается у одного этапа и ни у кого
    #    больше. Так «переход по калибру от большего к меньшему» становится
    #    экструзией, хотя слова «экструзия» в вопросе нет.
    votes: dict[str, int] = {}
    for stem in query_stems:
        found = hints["лексика"].get(stem)
        if found:
            votes[found] = votes.get(found, 0) + 1
    if votes:
        best, count = max(votes.items(), key=lambda x: x[1])
        # Одного слова мало. Живой прогон дал ровно эту ловушку: вопрос «где
        # находится заказ B-3007» уехал в «печать», потому что стем «находит»
        # встретился в одном пункте ТР-ПЕЧ и больше нигде. Два независимых
        # признака случайно не совпадают, а вопрос без этапа остаётся без этапа —
        # это честный ответ, а не неудача.
        rivals = sorted(votes.values(), reverse=True)
        decisive = count >= 2 and (len(rivals) == 1 or count > rivals[1])
        if decisive:
            return best, (f"лексика вопроса встречается только в нормах этапа "
                          f"«{best}» ({count} признака)")

    return None, "этап не выводится из вопроса"
