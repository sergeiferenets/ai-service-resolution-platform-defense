"""Кэш ответов на чтение в пределах одного обращения (ADR-0006).

Экземпляр создаётся на разбор одного обращения и выбрасывается вместе с ним.
Ключ включает контекст авторизации: ответ, полученный под одними правами,
под другими не отдаётся. Кэшируются только успешные ответы — отказ в
доступе каждый раз идёт в источник и попадает в его аудит. Запись не
кэшируется.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from backend.adapters.port import ErpPort
from backend.domain.models import (AuthContext, Contract, DraftRequest, DraftResult, Equipment, Partner,
                                   PartStock, ServiceCenter, ServiceHistoryEntry, Technician)


class RequestScopedCache:
    def __init__(self, inner: ErpPort):
        self._inner = inner
        self._store: dict[tuple, Any] = {}
        self.hits = 0
        self.misses = 0

    def _read(self, name: str, ctx: AuthContext, *args) -> Any:
        if not isinstance(ctx, AuthContext):
            raise TypeError("Контекст авторизации обязателен: ожидается AuthContext")
        key = (name, ctx, args)
        if key in self._store:
            self.hits += 1
        else:
            self.misses += 1
            fetch: Callable = getattr(self._inner, name)
            self._store[key] = fetch(ctx, *args)
        value = self._store[key]
        return list(value) if isinstance(value, list) else value

    def find_equipment_by_serial(self, ctx: AuthContext, serial_number: str) -> Equipment:
        return self._read("find_equipment_by_serial", ctx, serial_number)

    def find_partner_equipment(self, ctx: AuthContext, partner_id: str,
                               model_designation: str | None) -> list[Equipment]:
        return self._read("find_partner_equipment", ctx, partner_id, model_designation)

    def get_equipment(self, ctx: AuthContext, equipment_id: str) -> Equipment:
        return self._read("get_equipment", ctx, equipment_id)

    def get_partner(self, ctx: AuthContext, partner_id: str) -> Partner:
        return self._read("get_partner", ctx, partner_id)

    def list_contracts(self, ctx: AuthContext, partner_id: str, active_on: dt.date) -> list[Contract]:
        return self._read("list_contracts", ctx, partner_id, active_on)

    def service_history(self, ctx: AuthContext, equipment_id: str) -> list[ServiceHistoryEntry]:
        return self._read("service_history", ctx, equipment_id)

    def list_service_centers(self, ctx: AuthContext, territory_id: str) -> list[ServiceCenter]:
        return self._read("list_service_centers", ctx, territory_id)

    def list_technicians(self, ctx: AuthContext, territory_id: str) -> list[Technician]:
        return self._read("list_technicians", ctx, territory_id)

    def list_parts(self, ctx: AuthContext, model_id: str) -> list[PartStock]:
        return self._read("list_parts", ctx, model_id)

    def product_price(self, ctx: AuthContext, model_id: str) -> int:
        return self._read("product_price", ctx, model_id)

    def create_draft(self, ctx: AuthContext, draft: DraftRequest) -> DraftResult:
        return self._inner.create_draft(ctx, draft)
