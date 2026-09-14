# Отчёт по золотому набору

Профиль моделей: `cloud`  ·  дата: 2026-09-12 13:37

## Сводка

| Метрика | Значение |
|---|---|
| кейсов | 14 |
| пройдено | 2 |
| точность маршрута | 0.385 |
| полнота источников | 0.385 |
| корректность статуса | 0.357 |
| доказательность | 0.929 |
| обязательные источники названы | 0.5 |
| нет запрещённых утверждений | 1.0 |
| эскалация при расхождении | 0.0 |
| латентность P50, с | 77.5 |
| латентность P95, с | 173.2 |
| стоимость всего, $ | 0.2831 |

## Кейсы

| Кейс | Гр. | Вопрос | Намерение | Статус | Источники | Итог |
|---|---|---|---|---|---|---|
| A1 | A | Почему заказ Z-1060 не поставлен на линию ЛП | ORDER_EQUIPMENT_EXPLANATION | insufficient | nsi, plan, task | **сбой** |
| A2 | A | Почему заказ Z-1010 не попал в расписание и | ORDER_DELAY_EXPLANATION | insufficient | code, nsi, plan, task | **сбой** |
| A3 | A | Почему заказ Z-1020 вообще не попал в план? | ORDER_DELAY_EXPLANATION | insufficient | plan, task | **сбой** |
| A4 | A | Куда делся заказ Z-1030, его нет в расписани | ORDER_LOOKUP | insufficient | plan | **сбой** |
| A5 | A | Почему заказ Z-1040 запущен, хотя блок не на | ORDER_EQUIPMENT_EXPLANATION | insufficient | nsi, plan, task | **сбой** |
| A6 | A | Где находится заказ Z-1001? | ORDER_LOOKUP | clarify | — | **сбой** |
| A7 | A | Почему заказ Z-1070 кольцуется именно на ЛК2 | ORDER_EQUIPMENT_EXPLANATION | insufficient | nsi, plan, task | **сбой** |
| B1 | B | Регламент требует переходить по калибру от б | NSI_PLAN_CONSISTENCY_CHECK | insufficient | — | **сбой** |
| B2 | B | Учитывается ли диаметр кольца при отборе лин | ORDER_EQUIPMENT_EXPLANATION | insufficient | nsi, plan, task | **сбой** |
| B3 | B | Используется ли калибровый блок из таблицы м | DATA_VALIDATION | insufficient | nsi, task | **сбой** |
| C1 | C | Почему у заказа Z-1061 замечание НСИ по срок | CONSTRAINT_EXPLANATION | conflict | code, nsi, plan, regulations | **сбой** |
| N1 | N | Почему заказ не туда встал? | ORDER_POSITION_EXPLANATION | clarify | — | прошёл |
| N2 | N | Почему заказ Z-9999 не поставлен на линию ЛЭ | ORDER_EQUIPMENT_EXPLANATION | insufficient | nsi, plan, task | прошёл |
| N3 | N | Где находится заказ B-3007 и есть ли по нему | ORDER_LOOKUP | clarify | — | **сбой** |

## Нарушения

**A1** — Почему заказ Z-1060 не поставлен на линию ЛП2?
- статус insufficient, допустимы ['confirmed']
- ссылки вне доказательной базы: ТР-ПЕЧ-2026/02 п. 3.1

**A2** — Почему заказ Z-1010 не попал в расписание и стоит с годом 2070?
- намерение ORDER_DELAY_EXPLANATION вместо CONSTRAINT_EXPLANATION
- статус insufficient, допустимы ['confirmed']

**A3** — Почему заказ Z-1020 вообще не попал в план?
- намерение ORDER_DELAY_EXPLANATION вместо DATA_VALIDATION
- не просмотрены источники: code, nsi
- статус insufficient, допустимы ['confirmed', 'conflict']
- не сослался на: табл. 8

**A4** — Куда делся заказ Z-1030, его нет в расписании?
- намерение ORDER_LOOKUP вместо CONSTRAINT_EXPLANATION
- не просмотрены источники: code
- статус insufficient, допустимы ['confirmed']

**A5** — Почему заказ Z-1040 запущен, хотя блок не набран?
- намерение ORDER_EQUIPMENT_EXPLANATION вместо CONSTRAINT_EXPLANATION
- не просмотрены источники: code
- статус insufficient, допустимы ['confirmed']

**A6** — Где находится заказ Z-1001?
- не просмотрены источники: plan
- статус clarify, допустимы ['confirmed']
- не сослался на: Все_ПП

**A7** — Почему заказ Z-1070 кольцуется именно на ЛК2?
- статус insufficient, допустимы ['confirmed']

**B1** — Регламент требует переходить по калибру от большего к меньшему. Так ли это сделано в системе?

- намерение NSI_PLAN_CONSISTENCY_CHECK вместо DOC_CODE_CONSISTENCY_CHECK
- не просмотрены источники: code, regulations
- статус insufficient, допустимы ['conflict']
- не сослался на: ТР-ЭКС, extrusion.py
- расхождение не превращено в обращение в поддержку

**B2** — Учитывается ли диаметр кольца при отборе линии кольцевания?
- намерение ORDER_EQUIPMENT_EXPLANATION вместо DOC_CODE_CONSISTENCY_CHECK
- не просмотрены источники: code, regulations
- не сослался на: ТР-КОЛ, ringing.py
- расхождение не превращено в обращение в поддержку

**B3** — Используется ли калибровый блок из таблицы минимальных блоков?
- намерение DATA_VALIDATION вместо DOC_CODE_CONSISTENCY_CHECK
- не просмотрены источники: code
- расхождение не превращено в обращение в поддержку

**C1** — Почему у заказа Z-1061 замечание НСИ по срокам хранения до печати?
- намерение CONSTRAINT_EXPLANATION вместо DATA_VALIDATION
- не сослался на: табл. 29

**N3** — Где находится заказ B-3007 и есть ли по нему просрочка?
- не просмотрены источники: plan
- статус clarify, допустимы ['confirmed']
- не сослался на: Все_ПП

