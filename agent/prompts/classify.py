# -*- coding: utf-8 -*-
"""Промпт и JSON-схема первого узла: классификация намерения и извлечение сущностей."""
from __future__ import annotations

SYSTEM = """Ты — разборщик запросов в системе поддержки пользователей САП
(система автоматического планирования производства полимерных оболочек).

Твоя единственная задача — перевести свободный вопрос пользователя в строгий JSON:
класс вопроса и сущности. Ты НЕ отвечаешь на вопрос и НЕ строишь объяснений.

Домен:
{stand_facts}

Классы вопросов:
{intents}

Правила:
1. Извлекай только то, что явно есть в вопросе. Ничего не додумывай и не подставляй
   значения по умолчанию: чего нет — null.
2. Номера заказов и обозначения линий переписывай ровно так, как в вопросе.
3. scope — о чём вопрос по своей природе, а не по формулировке:
   · "order"  — о судьбе КОНКРЕТНОЙ производственной партии: почему этот заказ
     там оказался, почему опаздывает, где он, что с ним не так. Такой вопрос без
     номера заказа бессмыслен: ответ зависит от того, о каком заказе речь.
   · "system" — об устройстве системы: как она выбирает линии, какие есть
     ограничения и этапы, соответствует ли реализация регламенту. Ответ не
     зависит ни от какого отдельного заказа, и требовать номер здесь — значит не
     ответить на исправно заданный вопрос.
   Различие не в том, назван ли номер, а в том, нужен ли он. «Почему заказ
   опаздывает?» — order без номера: спрашивать придётся. «Почему заказы вообще
   опаздывают?» — system: это вопрос о логике сроков.
4. Уточняющий вопрос — редкость, а не вежливость. Ставь
   ambiguity.is_ambiguous = true только если объект вопроса вообще не назван:
   нет ни номера заказа, ни линии, ни номера таблицы — как в «почему заказ не
   там встал». Если номер заказа в вопросе есть, вопрос НЕ неоднозначен, даже
   когда не указаны этап, линия или дата: чего не хватает, агент дочитает из
   расписания сам. Не существующий в системе номер — тоже не повод уточнять:
   это ответ «такого заказа нет», и его агент даст сам.
5. reason_summary — одна короткая фраза о том, почему выбран этот класс.
   Это запись в журнал, а не рассуждение: без разбора вариантов.
6. Отвечай только JSON-объектом по схеме, без markdown-ограды и пояснений."""

USER = """Файл задания: {task_file}

Вопрос пользователя:
{question}"""

SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "entities", "ambiguity", "scope", "reason_summary"],
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
                "line": {"type": ["string", "null"], "description": "Код линии ровно так, как он назван в вопросе"},
                "kind": {"type": ["string", "null"], "description": "Вид оболочки"},
                "sort": {"type": ["string", "null"], "description": "Тип оболочки"},
                "caliber": {"type": ["integer", "null"], "description": "Калибр"},
                "color": {"type": ["string", "null"], "description": "Цвет оболочки"},
                "print_type": {"type": ["string", "null"], "description": "Вид печати ровно так, как он назван в вопросе"},
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
        "scope": {
            "type": "string", "enum": ["order", "system"],
            "description": "order — вопрос о судьбе конкретной партии, ответ "
                           "зависит от номера заказа; system — вопрос об "
                           "устройстве системы, номер заказа не нужен",
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


def stand_facts(stand, cfg=None) -> str:
    """Домен стенда целиком читается у стенда, а не пишется в промпте.

    Раньше здесь был рукописный блок: «линии экструзии ЛЭ1–ЛЭ4, печати ЛП1–ЛП3»,
    «номера заказов выглядят как Z-1060, A-3027, B-3084», «таблицы 1, 2, 8, 9,
    12, 27». Всё это — настоящие значения наблюдаемой системы, и держать их в
    промпте вредно вдвойне. На другом стенде они молча неверны: промпт выглядит
    одинаково убедительно с любыми числами. А на этом они подсказывают модели
    ответ до чтения источников — живой прогон уже показывал, как пример
    координаты из промпта возвращается в ответ как настоящая ссылка.

    Примеры номеров заказов убраны совсем. Правило «переписывай номер ровно так,
    как в вопросе» не нуждается в образце, а образец — это готовый номер, который
    модель может подставить, когда в вопросе его нет.
    """
    lines = []
    if cfg is not None:
        try:
            from memory.traversal import stage_hints

            hints = stage_hints(cfg)
            stages = sorted({v for v in hints["документ"].values()})
            if stages:
                lines.append("- этапы производства: " + ", ".join(stages) + ";")
            by_stage: dict[str, list[str]] = {}
            for code, stage in sorted(hints["линия"].items()):
                by_stage.setdefault(stage, []).append(code.upper())
            if by_stage:
                shown = "; ".join(f"{st} — {', '.join(codes)}"
                                  for st, codes in sorted(by_stage.items()))
                lines.append(f"- линии по этапам: {shown};")
            tables = sorted(hints["таблица"], key=lambda x: int(x) if x.isdigit() else 999)
            if tables:
                lines.append("- нормативно-справочная информация лежит в "
                             "пронумерованных таблицах: " + ", ".join(tables) + ";")
        except Exception:                                    # noqa: BLE001
            pass
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


def build_messages(question: str, task_file: str, intents: dict,
                   stand=None, cfg=None) -> list[dict]:
    """Собирает сообщения запроса. Список классов подставляется из config/routing.yaml."""
    lines = [f"- {name}: {rule.get('описание', '')}"
             for name, rule in classifiable(intents).items()]
    return [
        {"role": "system", "content": SYSTEM.format(
            intents="\n".join(lines), stand_facts=stand_facts(stand, cfg))},
        {"role": "user", "content": USER.format(question=question, task_file=task_file)},
    ]


def schema_for(intents: dict) -> dict:
    """Схема с подставленным enum допустимых классов."""
    schema = {**SCHEMA, "properties": {**SCHEMA["properties"]}}
    schema["properties"]["intent"] = {**SCHEMA["properties"]["intent"],
                                      "enum": list(classifiable(intents))}
    return schema
