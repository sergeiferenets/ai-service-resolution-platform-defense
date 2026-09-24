"""Запись вызовов адаптера в TOOL_CALL (data-model.md).

Обёртка над портом на время одного шага процесса: каждый вызов — строка
TOOL_CALL со ссылкой на шаг, а через шаг — на версию набора артефактов.
Отказ источника в доступе дополнительно пишется в AUDIT_EVENT платформы:
учётная система ведёт свой аудит, платформа — свой (ADR-0007, Compliance).

Сигнатуры повторяют порт: контекст авторизации обязателен и здесь.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from typing import TYPE_CHECKING, Any

from backend.adapters.port import ErpPort
from backend.domain.errors import (AccessDenied, IdempotencyConflict, NotFound, SourceContractError,
                                   SourceUnavailable)
from backend.domain.models import (AuthContext, Contract, DraftRequest, DraftResult, Equipment, Partner,
                                   PartStock, ServiceCenter, ServiceHistoryEntry, Technician)
from backend.orchestrator.serialize import to_jsonable

if TYPE_CHECKING:
    from backend.orchestrator.store import Store

SUCCESS, DENIED, DEGRADED = "успех", "отказ", "деградация"


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def failure_payload(exc: Exception) -> tuple[dict, str]:
    if isinstance(exc, AccessDenied):
        return ({"error": "access_denied", "http_status": 403, "reason": exc.reason,
                 "object_type": exc.object_type, "object_id": exc.object_id}, DENIED)
    if isinstance(exc, NotFound):
        return {"error": "not_found", "http_status": 404, "detail": str(exc)}, DENIED
    if isinstance(exc, IdempotencyConflict):
        return {"error": "idempotency_conflict", "http_status": 409, "detail": str(exc)}, DENIED
    if isinstance(exc, (SourceUnavailable, SourceContractError)):
        return {"error": type(exc).__name__, "detail": str(exc)}, DEGRADED
    return {"error": type(exc).__name__, "detail": str(exc)}, DENIED


class RecordingErp:
    def __init__(self, inner: ErpPort, store: Store, *, request_id: uuid.UUID, step_id: uuid.UUID):
        self._inner = inner
        self._store = store
        self._request_id = request_id
        self._step_id = step_id

    def _call(self, name: str, ctx: AuthContext, *args: Any) -> Any:
        if not isinstance(ctx, AuthContext):
            raise TypeError("Контекст авторизации обязателен: ожидается AuthContext")
        tool = f"erp.{name}"
        request = {"auth": {"user_id": ctx.user_id, "role": ctx.role, "territories": sorted(ctx.territories)},
                   "args": to_jsonable(list(args))}
        started = time.perf_counter()
        try:
            result = getattr(self._inner, name)(ctx, *args)
        except Exception as exc:
            response, outcome = failure_payload(exc)
            self._store.record_tool_call(self._step_id, tool, request, response, elapsed_ms(started), outcome)
            if isinstance(exc, AccessDenied):
                self._store.audit(self._request_id, ctx.user_id, "access_denied",
                                  after={"source": "учётная система", "tool": tool, **response})
            raise
        self._store.record_tool_call(self._step_id, tool, request, result, elapsed_ms(started), SUCCESS)
        return result

    def find_equipment_by_serial(self, ctx: AuthContext, serial_number: str) -> Equipment:
        return self._call("find_equipment_by_serial", ctx, serial_number)

    def find_partner_equipment(self, ctx: AuthContext, partner_id: str,
                               model_designation: str | None) -> list[Equipment]:
        return self._call("find_partner_equipment", ctx, partner_id, model_designation)

    def get_equipment(self, ctx: AuthContext, equipment_id: str) -> Equipment:
        return self._call("get_equipment", ctx, equipment_id)

    def get_partner(self, ctx: AuthContext, partner_id: str) -> Partner:
        return self._call("get_partner", ctx, partner_id)

    def list_contracts(self, ctx: AuthContext, partner_id: str, active_on: dt.date) -> list[Contract]:
        return self._call("list_contracts", ctx, partner_id, active_on)

    def service_history(self, ctx: AuthContext, equipment_id: str) -> list[ServiceHistoryEntry]:
        return self._call("service_history", ctx, equipment_id)

    def list_service_centers(self, ctx: AuthContext, territory_id: str) -> list[ServiceCenter]:
        return self._call("list_service_centers", ctx, territory_id)

    def list_technicians(self, ctx: AuthContext, territory_id: str) -> list[Technician]:
        return self._call("list_technicians", ctx, territory_id)

    def list_parts(self, ctx: AuthContext, model_id: str) -> list[PartStock]:
        return self._call("list_parts", ctx, model_id)

    def product_price(self, ctx: AuthContext, model_id: str) -> int:
        return self._call("product_price", ctx, model_id)

    def create_draft(self, ctx: AuthContext, draft: DraftRequest) -> DraftResult:
        return self._call("create_draft", ctx, draft)
