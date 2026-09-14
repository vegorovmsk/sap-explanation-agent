# -*- coding: utf-8 -*-
"""
Разбор кода стенда на фрагменты.

Чанк равен **функции или методу**, границы берутся из AST, а не из окна по
строкам: половина условия, оторванная от заголовка функции, доказательством быть
не может. Координата фрагмента — файл, полное имя и диапазон строк
(`extrusion.py:_place_group:150-171`), её агент цитирует в ответе.

Попутно из тела функции вытягиваются обращения к нормативным таблицам:
`self.tables["transition_waste"]`, `data.tables[...]`, а также упоминания
«табл. 27» в комментариях и строках. Логическое имя таблицы превращается в её
номер по секции `input_files` конфига стенда — так фрагмент кода получает те же
номера таблиц, что и пункт регламента, и они связываются друг с другом.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

TABLE_KEY_RE = re.compile(r"""tables\[\s*["']([A-Za-z_]+)["']\s*\]""")
TABLE_NUM_RE = re.compile(r"табл\w*\.?\s*(\d+)", re.IGNORECASE)
FILE_NUM_RE = re.compile(r"(\d+)_")

# Индексы, которые InputData строит поверх таблиц: имя атрибута → логическая таблица
# Модуль генерации синтетических данных к логике планирования не относится:
# по умолчанию он в выдачу поиска не идёт, иначе вопрос про сортировку калибров
# приводит агента в генератор тестовых заданий.
NOT_PLANNING = {"synth_data.py"}

INDEX_ALIASES = {
    "perf_index": "extrusion_outcome",
    "color_index": "color_matrix",
    "minblock_index": "minimal_blocks",
    "assort_index": "assortment",
    "waste_exceptions": "waste_exceptions",
    "print_lines_by_type": "print_equipment_sets",
    "print_perf": "print_outcome",
    "print_types": "print_types_sets",
    "store_index": "print_days_before",
}


def table_numbers_by_key(cfg) -> dict[str, str]:
    """Логическое имя таблицы в конфиге стенда → её номер (по имени файла)."""
    import yaml

    with open(cfg.stand.config_file, "r", encoding="utf-8") as f:
        stand_cfg = yaml.safe_load(f)
    out: dict[str, str] = {}
    for key, rel in (stand_cfg.get("input_files") or {}).items():
        m = FILE_NUM_RE.search(Path(str(rel)).name)
        if m:
            out[key] = m.group(1)
    return out


def _tables_used(source: str, key_to_number: dict[str, str]) -> list[str]:
    numbers = {key_to_number[k] for k in TABLE_KEY_RE.findall(source) if k in key_to_number}
    for alias, key in INDEX_ALIASES.items():
        if re.search(rf"\b{alias}\b", source) and key in key_to_number:
            numbers.add(key_to_number[key])
    numbers.update(TABLE_NUM_RE.findall(source))
    return sorted(numbers, key=int)


def _walk(tree: ast.AST):
    """Функции и методы верхнего уровня и внутри классов, с именем класса."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield None, node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield node.name, item


def parse_module(path: Path, cfg, key_to_number: dict[str, str]) -> list[dict]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    rel = path.relative_to(cfg.stand.root).as_posix()
    chunks: list[dict] = []
    for cls, node in _walk(tree):
        start, end = node.lineno, getattr(node, "end_lineno", node.lineno)
        body = "\n".join(lines[start - 1:end])
        qualname = f"{cls}.{node.name}" if cls else node.name
        chunks.append({
            "id": f"{rel}:{qualname}",
            "text": body,
            "meta": {
                "источник": "code",
                "файл": rel,
                "класс": cls or "",
                "функция": node.name,
                "полное_имя": qualname,
                "строки": f"{start}-{end}",
                "документация": (ast.get_docstring(node) or "").strip(),
                "таблицы_НСИ": _tables_used(body, key_to_number),
                "координата": f"{rel}:{qualname}:{start}-{end}",
                "к_планированию": path.name not in NOT_PLANNING,
            },
        })

    module_doc = ast.get_docstring(tree)
    if module_doc:
        chunks.append({
            "id": f"{rel}:__module__",
            "text": module_doc.strip(),
            "meta": {
                "источник": "code", "файл": rel, "класс": "", "функция": "__module__",
                "полное_имя": "описание модуля", "строки": "1",
                "документация": module_doc.strip(),
                "таблицы_НСИ": _tables_used(source, key_to_number),
                "координата": f"{rel} (описание модуля)",
                "к_планированию": path.name not in NOT_PLANNING,
            },
        })
    return chunks


def embedding_text(chunk: dict) -> str:
    """В эмбеддер уходят имя, документация и тело — тело обрезается: длинные
    функции иначе перевешивают короткие просто объёмом."""
    m = chunk["meta"]
    tables = (" Использует таблицы НСИ: " + ", ".join(m["таблицы_НСИ"]) + "."
              if m["таблицы_НСИ"] else "")
    doc = f" {m['документация']}" if m["документация"] else ""
    return f"{m['файл']} {m['полное_имя']}.{doc}{tables}\n{chunk['text'][:1500]}"


def collect(cfg) -> list[dict]:
    key_to_number = table_numbers_by_key(cfg)
    chunks: list[dict] = []
    seen: set[Path] = set()
    for directory in [*cfg.stand.code_dirs, cfg.stand.root]:
        for path in sorted(Path(directory).glob("*.py")):
            if "__pycache__" in path.parts or path in seen:
                continue
            seen.add(path)
            chunks.extend(parse_module(path, cfg, key_to_number))
    return chunks
