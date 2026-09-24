# Мок учётной системы

Изображает учётную систему для MVP: отдаёт факты об оборудовании, партнёрах, договорах, центрах,
исполнителях и запчастях и принимает черновики документов. Данные — справочники из
`../data/reference` и `../data/synthetic`. Платформа обращается к моку только через адаптер
(`../backend/adapters`, ADR-0006). Рабочий backend использует SapODataAdapter и custom SAP-like OData V4 façade ZAI_SERVICE. Совместимость с released SAP standard API не заявляется.

FastAPI и PostgreSQL. База `mock_erp` — отдельная, на том же сервере, что и база платформы:
мок изображает внешнюю систему.

## Рабочий OData-контракт

Базовый URL: `/sap/opu/odata4/sap/zai_service/0001/`, конфигурация backend — `ERP_BASE_URL`.
Это ограниченный custom service над той же PostgreSQL, с теми же ACL, аудитом и проверками записи.
Данные и старые операции не заменены статическими ответами. Настоящий SAP не вызывается.

`GET /` возвращает service document; `GET /$metadata` — XML CSDL. Каждый Entity Set поддерживает
ключ вида `Equipment('EQ-0011')`. Списки требуют один из перечисленных фильтров:

| Entity Set | Ключ | Поддерживаемый `$filter` |
|---|---|---|
| Equipment | EquipmentId | `SerialNumber eq '...'` либо `BusinessPartnerId eq '...'` |
| BusinessPartners | BusinessPartnerId | `BusinessPartnerId eq '...'` |
| ServiceContracts | ContractId | `BusinessPartnerId eq '...'`, дополнительно `and ValidFrom le YYYY-MM-DD and ValidTo ge YYYY-MM-DD` на одну дату |
| ServiceHistory | ServiceOrderId | `EquipmentId eq '...'` |
| ServiceCenters | CenterId | `TerritoryId eq '...'` |
| Technicians | TechnicianId | `TerritoryId eq '...'` |
| Parts | PartId | `ModelId eq '...'` |
| Models | ModelId | `ModelId eq '...'`; цена оборудования — поле PriceRub |
| WorkOrderDrafts | DocumentId | `DocumentId eq '...'` |

Строковые литералы экранируют апостроф как `''`. Значения передаются SQL-параметрами;
произвольные выражения не исполняются. `$select` выбирает известные структурные поля;
у Equipment доступен `$expand=Model,BusinessPartner`. Коллекции возвращают `value: [...]`,
объект по ключу — отдельный JSON; даты имеют ISO-формат. Поля описаны в `$metadata`.
Справочники моделей и запчастей доступны известному пользователю; территориальные объекты
ограничены территорией и партнёром. `400` означает неподдерживаемый запрос, `401` — отсутствие
служебной авторизации, `403` — отказ по правам/CSRF, `404` — отсутствующий ключ,
`409` — конфликт ключа идемпотентности, `500` — сбой источника. Ошибка имеет объект
`error` с полями `code`, `message`, `target` при наличии. Отсутствие результата поиска
по серийному номеру — пустой список; чужое оборудование даёт явный отказ.

Запись: `GET /` с `X-CSRF-Token: Fetch`, затем `POST /WorkOrderDrafts` с полученным токеном,
`Idempotency-Key` и PascalCase-полями DraftRequest: IdempotencyKey, EquipmentId, ExecutionMode,
FulfillmentType, ServiceCenterId, TechnicianId, WarrantyPreliminary, DecisionRef, FactsSnapshot.
Внутренние ключи FactsSnapshot не преобразуются. CSRF связан с тем же пользователем,
ролью, партнёром и территориями, действует 30 минут; cookie session не имитируется.
Создание возвращает 201, повтор того же тела — 200 и Replayed=true; повтор с другим телом — 409.
Уникальность ключа обеспечена PostgreSQL, включая конкурентные запросы.

Пагинация, `$batch`, произвольный `$filter`, nested expand и изменение других сущностей
вне поддерживаемого subset. SapODataAdapter не следует nextLink и не повторяет запись без ключа.
Для настоящего S/4HANA может понадобиться другой mapping/auth; работоспособность там не проверена.

Ориентиры контракта: [OASIS OData protocol](https://docs.oasis-open.org/odata/odata/v4.01/odata-v4.01-part1-protocol.html)
и [SAP CSRF flow](https://help.sap.com/docs/SAP_Cloud_Platform_Master_Data_for_Business_Partners/baa3bb2e1c3e45b7a55bcc326922471a/56e451cb6ed348779878db1e8bc24f90.html).
Они не означают полной реализации этих спецификаций в MVP.

## Совместимые старые endpoints /v1

| Метод | Путь | Проверка прав |
|---|---|---|
| GET | `/health` | Без авторизации |
| GET | `/v1/equipment/by-serial/{serial}` | Территория и партнёр объекта |
| GET | `/v1/equipment/{equipment_id}` | Территория и партнёр объекта |
| GET | `/v1/equipment/{equipment_id}/service-history` | Территория и партнёр объекта |
| GET | `/v1/partners/{partner_id}` | Территория и партнёр объекта |
| GET | `/v1/partners/{partner_id}/contracts?active_on=` | Территория и партнёр объекта |
| GET | `/v1/service-centers?territory_id=` | Указанная территория |
| GET | `/v1/technicians?territory_id=` | Указанная территория |
| GET | `/v1/parts?model_id=` | Известный пользователь |
| GET | `/v1/models/{model_id}/price` | Известный пользователь |
| POST | `/v1/work-order-drafts` | Роль «Диспетчер» или «Сервис-менеджер», территория оборудования |
| GET | `/v1/work-order-drafts/{document_id}` | Территория и партнёр оборудования |

## Авторизация

- **Сервисный токен.** Каждый запрос `/v1` и OData несёт `Authorization: Bearer <токен>`. Токен — секрет
  `mock_erp_token`. Без токена ответ 401.
- **Контекст авторизации.** Заголовки `X-Auth-User` и `X-Auth-Territories`. Без них ответ 400.
  Действуют только те территории, что есть и в заголовке, и в реестре прав самой учётной системы,
  поэтому присланную чужую территорию нельзя использовать.
- **Отказ, а не пустой результат.** Объект другой территории, а для представителя партнёра —
  другого партнёра, даёт 403. Событие пишется в `audit_event` отдельной транзакцией, чтобы откат
  запроса его не стёр. Списки центров и исполнителей требуют явную территорию и тоже отвечают 403
  вне зоны ответственности.

## Идемпотентность черновика

Ключ идемпотентности обязателен. Повтор с тем же ключом и тем же содержимым возвращает ранее
созданный документ с признаком `replayed: true`; второй документ не создаётся. Тот же ключ с другим
содержимым даёт 409. При одновременных запросах с одним ключом срабатывает уникальность в базе,
и оба получают один документ.

## Данные

Контейнер при старте заполняет пустую базу из справочников (`python -m app.seed --if-empty`).
Полная очистка с повторным заполнением, включая черновики и аудит:

```bash
docker exec mock-erp python -m app.seed --reset
```

## Проверка

`../tests/integration/test_mock_erp.py` — разграничение доступа, аудит отказов, идемпотентность.
Тест запускается в контейнере в сети стека, перед прогоном база перезаполняется.
