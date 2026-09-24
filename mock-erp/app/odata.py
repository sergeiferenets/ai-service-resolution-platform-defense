"""ZAI_SERVICE: ограниченный custom OData V4 façade над существующим ERP.

Все факты, ACL, аудит и идемпотентность остаются в существующих операциях
app.main и PostgreSQL. Это не реализация released SAP API или полного OData.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
import time
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.auth import Caller, audit, get_caller, require_service_token

BASE = '/sap/opu/odata4/sap/zai_service/0001'
# Entity set -> key, structural properties. Collection fields are explicit.
SCHEMA = {
    'Equipment': ('EquipmentId', 'EquipmentId SerialNumber ModelId BusinessPartnerId TerritoryId SaleDate Location Status AssignedTechnicianId'),
    'BusinessPartners': ('BusinessPartnerId', 'BusinessPartnerId Name Kind TerritoryId Address ContactPerson Phone'),
    'ServiceContracts': ('ContractId', 'ContractId BusinessPartnerId Kind ValidFrom ValidTo ReactionHours ResolutionSla Coverage AssignedTechnicianId'),
    'ServiceHistory': ('ServiceOrderId', 'ServiceOrderId EquipmentId Date Symptom ErrorCode Diagnosis WorkDone PartId TechnicianId Billing LaborHours'),
    'ServiceCenters': ('CenterId', 'CenterId Name IsInternal ServiceOrgId SupplierId TerritoryId Address Brands SkillIds'),
    'Technicians': ('TechnicianId', 'TechnicianId FullName ServiceOrgId TerritoryId Schedule BusyUntil SkillIds'),
    'Parts': ('PartId', 'PartId Name Stock WarehouseTerritoryId PriceRub LeadTimeDays'),
    'Models': ('ModelId', 'ModelId Manufacturer Name EquipmentType PriceRub'),
    'WorkOrderDrafts': ('DocumentId', 'DocumentId IdempotencyKey EquipmentId BusinessPartnerId ExecutionMode FulfillmentType ServiceCenterId TechnicianId WarrantyPreliminary DecisionRef FactsSnapshot CreatedBy CreatedAt Replayed'),
}


class ODataRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def wrapped(request):
            try:
                response = await handler(request)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {'message': str(exc.detail)}
                response = JSONResponse(status_code=exc.status_code, content={'error': {
                    'code': detail.get('error', f'ZAI/{exc.status_code}'),
                    'message': detail.get('reason') or detail.get('message') or 'Запрос отклонён',
                    'target': detail.get('object_type', '')}})
            except (RequestValidationError, ValidationError, ValueError):
                response = JSONResponse(status_code=400, content={'error': {
                    'code': 'ZAI/INVALID_REQUEST', 'message': 'Запрос не соответствует контракту ZAI_SERVICE'}})
            except Exception:
                response = JSONResponse(status_code=500, content={'error': {
                    'code': 'ZAI/INTERNAL_ERROR', 'message': 'Источник временно не смог выполнить запрос'}})
            response.headers['OData-Version'] = '4.0'
            return response
        return wrapped


router = APIRouter(prefix=BASE, route_class=ODataRoute, dependencies=[Depends(require_service_token)])


def name(field: str) -> str:
    return 'BusinessPartnerId' if field == 'partner_id' else ''.join(w.title() for w in field.split('_'))


def dto(row: dict) -> dict:
    if isinstance(row.get('created_at'), str):
        row = {**row, 'created_at': dt.datetime.fromisoformat(row['created_at'])}
    return {name(k): v for k, v in row.items() if k != 'model'}


def csrf_token(request: Request, caller: Caller, stamp: int) -> str:
    context = json.dumps([stamp, caller.user_id, caller.role, caller.partner_id, sorted(caller.effective)],
                         separators=(',', ':'), ensure_ascii=False).encode()
    signature = hmac.new(request.app.state.settings.service_token.encode(), context, hashlib.sha256).hexdigest()
    return f'{stamp}.{signature}'


def check_csrf(request: Request, caller: Caller) -> None:
    presented = request.headers.get('X-CSRF-Token', '')
    try:
        stamp = int(presented.split('.')[0])
        valid = 0 <= int(time.time()) - stamp <= 1800 and hmac.compare_digest(presented, csrf_token(request, caller, stamp))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        audit(request, event_type='csrf_denied', caller=caller, object_type='work_order_draft')
        raise HTTPException(403, detail={'error': 'ZAI/CSRF_REQUIRED', 'message': 'Получите X-CSRF-Token под тем же контекстом'})


@router.get('/')
def service_document(request: Request, caller: Caller = Depends(get_caller)):
    headers = {}
    if request.headers.get('X-CSRF-Token', '').lower() == 'fetch':
        headers['X-CSRF-Token'] = csrf_token(request, caller, int(time.time()))
    return JSONResponse({'@odata.context': BASE + '/$metadata',
                         'value': [{'name': n, 'kind': 'EntitySet', 'url': n} for n in SCHEMA]}, headers=headers)


@router.get('/$metadata')
def metadata(caller: Caller = Depends(get_caller)):
    edm = 'http://docs.oasis-open.org/odata/ns/edm'
    edmx = 'http://docs.oasis-open.org/odata/ns/edmx'
    ET.register_namespace('edmx', edmx)
    ET.register_namespace('', edm)
    document = ET.Element(f'{{{edmx}}}Edmx', Version='4.0')
    data = ET.SubElement(document, f'{{{edmx}}}DataServices')
    schema = ET.SubElement(data, f'{{{edm}}}Schema', Namespace='ZAI_SERVICE')
    ET.SubElement(schema, f'{{{edm}}}ComplexType', Name='FactsSnapshot', OpenType='true')
    dates = {'SaleDate', 'ValidFrom', 'ValidTo', 'Date', 'BusyUntil'}
    ints = {'ReactionHours', 'Stock', 'PriceRub', 'LeadTimeDays'}
    for entity, (key, fields) in SCHEMA.items():
        node = ET.SubElement(schema, f'{{{edm}}}EntityType', Name=entity)
        ET.SubElement(ET.SubElement(node, f'{{{edm}}}Key'), f'{{{edm}}}PropertyRef', Name=key)
        for field in fields.split():
            kind = ('Edm.Date' if field in dates else 'Edm.Int64' if field in ints else
                    'Edm.Boolean' if field in {'IsInternal', 'Replayed'} else
                    'Collection(Edm.String)' if field in {'Brands', 'SkillIds'} else
                    'Edm.DateTimeOffset' if field == 'CreatedAt' else
                    'Edm.Decimal' if field == 'LaborHours' else
                    'ZAI_SERVICE.FactsSnapshot' if field == 'FactsSnapshot' else 'Edm.String')
            ET.SubElement(node, f'{{{edm}}}Property', Name=field, Type=kind, Nullable='false' if field == key else 'true')
        if entity == 'Equipment':
            ET.SubElement(node, f'{{{edm}}}NavigationProperty', Name='Model', Type='ZAI_SERVICE.Models')
            ET.SubElement(node, f'{{{edm}}}NavigationProperty', Name='BusinessPartner', Type='ZAI_SERVICE.BusinessPartners')
    container = ET.SubElement(schema, f'{{{edm}}}EntityContainer', Name='Container')
    for entity in SCHEMA:
        node = ET.SubElement(container, f'{{{edm}}}EntitySet', Name=entity, EntityType='ZAI_SERVICE.' + entity)
        if entity == 'Equipment':
            ET.SubElement(node, f'{{{edm}}}NavigationPropertyBinding', Path='Model', Target='Models')
            ET.SubElement(node, f'{{{edm}}}NavigationPropertyBinding', Path='BusinessPartner', Target='BusinessPartners')
    return Response(ET.tostring(document, encoding='utf-8', xml_declaration=True), media_type='application/xml')


TERM = re.compile(r"([A-Za-z][A-Za-z0-9]*)\s+(eq|le|ge)\s+('(?:[^']|'')*'|\d{4}-\d{2}-\d{2})(?=\s+and\s+|$)")


def filters(value: str) -> dict[tuple[str, str], str]:
    if not value or len(value) > 2048:
        raise HTTPException(400, 'Нужен поддерживаемый $filter')
    result = {}
    rest = value.strip()
    while rest:
        match = TERM.match(rest)
        if not match: raise HTTPException(400, 'Неподдерживаемое выражение $filter')
        field, op, raw = match.groups()
        if (field, op) in result: raise HTTPException(400, 'Повтор условия $filter')
        if field in {'ValidFrom', 'ValidTo'}:
            dt.date.fromisoformat(raw)
        elif not raw.startswith("'"):
            raise HTTPException(400, 'Строковое значение должно быть в кавычках')
        result[field, op] = raw[1:-1].replace("''", "'") if raw.startswith("'") else raw
        rest = rest[match.end():]
        if rest:
            rest = re.sub(r'^\s+and\s+', '', rest, count=1)
    return result


def options(request: Request, entity: str, *, keyed=False):
    allowed = {'$select', '$expand'} | (set() if keyed else {'$filter'})
    if any(k not in allowed for k in request.query_params) or len(request.query_params.multi_items()) != len(request.query_params):
        raise HTTPException(400, 'Неподдерживаемые или повторные query options')
    selected = request.query_params.get('$select')
    selected = set(selected.split(',')) if selected else None
    if selected and not selected <= set(SCHEMA[entity][1].split()):
        raise HTTPException(400, 'Неизвестное поле $select')
    expanded = set(filter(None, request.query_params.get('$expand', '').split(',')))
    if expanded and (entity != 'Equipment' or not expanded <= {'Model', 'BusinessPartner'}):
        raise HTTPException(400, 'Неподдерживаемое $expand')
    return selected, expanded


def project(row, selected, expanded):
    return {k: v for k, v in row.items() if selected is None or k in selected or k in expanded}


def equipment(row, request, caller, expanded):
    from app import main as erp
    value = dto(row)
    value['ModelId'] = row['model']['model_id']
    if 'Model' in expanded:
        value['Model'] = dto(erp.one(request, 'SELECT model_id, manufacturer, name, equipment_type, price_rub FROM model WHERE model_id = %s',
                                    (value['ModelId'],), 'Модель не найдена'))
    if 'BusinessPartner' in expanded:
        value['BusinessPartner'] = dto(erp.load_partner(request, caller, row['partner_id']))
    return value


def collection_rows(entity, terms, request, caller, expanded):
    from app import main as erp
    keys = set(terms)
    if entity == 'Equipment' and keys == {('SerialNumber', 'eq')}:
        try:
            rows = [erp.equipment_by_serial(terms['SerialNumber', 'eq'], request, caller)]
        except HTTPException as exc:
            if exc.status_code != 404: raise
            rows = []
        return [equipment(r, request, caller, expanded) for r in rows]
    if entity == 'Equipment' and keys == {('BusinessPartnerId', 'eq')}:
        return [equipment(r, request, caller, expanded) for r in
                erp.partner_equipment(terms['BusinessPartnerId', 'eq'], request, model=None, caller=caller)]
    if entity == 'ServiceContracts' and keys in ({('BusinessPartnerId', 'eq')},
            {('BusinessPartnerId', 'eq'), ('ValidFrom', 'le'), ('ValidTo', 'ge')}):
        active_on = None
        if ('ValidFrom', 'le') in terms:
            if terms['ValidFrom', 'le'] != terms['ValidTo', 'ge']:
                raise HTTPException(400, 'Поддерживается проверка договора на одну дату')
            active_on = dt.date.fromisoformat(terms['ValidFrom', 'le'])
        return [dto(r) for r in erp.contracts(terms['BusinessPartnerId', 'eq'], request, active_on, caller)]
    if entity == 'ServiceHistory' and keys == {('EquipmentId', 'eq')}:
        eq = terms['EquipmentId', 'eq']
        return [{**dto(r), 'EquipmentId': eq} for r in erp.service_history(eq, request, caller)]
    operations = {'ServiceCenters': ('TerritoryId', erp.service_centers),
                  'Technicians': ('TerritoryId', erp.technicians), 'Parts': ('ModelId', erp.parts)}
    if entity in operations:
        field, operation = operations[entity]
        if keys == {(field, 'eq')}:
            return [dto(r) for r in operation(terms[field, 'eq'], request, caller)]
    single = {'BusinessPartners': ('BusinessPartnerId', erp.partner),
              'Models': ('ModelId', lambda key, req, who: erp.one(req,
                    'SELECT model_id, manufacturer, name, equipment_type, price_rub FROM model WHERE model_id = %s',
                    (key,), 'Модель не найдена')),
              'WorkOrderDrafts': ('DocumentId', erp.get_draft)}
    if entity in single:
        field, operation = single[entity]
        if keys == {(field, 'eq')}:
            try:
                return [dto(operation(terms[field, 'eq'], request, caller))]
            except HTTPException as exc:
                if exc.status_code != 404: raise
                return []
    raise HTTPException(400, 'Этот набор условий $filter не поддерживается')


def lookup(entity, key, request, caller, expanded):
    from app import main as erp
    if entity == 'Equipment':
        return equipment(erp.equipment_by_id(key, request, caller), request, caller, expanded)
    field = SCHEMA[entity][0]
    if entity in {'BusinessPartners', 'Models', 'WorkOrderDrafts'}:
        rows = collection_rows(entity, {(field, 'eq'): key}, request, caller, expanded)
    elif entity == 'Parts':
        return dto(erp.one(request, 'SELECT * FROM part WHERE part_id = %s', (key,), 'Запчасть не найдена'))
    else:
        # Известные имена столбцов задаёт код; значения никогда не входят в SQL.
        references = {'ServiceContracts': ('contract', 'contract_id', 'partner_id', 'BusinessPartnerId'),
                      'ServiceHistory': ('service_history', 'service_order_id', 'equipment_id', 'EquipmentId'),
                      'ServiceCenters': ('service_center', 'center_id', 'territory_id', 'TerritoryId'),
                      'Technicians': ('technician', 'technician_id', 'territory_id', 'TerritoryId')}
        table, column, parent, filter_field = references[entity]
        ref = erp.one(request, f'SELECT {parent} FROM {table} WHERE {column} = %s', (key,), 'Объект не найден')
        rows = collection_rows(entity, {(filter_field, 'eq'): ref[parent]}, request, caller, expanded)
        rows = [r for r in rows if r[field] == key]
    if not rows: raise HTTPException(404, 'Объект не найден')
    return rows[0]


@router.get('/{resource}')
def read(resource: str, request: Request, caller: Caller = Depends(get_caller)):
    match = re.fullmatch(r"([A-Za-z]+)(?:\('((?:[^']|'')*)'\))?", resource)
    if not match or match[1] not in SCHEMA:
        raise HTTPException(404, 'Неизвестный Entity Set или ключ')
    entity, key = match.groups()
    selected, expanded = options(request, entity, keyed=key is not None)
    context = BASE + '/$metadata#' + entity
    if key is not None:
        value = project(lookup(entity, key.replace("''", "'"), request, caller, expanded), selected, expanded)
        return JSONResponse(jsonable_encoder({'@odata.context': context + '/$entity', **value}))
    terms = filters(request.query_params.get('$filter', ''))
    rows = collection_rows(entity, terms, request, caller, expanded)
    return JSONResponse(jsonable_encoder({'@odata.context': context,
                                         'value': [project(r, selected, expanded) for r in rows]}))


@router.post('/WorkOrderDrafts')
def create(body: dict, request: Request, caller: Caller = Depends(get_caller)):
    from app import main as erp
    check_csrf(request, caller)
    if request.query_params:
        raise HTTPException(400, 'Query options для записи не поддерживаются')
    mapping = {name(k): k for k in erp.DraftRequest.model_fields}
    if set(body) - set(mapping): raise HTTPException(400, 'Неизвестные поля черновика')
    draft = erp.DraftRequest.model_validate({mapping[k]: v for k, v in body.items()})
    if request.headers.get('Idempotency-Key') != draft.idempotency_key:
        raise HTTPException(400, 'Idempotency-Key должен совпадать с IdempotencyKey тела')
    result = erp.create_draft(draft, request, caller)
    if isinstance(result, JSONResponse):
        row = json.loads(result.body)
        status = result.status_code
    else:
        row, status = result, 201
    return JSONResponse(jsonable_encoder({'@odata.context': BASE + '/$metadata#WorkOrderDrafts/$entity', **dto(row)}),
                        status_code=status, headers={'Location': BASE + "/WorkOrderDrafts('" + row['document_id'] + "')"})
