# -*- coding: utf-8 -*-
"""
Граф связей: пункт регламента ↔ таблица НСИ ↔ функция в коде.

Зачем он нужен. Вопрос «где в коде реализована норма 5.1» плохо решается
эмбеддингами: текст регламента и текст Python-функции почти не пересекаются
лексически, а семантическая близость между «выбирать наименее загруженную линию»
и `min(eligible, key=lambda ln: (self.line_load[ln], ln))` невелика. Зато у них
есть общий ключ — номер нормативной таблицы. Пункт ссылается на приложение,
приложение соответствует таблице НСИ, функция эту таблицу читает. Получается
дешёвый и точный переход по ребру вместо гадания по векторам.

Граф строится один раз при сборке индексов и кладётся рядом с ними в JSON.
Именно он закрывает опциональный пункт требования «хранение связей или простой
гибридный поиск»: поиск по регламентам семантический, а связка с кодом —
структурная.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


def build(reg_chunks: list[dict], code_chunks: list[dict]) -> dict:
    """Собирает граф из уже разобранных фрагментов регламентов и кода."""
    # косвенные ссылки: пункт без своей ссылки наследует таблицы своего раздела
    section_tables: dict[tuple[str, str], set[str]] = defaultdict(set)
    for c in reg_chunks:
        m = c["meta"]
        section_tables[(m["документ"], m["раздел"])].update(m["таблицы_НСИ"])

    clauses: dict[str, dict] = {}
    by_table_clause: dict[str, list[str]] = defaultdict(list)
    for c in reg_chunks:
        m = c["meta"]
        direct = list(m["таблицы_НСИ"])
        indirect = sorted(section_tables[(m["документ"], m["раздел"])] - set(direct), key=int)
        clauses[c["id"]] = {
            "документ": m["документ"], "координата": m["координата"], "раздел": m["раздел"],
            "пункт": m["пункт"], "этап": m["этап"], "к_планированию": m["к_планированию"],
            "таблицы": direct, "таблицы_раздела": indirect,
            "текст": c["text"][:400],
        }
        for t in direct:
            by_table_clause[t].append(c["id"])

    functions: dict[str, dict] = {}
    by_table_code: dict[str, list[str]] = defaultdict(list)
    for c in code_chunks:
        m = c["meta"]
        functions[c["id"]] = {
            "файл": m["файл"], "полное_имя": m["полное_имя"], "координата": m["координата"],
            "строки": m["строки"], "таблицы": list(m["таблицы_НСИ"]),
            "к_планированию": m.get("к_планированию", True),
        }
        for t in m["таблицы_НСИ"]:
            by_table_code[t].append(c["id"])

    tables = sorted(set(by_table_clause) | set(by_table_code), key=int)
    return {
        "версия": 1,
        "пункты": clauses,
        "функции": functions,
        "таблицы": {
            t: {"пункты": sorted(by_table_clause.get(t, [])),
                "функции": sorted(by_table_code.get(t, []))}
            for t in tables
        },
    }


def save(graph: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(graph, ensure_ascii=False, indent=1), encoding="utf-8")


class LinkGraph:
    """Чтение и обход графа связей."""

    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"Граф связей не собран: {self.path}. Выполните python -m memory.build")
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------- запросы
    def resolve(self, ref: str) -> str | None:
        """Принимает ссылку на пункт в любом виде: «ТР-ЭКС п. 4.3»,
        «ТР-ЭКС-2026/01 п. 4.3» или «ТР-ЭКС 4.3»."""
        ref = str(ref).strip()
        if ref in self.data["пункты"]:
            return ref
        import re
        m = re.search(r"(\d+\.\d+)", ref)
        if not m:
            return None
        number = m.group(1)
        prefix = ref[:m.start()].replace("п.", "").strip()
        best = None
        for cid, c in self.data["пункты"].items():
            if c["пункт"] != number:
                continue
            if not prefix or prefix in cid or prefix in c["координата"]:
                return cid
            best = best or cid
        return best

    def clause(self, clause_id: str) -> dict | None:
        return self.data["пункты"].get(clause_id) or self.data["пункты"].get(
            self.resolve(clause_id) or "")

    def tables_of_clause(self, clause_id: str, include_section: bool = True) -> list[str]:
        c = self.clause(clause_id)
        if not c:
            return []
        tables = list(c["таблицы"])
        if include_section:
            tables += [t for t in c["таблицы_раздела"] if t not in tables]
        return tables

    def clauses_of_table(self, table: str) -> list[dict]:
        # ключ кладётся ПОСЛЕ распаковки: иначе он затирается полем «пункт»
        # из самой записи, где лежит номер (4.3), а не идентификатор
        ids = self.data["таблицы"].get(str(table), {}).get("пункты", [])
        return [{**self.data["пункты"][i], "пункт_id": i} for i in ids]

    @staticmethod
    def _by_specificity(items: list[dict]) -> list[dict]:
        """Чем меньше таблиц читает функция, тем она конкретнее.

        Без этой сортировки любой запрос по графу выигрывают загрузчики
        (`_load_tables`, `_build_indexes`): они трогают все шестнадцать таблиц
        и потому связаны с каждым пунктом, ничего при этом не объясняя.
        """
        return sorted(items, key=lambda f: (len(f.get("таблицы", [])),
                                            f.get("файл", ""), f.get("полное_имя", "")))

    def functions_of_table(self, table: str, planning_only: bool = True) -> list[dict]:
        ids = self.data["таблицы"].get(str(table), {}).get("функции", [])
        out = [{"фрагмент": i, **self.data["функции"][i]} for i in ids]
        if planning_only:
            out = [f for f in out if f["к_планированию"]]
        return self._by_specificity(out)

    def code_for_clause(self, clause_id: str, planning_only: bool = True) -> list[dict]:
        """Функции, читающие те же таблицы, на которые ссылается пункт."""
        seen, out = set(), []
        for table in self.tables_of_clause(clause_id):
            for fn in self.functions_of_table(table, planning_only):
                if fn["фрагмент"] in seen:
                    continue
                seen.add(fn["фрагмент"])
                out.append({**fn, "через_таблицу": table})
        return self._by_specificity(out)

    def clauses_for_code(self, chunk_id: str, planning_only: bool = True) -> list[dict]:
        """Пункты регламента, говорящие о таблицах, которые читает эта функция."""
        fn = self.data["функции"].get(chunk_id)
        if not fn:
            return []
        seen, out = set(), []
        for table in fn["таблицы"]:
            for cl in self.clauses_of_table(table):
                if cl["пункт_id"] in seen or (planning_only and not cl["к_планированию"]):
                    continue
                seen.add(cl["пункт_id"])
                out.append({**cl, "через_таблицу": table})
        return out

    def stats(self) -> dict:
        return {
            "пунктов": len(self.data["пункты"]),
            "функций": len(self.data["функции"]),
            "таблиц_в_графе": len(self.data["таблицы"]),
            "связанных_таблиц": sorted(
                (t for t, v in self.data["таблицы"].items() if v["пункты"] and v["функции"]),
                key=int),
        }
