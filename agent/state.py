# -*- coding: utf-8 -*-
"""
Состояние агента.

Одна структура на весь прогон: её целиком видно в трассе, по ней восстанавливается
любой ответ. Заполняется узлами графа по ходу цикла Reason → Act → Observe.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Status = Literal["in_progress", "confirmed", "conflict", "insufficient", "clarify", "error"]


@dataclass
class Evidence:
    """Факт с обязательной привязкой к источнику.

    Без координаты факт не попадает в ответ: это главное правило проекта.
    Координата — не имя файла, а место: лист и строка, номер пункта, функция.
    """
    claim: str                  # что установлено
    value: Any = None           # значение, если это число или строка из источника
    source: str = ""            # plan | task | nsi | regulations | code | logs
    locator: str = ""           # «Все_ПП, строка 42» / «ТР-ЭКС п. 4.3» / «extrusion.py:_place_group»
    tool: str = ""              # каким инструментом получено
    trusted: bool = True        # False для содержимого документов и кода (untrusted content)


@dataclass
class Conflict:
    """Расхождение между источниками — вход в ветку обращения в поддержку."""
    subject: str
    expected: str
    expected_source: str
    actual: str
    actual_source: str
    severity: Literal["low", "medium", "high"] = "medium"


@dataclass
class AgentState:
    # --- вход
    request_id: str = ""
    question: str = ""
    task_file: str = ""

    # --- разбор запроса
    intent: str = ""
    entities: dict = field(default_factory=dict)
    ambiguity: dict = field(default_factory=dict)

    # --- маршрут
    role: str = ""
    companion_role: str | None = None
    required_sources: list[str] = field(default_factory=list)
    optional_sources: list[str] = field(default_factory=list)
    planned_tools: list[str] = field(default_factory=list)

    # --- доказательная база
    evidence: list[Evidence] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    # История пробелов за все итерации. Нужна, чтобы поймать самый дорогой отказ:
    # модель на каждом круге заявляет один и тот же пробел, ничего для его
    # закрытия не вызывает, и прогон упирается в лимит итераций. Живой прогон
    # по заказу Z-1010 дал ровно это — четыре круга с одной и той же фразой.
    gap_history: list[str] = field(default_factory=list)
    failed_calls: list[dict] = field(default_factory=list)   # неудачные вызовы инструментов
    conflicts: list[Conflict] = field(default_factory=list)
    sources_seen: list[str] = field(default_factory=list)

    # --- счётчики лимитов и решения цикла
    iterations: int = 0
    tool_calls: int = 0
    retrieval_queries: int = 0
    evidence_enough: bool = False
    limited_by: str | None = None
    # Сколько фактов было на прошлой оценке полноты. По этому числу узел observe
    # механически видит, дал ли круг хоть что-нибудь новое: круг без новых фактов
    # и с тем же пробелом — это петля, и рвать её должен код, а не лимит.
    last_evidence_count: int = 0
    # Сколько неудачных вызовов было на прошлой оценке полноты. Промах с
    # подсказкой («ЛП2 ищите в табл. 18, а не в табл. 27») фактом не является,
    # но следующий круг делает осмысленным — значит, это прогресс.
    last_failed_count: int = 0

    # --- итог
    status: Status = "in_progress"
    confidence: float = 0.0
    confidence_label: str = "низкая"
    answer_summary: str = ""
    answer_explanation: str = ""
    cited_locators: list[str] = field(default_factory=list)
    answer: str = ""
    ticket: dict | None = None
    ticket_text: str = ""
    verification: dict | None = None
    # Сколько раз ответ переписывался по замечаниям проверки. Ограничивает
    # обратное ребро verify → answer одним заходом.
    answer_revisions: int = 0
    # Карта источников этапа: нормы, таблицы НСИ, код и кандидаты в расхождения.
    # Собирается кодом по индексам, чтобы модель не угадывала структуру.
    stage_map: dict = field(default_factory=dict)
    # Сколько утверждений пришлось вычеркнуть из ответа механически: модель
    # сослалась на непрочитанный источник и не отступилась после просьбы. Это
    # нарушение того же рода, что ссылка вне базы, и вердикт оно связывает.
    struck_claims: int = 0
    security_events: list[str] = field(default_factory=list)
    error: str | None = None

    def add_evidence(self, item: Evidence) -> None:
        self.evidence.append(item)
        if item.source and item.source not in self.sources_seen:
            self.sources_seen.append(item.source)

    def missing_sources(self) -> list[str]:
        return [s for s in self.required_sources if s not in self.sources_seen]

    def locators(self) -> set[str]:
        """Все координаты, которые агент имеет право процитировать."""
        return {e.locator for e in self.evidence if e.locator}

    def to_dict(self) -> dict:
        return asdict(self)
