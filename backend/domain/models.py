"""Доменная модель платформы в нейтральных терминах (ADR-0006).

Ни одно понятие конкретной учётной системы сюда не проникает: соответствие
полей поставщика этим записям существует только внутри его адаптера.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal

ExecutionMode = Literal["remote", "onsite", "workshop"]
FulfillmentType = Literal["internal", "external"]
WarrantyPreliminary = Literal["warranty", "paid"]


@dataclass(frozen=True)
class AuthContext:
    """Контекст авторизации — обязательный параметр каждого метода порта.

    Неизменяемый и хешируемый: входит в ключ кэша, поэтому ответ, полученный
    под одними правами, не может быть отдан под другими.
    """
    user_id: str
    role: str
    territories: frozenset[str]
    partner_id: str | None = None


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    manufacturer: str
    name: str
    equipment_type: str


@dataclass(frozen=True)
class Equipment:
    equipment_id: str
    serial_number: str
    model: ModelInfo
    partner_id: str
    territory_id: str
    sale_date: dt.date
    location: str | None
    status: str
    assigned_technician_id: str | None


@dataclass(frozen=True)
class Partner:
    partner_id: str
    name: str
    kind: str
    territory_id: str


@dataclass(frozen=True)
class Contract:
    contract_id: str
    partner_id: str
    kind: str | None
    valid_from: dt.date | None
    valid_to: dt.date | None
    reaction_hours: int | None
    resolution_sla: str | None
    coverage: str | None
    assigned_technician_id: str | None


@dataclass(frozen=True)
class ServiceHistoryEntry:
    service_order_id: str
    date: dt.date | None
    symptom: str | None
    error_code: str | None
    diagnosis: str | None
    work_done: str | None
    part_id: str | None
    technician_id: str | None
    billing: str | None


@dataclass(frozen=True)
class ServiceCenter:
    center_id: str
    name: str
    is_internal: bool
    service_org_id: str
    territory_id: str
    brands: frozenset[str]
    skill_ids: frozenset[str]


@dataclass(frozen=True)
class Technician:
    technician_id: str
    full_name: str
    service_org_id: str
    territory_id: str
    skill_ids: frozenset[str]
    busy_until: dt.date | None


@dataclass(frozen=True)
class PartStock:
    part_id: str
    name: str
    stock: int
    warehouse_territory_id: str
    price_rub: int
    lead_time_days: int


@dataclass(frozen=True)
class DraftRequest:
    """Черновик документа. Ключ идемпотентности обязателен (ADR-0006)."""
    idempotency_key: str
    equipment_id: str
    execution_mode: ExecutionMode
    fulfillment_type: FulfillmentType
    service_center_id: str | None
    technician_id: str | None
    warranty_preliminary: WarrantyPreliminary
    decision_ref: str
    facts_snapshot: dict = field(hash=False, compare=False)


@dataclass(frozen=True)
class DraftResult:
    document_id: str
    idempotency_key: str
    replayed: bool
