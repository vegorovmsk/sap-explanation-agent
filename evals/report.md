# Отчёт по золотому набору

Профиль моделей: `cloud`  ·  дата: 2026-09-15 13:04

## Сводка

| Метрика | Значение |
|---|---|
| кейсов | 19 |
| прогонов состоялось | 19 |
| пройдено | 13 |
| точность маршрута | 0.944 |
| полнота источников | 0.778 |
| корректность статуса | 0.842 |
| доказательность | 1.0 |
| обязательные источники названы | 0.706 |
| нет запрещённых утверждений | 1.0 |
| эскалация при расхождении | 0.667 |
| латентность P50, с | 71.0 |
| латентность P95, с | 190.8 |
| стоимость всего, $ | 0.3192 |

## Кейсы

| Кейс | Гр. | Вопрос | Намерение | Статус | Источники | Итог |
|---|---|---|---|---|---|---|
| A1 | A | Почему заказ Z-1060 не поставлен на линию ЛП | ORDER_EQUIPMENT_EXPLANATION | confirmed | nsi, plan, task | прошёл |
| A2 | A | Почему заказ Z-1010 не попал в расписание и | CONSTRAINT_EXPLANATION | confirmed | plan, task | **сбой** |
| A3 | A | Почему заказ Z-1020 вообще не попал в план? | CONSTRAINT_EXPLANATION | confirmed | code, nsi, plan, task | прошёл |
| A4 | A | Куда делся заказ Z-1030, его нет в расписани | CONSTRAINT_EXPLANATION | insufficient | code, nsi, plan, task | **сбой** |
| A5 | A | Почему заказ Z-1040 запущен, хотя блок не на | CONSTRAINT_EXPLANATION | confirmed | code, nsi, plan, task | прошёл |
| A6 | A | Где находится заказ Z-1001? | ORDER_LOOKUP | confirmed | plan, task | прошёл |
| A7 | A | Почему заказ Z-1070 кольцуется именно на ЛК2 | ORDER_EQUIPMENT_EXPLANATION | confirmed | plan, task | **сбой** |
| B1 | B | Регламент требует переходить по калибру от б | — | error | — | **сбой** |
| B2 | B | Учитывается ли диаметр кольца при отборе лин | DOC_CODE_CONSISTENCY_CHECK | conflict | code, nsi, regulations | прошёл |
| B3 | B | Используется ли калибровый блок из таблицы м | DOC_CODE_CONSISTENCY_CHECK | conflict | code, nsi, regulations | прошёл |
| C1 | C | Почему у заказа Z-1061 замечание НСИ по срок | DATA_VALIDATION | conflict | code, nsi, plan, task | прошёл |
| N1 | N | Почему заказ не туда встал? | ORDER_POSITION_EXPLANATION | clarify | — | прошёл |
| N2 | N | Почему заказ Z-9999 не поставлен на линию ЛЭ | ORDER_EQUIPMENT_EXPLANATION | insufficient | nsi, plan, task | прошёл |
| N3 | N | Где находится заказ B-3007 и есть ли по нему | ORDER_LOOKUP | confirmed | plan, task | прошёл |
| D1 | D | Объясни действующие ограничения на экструзии | GENERAL_LOGIC_EXPLANATION | confirmed | code, nsi, regulations | прошёл |
| D2 | D | Почему заказ A-3063 готов позже желаемой дат | ORDER_DELAY_EXPLANATION | confirmed | plan, task | прошёл |
| D3 | D | Почему заказ Z-1001 стоит на ЛЭ2 между Z-106 | ORDER_POSITION_EXPLANATION | confirmed | nsi, plan, regulations, task | прошёл |
| D4 | D | Соответствует ли длительность экструзии зака | NSI_PLAN_CONSISTENCY_CHECK | insufficient | nsi, plan, task | **сбой** |
| D5 | D | По заказу Z-1061 в журнале расчёта есть заме | DATA_VALIDATION | confirmed | code, nsi, plan, task | **сбой** |

## Нарушения

**A2** — Почему заказ Z-1010 не попал в расписание и стоит с годом 2070?
- не просмотрены источники: code, nsi
- не сослался на: табл. 12

**A4** — Куда делся заказ Z-1030, его нет в расписании?
- статус insufficient, допустимы ['confirmed']

**A7** — Почему заказ Z-1070 кольцуется именно на ЛК2?
- не просмотрены источники: nsi
- не сослался на: табл. 20

**B1** — Регламент требует переходить по калибру от большего к меньшему. Так ли это сделано в системе?

- намерение  вместо DOC_CODE_CONSISTENCY_CHECK
- не просмотрены источники: code, regulations
- статус error, допустимы ['conflict']
- не сослался на: ТР-ЭКС, extrusion.py
- расхождение не превращено в обращение в поддержку

**D4** — Соответствует ли длительность экструзии заказа Z-1001 нормативу выработки для линии ЛЭ2?
- статус insufficient, допустимы ['confirmed']
- не сослался на: табл. 7

**D5** — По заказу Z-1061 в журнале расчёта есть замечание НСИ — что оно означает?
- не просмотрены источники: logs
- не сослался на: demo_input_task_1.log

