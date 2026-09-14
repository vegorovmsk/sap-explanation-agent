# -*- coding: utf-8 -*-
"""Промпт и JSON-схема первого узла: классификация намерения и извлечение сущностей."""
from __future__ import annotations

SYSTEM = """Ты — разборщик запросов в системе поддержки пользователей САП
(система автоматического планирования производства полимерных оболочек).

Твоя единственная задача — перевести свободный вопрос пользователя в строгий JSON:
класс вопроса и сущности. Ты НЕ отвечаешь на вопрос и НЕ строишь объяснений.

Домен:
- этапы производства: экструзия, печать, кольцевание;
- линии экструзии ЛЭ1–ЛЭ4, печати ЛП1–ЛП3, кольцевания ЛК1–ЛК2;
- номера заказов выглядят как Z-1060, A-3027, B-3084;
- нормативно-справочная информация лежит в пронумерованных таблицах (табл. 1, 2, 8, 9, 12, 27 …);
{stand_facts}

Классы вопросов:
{intents}

Правила:
1. Извлекай только то, что явно есть в вопросе. Ничего не додумывай и не подставляй
   значения по умолчанию: чего нет — null.
2. Номера заказов и обозначения линий переписывай ровно так, как в вопросе.
3. Уточняющий вопрос — редкость, а не вежливость. Ставь
   ambiguity.is_ambiguous = true только если объект вопроса вообще не назван:
   нет ни номера заказа, ни линии, ни номера таблицы — как в «почему заказ не
   там встал». Если номер заказа в вопросе есть, вопрос НЕ неоднозначен, даже
   когда не указаны этап, линия или дата: чего не хватает, агент дочитает из
   расписания сам. Не существующий в системе номер — тоже не повод уточнять:
   это ответ «такого заказа нет», и его агент даст сам.
4. reason_summary — одна короткая фраза о том, почему выбран этот класс.
   Это запись в журнал, а не рассуждение: без разбора вариантов.
5. Отвечай только JSON-объектом по схеме, без markdown-ограды и пояснений."""

USER = """Файл задания: {task_file}

Вопрос пользователя:
{question}"""

SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "entities", "ambiguity", "reason_summary"],
    "properties": {
        "intent": {
            "type": "string",
            "description": "Класс вопроса из списка",
        },
        "entities": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "order_number", "left_neighbor", "right_neighbor", "stage", "line",
                "kind", "sort", "caliber", "color", "print_type", "nsi_table", "due_date",
            ],
            "properties": {
                "order_number": {"type": ["string", "null"], "description": "Заказ, о котором спрашивают"},
                "left_neighbor": {"type": ["string", "null"], "description": "Левый соседний заказ в очереди"},
                "right_neighbor": {"type": ["string", "null"], "description": "Правый соседний заказ в очереди"},
                "stage": {"type": ["string", "null"], "description": "экструзия, печать или кольцевание"},
                "line": {"type": ["string", "null"], "description": "Линия, например ЛП2"},
                "kind": {"type": ["string", "null"], "description": "Вид оболочки"},
                "sort": {"type": ["string", "null"], "description": "Тип оболочки"},
                "caliber": {"type": ["integer", "null"], "description": "Калибр"},
                "color": {"type": ["string", "null"], "description": "Цвет оболочки"},
                "print_type": {"type": ["string", "null"], "description": "Вид печати, например Флексо-4"},
                "nsi_table": {"type": ["string", "null"], "description": "Номер таблицы НСИ, если назван"},
                "due_date": {"type": ["string", "null"], "description": "Дата в формате ГГГГ-ММ-ДД, если названа"},
            },
        },
        "ambiguity": {
            "type": "object",
            "additionalProperties": False,
            "required": ["is_ambiguous", "question"],
            "properties": {
                "is_ambiguous": {"type": "boolean"},
                "question": {"type": ["string", "null"],
                             "description": "Один уточняющий вопрос пользователю или null"},
            },
        },
        "reason_summary": {"type": "string", "description": "Одна фраза: почему выбран этот класс"},
    },
}


def classifiable(intents: dict) -> dict:
    """Классы, которые можно выбрать по формулировке вопроса.

    Часть маршрутов — внутренние ветки графа, а не классы вопросов. Предлагать
    их модели вредно: у SUPPORT_TICKET_GENERATION источник «evidence» не даёт ни
    одного инструмента, и выбор этого класса означал бы прогон без сбора фактов.
    """
    return {k: v for k, v in intents.items() if not v.get("не_для_классификации")}


def stand_facts(stand) -> str:
    """Служебные обозначения стенда — из его конфига, а не из памяти модели.

    Раньше здесь стояли «2070, 2099, 2100» прямым текстом. На другом стенде с
    другими служебными годами агент классифицировал бы вопрос неверно и ничем
    бы этого не выдал: промпт выглядит одинаково убедительно с любыми числами.
    """
    lines = []
    pseudo = list(getattr(stand, "pseudo_lines", None) or [])
    if pseudo:
        names = ", ".join(f"«{p}»" for p in pseudo)
        lines.append(f"- служебные линии {names} означают, что заказ не попал "
                     f"в расписание;")
    years = getattr(stand, "service_years", None) or {}
    if years:
        parts = ", ".join(f"{year} ({key})" for year, key in sorted(years.items()))
        lines.append(f"- служебные годы готовности из конфига стенда: {parts}. "
                     f"Вопрос про такой год — это вопрос о сработавшем ограничении, "
                     f"а не о сроке. ЧТО означает конкретный год, здесь не сказано "
                     f"намеренно: это написано в конфиге стенда и читается "
                     f"инструментом, а не берётся из памяти.")
    return "\n".join(lines)


def build_messages(question: str, task_file: str, intents: dict, stand=None) -> list[dict]:
    """Собирает сообщения запроса. Список классов подставляется из config/routing.yaml."""
    lines = [f"- {name}: {rule.get('описание', '')}"
             for name, rule in classifiable(intents).items()]
    return [
        {"role": "system", "content": SYSTEM.format(intents="\n".join(lines),
                                                    stand_facts=stand_facts(stand))},
        {"role": "user", "content": USER.format(question=question, task_file=task_file)},
    ]


def schema_for(intents: dict) -> dict:
    """Схема с подставленным enum допустимых классов."""
    schema = {**SCHEMA, "properties": {**SCHEMA["properties"]}}
    schema["properties"]["intent"] = {**SCHEMA["properties"]["intent"],
                                      "enum": list(classifiable(intents))}
    return schema
