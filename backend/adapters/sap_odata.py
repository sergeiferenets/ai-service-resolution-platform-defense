"""ErpPort поверх custom ZAI_SERVICE (OData V4 subset), ADR-0006.

Никакой зависимости от MockErpAdapter. Endpoint/auth задаются окружением;
совместимость с released SAP API не заявляется. Контракт описан в mock-erp/README.md.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import asdict
from urllib.parse import quote

import httpx

from backend.domain.errors import AccessDenied, IdempotencyConflict, NotFound, SourceContractError, SourceUnavailable
from backend.domain.models import (AuthContext, Contract, DraftRequest, DraftResult, Equipment, ModelInfo,
                                   Partner, PartStock, ServiceCenter, ServiceHistoryEntry, Technician)


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def key_path(entity: str, value: str) -> str:
    return entity + '(' + quote(literal(value), safe="'") + ')'


def date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def field_name(value: str) -> str:
    return 'BusinessPartnerId' if value == 'partner_id' else ''.join(s.title() for s in value.split('_'))


class SapODataAdapter:
    def __init__(self, base_url: str, token: str, *, client: httpx.Client | None = None,
                 read_timeout_s: float = 5, write_timeout_s: float = 15, retry_delay_s: float = .5):
        self._client = client or httpx.Client(base_url=base_url.rstrip('/') + '/')
        self._base_url = base_url.rstrip('/') + '/'
        self._token = token
        self._read_timeout = read_timeout_s
        self._write_timeout = write_timeout_s
        self._retry_delay = retry_delay_s

    def _request(self, ctx: AuthContext, method: str, path: str, *, params=None, body=None,
                 idempotency_key: str | None = None, csrf: str | None = None) -> httpx.Response:
        if not isinstance(ctx, AuthContext):
            raise TypeError('Контекст авторизации обязателен')
        write = method != 'GET'
        headers = {'Authorization': f'Bearer {self._token}', 'X-Auth-User': ctx.user_id,
                   'X-Auth-Territories': ','.join(sorted(ctx.territories)), 'OData-Version': '4.0',
                   'Accept': 'application/json'}
        if csrf:
            headers['X-CSRF-Token'] = csrf
        if idempotency_key:
            headers['Idempotency-Key'] = idempotency_key
        attempts = 2 if not write or idempotency_key else 1
        failure = None
        for attempt in range(attempts):
            try:
                response = self._client.request(method, self._base_url + path, params=params, json=body,
                                                headers=headers, timeout=self._write_timeout if write else self._read_timeout)
            except httpx.TransportError as exc:
                failure = type(exc).__name__
            else:
                if response.status_code < 500:
                    self._check(response)
                    return response
                failure = f'HTTP {response.status_code}'
            if attempt + 1 < attempts:
                time.sleep(self._retry_delay)
        raise SourceUnavailable(f'{method} {path}: источник недоступен ({failure})')

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        try:
            payload = response.json()
            error = payload.get('error', {}) if isinstance(payload, dict) else {}
            if not isinstance(error, dict): error = {}
        except ValueError:
            error = {}
        message = error.get('message') or f'HTTP {response.status_code}'
        if response.status_code in (401, 403):
            raise AccessDenied(str(message), error.get('target'), None)
        if response.status_code == 404:
            raise NotFound(str(message))
        if response.status_code == 409:
            raise IdempotencyConflict(str(message))
        raise SourceContractError(f'OData HTTP {response.status_code}: {message}')

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            value = response.json()
            if isinstance(value, dict) and 'error' not in value:
                return value
        except ValueError:
            pass
        raise SourceContractError('Ответ OData не является объектом по контракту')

    def _entity(self, ctx: AuthContext, entity: str, key: str, **params) -> dict:
        return self._json(self._request(ctx, 'GET', key_path(entity, key), params=params))

    def _collection(self, ctx: AuthContext, entity: str, condition: str, **params) -> list[dict]:
        payload = self._json(self._request(ctx, 'GET', entity, params={'$filter': condition, **params}))
        value = payload.get('value')
        if not isinstance(value, list) or not all(isinstance(r, dict) for r in value) or '@odata.nextLink' in payload:
            raise SourceContractError('Ожидалась полная OData collection value без пагинации')
        return value

    @staticmethod
    def _equipment(row: dict) -> Equipment:
        try:
            m = row['Model']
            return Equipment(row['EquipmentId'], row['SerialNumber'],
                             ModelInfo(m['ModelId'], m['Manufacturer'], m['Name'], m['EquipmentType']),
                             row['BusinessPartnerId'], row['TerritoryId'], date(row['SaleDate']),
                             row.get('Location'), row['Status'], row.get('AssignedTechnicianId'))
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceContractError('Equipment/Model не соответствует контракту ZAI_SERVICE') from exc

    def find_equipment_by_serial(self, ctx: AuthContext, serial_number: str) -> Equipment:
        rows = self._collection(ctx, 'Equipment', f'SerialNumber eq {literal(serial_number)}', **{'$expand': 'Model'})
        if not rows: raise NotFound('Оборудование не найдено')
        if len(rows) != 1: raise SourceContractError('Серийный номер не уникален')
        return self._equipment(rows[0])

    def get_equipment(self, ctx: AuthContext, equipment_id: str) -> Equipment:
        return self._equipment(self._entity(ctx, 'Equipment', equipment_id, **{'$expand': 'Model'}))

    def find_partner_equipment(self, ctx: AuthContext, partner_id: str, model_designation: str | None) -> list[Equipment]:
        rows = self._collection(ctx, 'Equipment', f'BusinessPartnerId eq {literal(partner_id)}', **{'$expand': 'Model'})
        equipment = [self._equipment(row) for row in rows]
        if model_designation is None:
            return equipment
        # Сохраняем прежнее сопоставление обозначений. ACL уже применён источником.
        tokens = ''.join(c if c.isalnum() else ' ' for c in model_designation.lower()).split()
        meaningful = [t for t in tokens if len(t) >= 2 and any(c.isdigit() for c in t)]
        meaningful = meaningful or [t for t in tokens if len(t) >= 3]
        return [e for e in equipment if meaningful and all(
            t in f'{e.model.manufacturer} {e.model.name}'.lower() for t in meaningful)]

    def get_partner(self, ctx: AuthContext, partner_id: str) -> Partner:
        r = self._entity(ctx, 'BusinessPartners', partner_id)
        return Partner(r['BusinessPartnerId'], r['Name'], r['Kind'], r['TerritoryId'])

    def list_contracts(self, ctx: AuthContext, partner_id: str, active_on: dt.date) -> list[Contract]:
        condition = (f'BusinessPartnerId eq {literal(partner_id)} and ValidFrom le {active_on.isoformat()}'
                     f' and ValidTo ge {active_on.isoformat()}')
        return [Contract(r['ContractId'], r['BusinessPartnerId'], r.get('Kind'), date(r.get('ValidFrom')),
                         date(r.get('ValidTo')), r.get('ReactionHours'), r.get('ResolutionSla'), r.get('Coverage'),
                         r.get('AssignedTechnicianId')) for r in self._collection(ctx, 'ServiceContracts', condition)]

    def service_history(self, ctx: AuthContext, equipment_id: str) -> list[ServiceHistoryEntry]:
        return [ServiceHistoryEntry(r['ServiceOrderId'], date(r.get('Date')), r.get('Symptom'), r.get('ErrorCode'),
                                   r.get('Diagnosis'), r.get('WorkDone'), r.get('PartId'), r.get('TechnicianId'),
                                   r.get('Billing')) for r in self._collection(ctx, 'ServiceHistory',
                                                                           f'EquipmentId eq {literal(equipment_id)}')]

    def list_service_centers(self, ctx: AuthContext, territory_id: str) -> list[ServiceCenter]:
        return [ServiceCenter(r['CenterId'], r['Name'], r['IsInternal'], r['ServiceOrgId'], r['TerritoryId'],
                              frozenset(r['Brands']), frozenset(r['SkillIds']))
                for r in self._collection(ctx, 'ServiceCenters', f'TerritoryId eq {literal(territory_id)}')]

    def list_technicians(self, ctx: AuthContext, territory_id: str) -> list[Technician]:
        return [Technician(r['TechnicianId'], r['FullName'], r['ServiceOrgId'], r['TerritoryId'],
                           frozenset(r['SkillIds']), date(r.get('BusyUntil')))
                for r in self._collection(ctx, 'Technicians', f'TerritoryId eq {literal(territory_id)}')]

    def list_parts(self, ctx: AuthContext, model_id: str) -> list[PartStock]:
        return [PartStock(r['PartId'], r['Name'], r['Stock'], r['WarehouseTerritoryId'], r['PriceRub'], r['LeadTimeDays'])
                for r in self._collection(ctx, 'Parts', f'ModelId eq {literal(model_id)}')]

    def product_price(self, ctx: AuthContext, model_id: str) -> int:
        return int(self._entity(ctx, 'Models', model_id, **{'$select': 'ModelId,PriceRub'})['PriceRub'])

    def create_draft(self, ctx: AuthContext, draft: DraftRequest) -> DraftResult:
        if not draft.idempotency_key:
            raise ValueError('Запись возможна только с ключом идемпотентности')
        # Токен получаем под тем же контекстом; между пользователями не кэшируем.
        csrf = self._request(ctx, 'GET', '', csrf='Fetch').headers.get('X-CSRF-Token')
        if not csrf or csrf.lower() == 'fetch':
            raise SourceContractError('Источник не выдал CSRF token')
        body = {field_name(k): v for k, v in asdict(draft).items()}
        r = self._json(self._request(ctx, 'POST', 'WorkOrderDrafts', body=body,
                                     idempotency_key=draft.idempotency_key, csrf=csrf))
        return DraftResult(r['DocumentId'], r['IdempotencyKey'], bool(r['Replayed']))
