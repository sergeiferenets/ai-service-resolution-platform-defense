"""Порт учётной системы (ADR-0006).

Контекст авторизации — первый обязательный параметр каждого метода: не
поле объекта, не глобальная переменная и не значение по умолчанию. Вызов
без него невозможен по сигнатуре; тест tests/unit/test_adapter.py проверяет
это для порта и для каждой реализации.

find_partner_equipment — оборудование партнёра, а с обозначением модели —
только подходящее. Так диспетчер определяет аппарат, когда серийный номер
не указан (PRD 5.3). Пустой список — факт, а не отказ.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from backend.domain.models import (AuthContext, Contract, DraftRequest, DraftResult, Equipment, Partner,
                                   PartStock, ServiceCenter, ServiceHistoryEntry, Technician)


class ErpPort(Protocol):
    def find_equipment_by_serial(self, ctx: AuthContext, serial_number: str) -> Equipment: ...

    def find_partner_equipment(self, ctx: AuthContext, partner_id: str,
                               model_designation: str | None) -> list[Equipment]: ...

    def get_equipment(self, ctx: AuthContext, equipment_id: str) -> Equipment: ...

    def get_partner(self, ctx: AuthContext, partner_id: str) -> Partner: ...

    def list_contracts(self, ctx: AuthContext, partner_id: str, active_on: dt.date) -> list[Contract]: ...

    def service_history(self, ctx: AuthContext, equipment_id: str) -> list[ServiceHistoryEntry]: ...

    def list_service_centers(self, ctx: AuthContext, territory_id: str) -> list[ServiceCenter]: ...

    def list_technicians(self, ctx: AuthContext, territory_id: str) -> list[Technician]: ...

    def list_parts(self, ctx: AuthContext, model_id: str) -> list[PartStock]: ...

    def product_price(self, ctx: AuthContext, model_id: str) -> int: ...

    def create_draft(self, ctx: AuthContext, draft: DraftRequest) -> DraftResult: ...


PORT_METHODS = (
    "find_equipment_by_serial", "find_partner_equipment", "get_equipment", "get_partner", "list_contracts",
    "service_history", "list_service_centers", "list_technicians", "list_parts", "product_price", "create_draft",
)
READ_METHODS = tuple(m for m in PORT_METHODS if m != "create_draft")
