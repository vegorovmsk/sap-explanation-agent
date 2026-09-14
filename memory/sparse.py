# -*- coding: utf-8 -*-
"""
Разреженные векторы BM25 — лексическая половина гибридного поиска.

Зачем они нужны рядом с плотными эмбеддингами. Нормативный текст держится на
точных обозначениях: «Флексо-4», «ЛП2», «приложение 5», «табл. 27», «Дк». Плотная
модель такие токены размывает — для неё «Флексо-2» и «Флексо-4» почти одно и то
же, а для планирования это принципиально разные вещи. Разреженный вектор,
наоборот, точное совпадение термина видит и не видит синонимов.

Qdrant умеет держать оба вектора в одной точке и объединять выдачи по RRF
(reciprocal rank fusion): каждый список ранжируется отдельно, а итоговый вес
складывается из обратных рангов. Это и есть настоящий гибридный поиск, а не
переключатель между двумя режимами.

Веса документа считаются по BM25 и хранятся в самом векторе; запрос несёт
единичные веса, поэтому скалярное произведение даёт привычную оценку BM25.
Статистика корпуса (IDF и средняя длина) кладётся рядом с индексом: без неё
запрос нельзя закодировать так же, как документы.
"""
from __future__ import annotations

import json
import math
import re
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

TOKEN_RE = re.compile(r"[a-zA-Zа-яёА-ЯЁ0-9]+(?:[-\.][a-zA-Zа-яёА-ЯЁ0-9]+)*", re.UNICODE)
INDEX_SPACE = 2 ** 20
CYRILLIC = re.compile(r"[а-яё]")

# Упрощённое усечение окончаний. Полноценный стеммер здесь не нужен, а вот без
# какого-либо нормирования BM25 на русском почти бесполезен: «приложение»,
# «приложению» и «приложения» превращаются в три разных термина, и запрос
# перестаёт находить пункт, который прямо о нём говорит. Перебор усечения
# («линия» и «линии» → «лин») для поиска безвреден: слипаются формы одного слова.
SUFFIXES = sorted(
    ["иями", "ями", "ами", "ией", "иям", "ыми", "ими", "ого", "его", "ому", "ему",
     "ешь", "ете", "ившись", "вшись", "ется", "ются",
     "ая", "яя", "ые", "ие", "ый", "ий", "ой", "ей", "ом", "ем", "ах", "ях",
     "ов", "ев", "ью", "ия", "ии", "ую", "юю", "ет", "ут", "ют", "ат", "ят",
     "ла", "ло", "ли", "на", "но", "ны", "ть", "ти", "ся", "сь",
     "а", "я", "ы", "и", "е", "о", "у", "ю", "ь", "й"],
    key=len, reverse=True)
MIN_STEM = 3


def stem(token: str, passes: int = 2) -> str:
    """Отсекает окончания, оставляя не меньше трёх букв.

    Проходов два: у русского слова окончание бывает составным, и за один проход
    «приложению» усекается только до «приложени», а «приложение» — сразу до
    «приложен». Два прохода приводят обе формы к одной основе.
    """
    if not CYRILLIC.search(token):
        return token                      # латиница, числа и обозначения не трогаем
    for _ in range(passes):
        for suffix in SUFFIXES:
            if token.endswith(suffix) and len(token) - len(suffix) >= MIN_STEM:
                token = token[: -len(suffix)]
                break
        else:
            break
    return token


def tokenize(text: str) -> list[str]:
    """Слова, обозначения и номера: «флексо-4», «лп2», «4.3», «табл» — всё это термины."""
    out: list[str] = []
    for token in TOKEN_RE.findall(str(text).lower()):
        out.append(token)
        # составные обозначения дополнительно бьём на части: «флексо-4» → «флексо», «4»
        if "-" in token or "." in token:
            out.extend(p for p in re.split(r"[-\.]", token) if p)
    return out


def terms(text: str) -> list[str]:
    """Термины для BM25: те же токены, но с усечёнными окончаниями."""
    return [stem(t) for t in tokenize(text)]


def term_index(term: str) -> int:
    return zlib.crc32(term.encode("utf-8")) % INDEX_SPACE


@dataclass
class BM25Encoder:
    """BM25 поверх хеширующего пространства индексов."""

    k1: float = 1.5
    b: float = 0.75
    idf: dict[str, float] = field(default_factory=dict)   # ключ — строковый индекс термина
    avgdl: float = 1.0
    documents: int = 0

    # ------------------------------------------------------------------ обучение
    def fit(self, corpus: list[str]) -> "BM25Encoder":
        df: Counter[int] = Counter()
        total = 0
        for text in corpus:
            tokens = terms(text)
            total += len(tokens)
            df.update({term_index(t) for t in tokens})
        self.documents = max(1, len(corpus))
        self.avgdl = total / self.documents if self.documents else 1.0
        # сглаженный IDF: редкий термин весит больше, встречающийся везде — почти ничего
        self.idf = {
            str(idx): math.log(1 + (self.documents - n + 0.5) / (n + 0.5))
            for idx, n in df.items()
        }
        return self

    # ------------------------------------------------------------------ кодирование
    def encode_document(self, text: str) -> tuple[list[int], list[float]]:
        tokens = terms(text)
        if not tokens:
            return [], []
        dl = len(tokens)
        counts = Counter(term_index(t) for t in tokens)
        indices, values = [], []
        for idx, tf in counts.items():
            idf = self.idf.get(str(idx))
            if not idf:
                continue
            weight = idf * (tf * (self.k1 + 1)) / (
                tf + self.k1 * (1 - self.b + self.b * dl / max(self.avgdl, 1e-6)))
            if weight > 0:
                indices.append(int(idx))
                values.append(float(weight))
        return indices, values

    def encode_query(self, text: str) -> tuple[list[int], list[float]]:
        """У запроса единичные веса: вся статистика уже зашита в вектор документа."""
        indices = sorted({term_index(t) for t in terms(text) if str(term_index(t)) in self.idf})
        return indices, [1.0] * len(indices)

    # ------------------------------------------------------------------ хранение
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"k1": self.k1, "b": self.b, "avgdl": self.avgdl,
             "documents": self.documents, "idf": self.idf},
            ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "BM25Encoder | None":
        if not Path(path).exists():
            return None
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(k1=data["k1"], b=data["b"], idf=data["idf"],
                   avgdl=data["avgdl"], documents=data["documents"])
