"""Реализация порта учётной системы поверх мока (HTTP).

Бюджеты и повторы по ADR-0007:
- чтение: 5 с, не более одного повтора;
- запись: 15 с, повтор допустим только при ключе идемпотентности.
Размыкатель — следующей задачей.

Контекст авторизации превращается в заголовки в одном месте — _ctx_headers.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, asdict

import httpx

from backend.domain.errors import (AccessDenied, IdempotencyConflict, NotFound, SourceContractError,
                                   SourceUnavailable)
from backend.domain.models import (AuthContext, Contract, DraftRequest, DraftResult, Equipment, ModelInfo,
                                   Partner, PartStock, ServiceCenter, ServiceHistoryEntry, Technician)


@dataclass(frozen=True)
class Budgets:
    read_timeout_s: float = 5.0
    write_timeout_s: float = 15.0
    retry_delay_s: float = 0.5


def _date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value[:10]) if value else None


class MockErpAdapter:
    def __init__(self, base_url: str, token: str, *, budgets: Budgets = Budgets(),
                 client: httpx.Client | None = None):
        self._client = client or httpx.Client(base_url=base_url)
        self._token = token
        self._budgets = budgets

    @staticmethod
    def _ctx_headers(ctx: AuthContext) -> dict[str, str]:
        if not isinstance(ctx, AuthContext):
            raise TypeError("Контекст авторизации обязателен: ожидается AuthContext")
        return {"X-Auth-User": ctx.user_id, "X-Auth-Territories": ",".join(sorted(ctx.territories))}

    def _request(self, ctx: AuthContext, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None, idempotency_key: str | None = None):
        headers = {"Authorization": f"Bearer {self._token}", **self._ctx_headers(ctx)}
        is_write = method != "GET"
        timeout = self._budgets.write_timeout_s if is_write else self._budgets.read_timeout_s
        # Чтение повторяется один раз; запись — только под защитой ключа.
        attempts = 2 if (not is_write or idempotency_key) else 1
        failure: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._client.request(method, path, params=params, json=body, headers=headers,
                                                timeout=timeout)
            except httpx.TransportError as exc:
                failure = exc
            else:
                if response.status_code < 500:
                    return self._result(response)
                failure = SourceUnavailable(f"{method} {path}: HTTP {response.status_code}")
            if attempt + 1 < attempts:
                time.sleep(self._budgets.retry_delay_s)
        raise SourceUnavailable(f"{method} {path}: источник недоступен ({failure})") from failure

    @staticmethod
    def _result(response: httpx.Response):
        status = response.status_code
        if status in (200, 201):
            return response.json()
        detail = response.json().get("detail") if response.headers.get("content-type", "").startswith(
            "application/json") else response.text
        if status == 403:
            d = detail if isinstance(detail, dict) else {}
            raise AccessDenied(d.get("reason", "доступ запрещён"), d.get("object_type"), d.get("object_id"))
        if status == 404:
            raise NotFound(str(detail))
        if status == 409:
            raise IdempotencyConflict(str(detail))
        raise SourceContractError(f"HTTP {status}: {detail}")

    @staticmethod
    def _equipment(j: dict) -> Equipment:
        m = j["model"]
        return Equipment(
            equipment_id=j["equipment_id"], serial_number=j["serial_number"],
            model=ModelInfo(m["model_id"], m["manufacturer"], m["name"], m["equipment_type"]),
            partner_id=j["partner_id"], territory_id=j["territory_id"], sale_date=_date(j["sale_date"]),
            location=j.get("location"), status=j["status"], assigned_technician_id=j.get("assigned_technician_id"))

    def find_equipment_by_serial(self, ctx: AuthContext, serial_number: str) -> Equipment:
        return self._equipment(self._request(ctx, "GET", f"/v1/equipment/by-serial/{serial_number}"))

    def find_partner_equipment(self, ctx: AuthContext, partner_id: str,
                               model_designation: str | None) -> list[Equipment]:
        params = None if model_designation is None else {"model": model_designation}
        rows = self._request(ctx, "GET", f"/v1/partners/{partner_id}/equipment", params=params)
        return [self._equipment(j) for j in rows]

    def get_equipment(self, ctx: AuthContext, equipment_id: str) -> Equipment:
        return self._equipment(self._request(ctx, "GET", f"/v1/equipment/{equipment_id}"))

    def get_partner(self, ctx: AuthContext, partner_id: str) -> Partner:
        j = self._request(ctx, "GET", f"/v1/partners/{partner_id}")
        return Partner(j["partner_id"], j["name"], j["kind"], j["territory_id"])

    def list_contracts(self, ctx: AuthContext, partner_id: str, active_on: dt.date) -> list[Contract]:
        rows = self._request(ctx, "GET", f"/v1/partners/{partner_id}/contracts",
                             params={"active_on": active_on.isoformat()})
        return [Contract(
            contract_id=r["contract_id"], partner_id=r["partner_id"], kind=r.get("kind"),
            valid_from=_date(r.get("valid_from")), valid_to=_date(r.get("valid_to")),
            reaction_hours=r.get("reaction_hours"), resolution_sla=r.get("resolution_sla"),
            coverage=r.get("coverage"), assigned_technician_id=r.get("assigned_technician_id")) for r in rows]

    def service_history(self, ctx: AuthContext, equipment_id: str) -> list[ServiceHistoryEntry]:
        rows = self._request(ctx, "GET", f"/v1/equipment/{equipment_id}/service-history")
        return [ServiceHistoryEntry(
            service_order_id=r["service_order_id"], date=_date(r.get("date")), symptom=r.get("symptom"),
            error_code=r.get("error_code"), diagnosis=r.get("diagnosis"), work_done=r.get("work_done"),
            part_id=r.get("part_id"), technician_id=r.get("technician_id"), billing=r.get("billing"))
            for r in rows]

    def list_service_centers(self, ctx: AuthContext, territory_id: str) -> list[ServiceCenter]:
        rows = self._request(ctx, "GET", "/v1/service-centers", params={"territory_id": territory_id})
        return [ServiceCenter(
            center_id=r["center_id"], name=r["name"], is_internal=r["is_internal"],
            service_org_id=r["service_org_id"], territory_id=r["territory_id"],
            brands=frozenset(r["brands"]), skill_ids=frozenset(r["skill_ids"])) for r in rows]

    def list_technicians(self, ctx: AuthContext, territory_id: str) -> list[Technician]:
        rows = self._request(ctx, "GET", "/v1/technicians", params={"territory_id": territory_id})
        return [Technician(
            technician_id=r["technician_id"], full_name=r["full_name"], service_org_id=r["service_org_id"],
            territory_id=r["territory_id"], skill_ids=frozenset(r["skill_ids"]),
            busy_until=_date(r.get("busy_until"))) for r in rows]

    def list_parts(self, ctx: AuthContext, model_id: str) -> list[PartStock]:
        rows = self._request(ctx, "GET", "/v1/parts", params={"model_id": model_id})
        return [PartStock(r["part_id"], r["name"], r["stock"], r["warehouse_territory_id"], r["price_rub"],
                          r["lead_time_days"]) for r in rows]

    def product_price(self, ctx: AuthContext, model_id: str) -> int:
        return int(self._request(ctx, "GET", f"/v1/models/{model_id}/price")["price_rub"])

    def create_draft(self, ctx: AuthContext, draft: DraftRequest) -> DraftResult:
        if not draft.idempotency_key:
            raise ValueError("Черновик создаётся только с ключом идемпотентности")
        j = self._request(ctx, "POST", "/v1/work-order-drafts", body=asdict(draft),
                          idempotency_key=draft.idempotency_key)
        return DraftResult(j["document_id"], j["idempotency_key"], bool(j["replayed"]))
