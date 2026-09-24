"""Мок учётной системы: минимальный набор эндпоинтов для сквозного сценария.

Каждый эндпоинт /v1 требует сервисный токен и контекст авторизации.
Объекты партнёров проверяются по территории (и по партнёру для
представителя партнёра); справочные данные организации — центры и
исполнители — по явно указанной территории. Запчасти и цены изделий —
справочник организации без территориальной привязки, но контекст
авторизации обязателен и там: неизвестный пользователь получает отказ.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from contextlib import asynccontextmanager
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from app.auth import (ROLES_CREATING_DRAFTS, Caller, audit, check_object, check_territory, deny,
                      get_caller, require_service_token)
from app.config import load_settings
from app.db import ensure_database, make_pool, migrate


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    ensure_database(settings)
    migrate(settings)
    app.state.settings = settings
    app.state.pool = make_pool(settings)
    try:
        yield
    finally:
        app.state.pool.close()


app = FastAPI(title="Мок учётной системы", version="1", lifespan=lifespan)
v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_service_token)])


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def rows(request: Request, query: str, params: tuple = ()) -> list[dict]:
    with request.app.state.pool.connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(query, params).fetchall()


def one(request: Request, query: str, params: tuple, not_found: str) -> dict:
    found = rows(request, query, params)
    if not found:
        raise HTTPException(status_code=404, detail=not_found)
    return found[0]


EQUIPMENT_CARD = """
    SELECT e.equipment_id, e.serial_number, e.sale_date, e.location, e.status, e.assigned_technician_id,
           e.partner_id, p.territory_id,
           m.model_id, m.manufacturer, m.name AS model_name, m.equipment_type
      FROM equipment e
      JOIN partner p USING (partner_id)
      JOIN model m USING (model_id)
"""


def equipment_card(row: dict) -> dict:
    return {
        "equipment_id": row["equipment_id"],
        "serial_number": row["serial_number"],
        "model": {"model_id": row["model_id"], "manufacturer": row["manufacturer"],
                  "name": row["model_name"], "equipment_type": row["equipment_type"]},
        "partner_id": row["partner_id"],
        "territory_id": row["territory_id"],
        "sale_date": row["sale_date"],
        "location": row["location"],
        "status": row["status"],
        "assigned_technician_id": row["assigned_technician_id"],
    }


def load_equipment(request: Request, caller: Caller, where: str, value: str) -> dict:
    row = one(request, EQUIPMENT_CARD + f" WHERE {where} = %s", (value,), "Оборудование не найдено")
    check_object(request, caller, object_type="equipment", object_id=row["equipment_id"],
                 territory_id=row["territory_id"], partner_id=row["partner_id"])
    return row


@v1.get("/equipment/by-serial/{serial}")
def equipment_by_serial(serial: str, request: Request, caller: Caller = Depends(get_caller)) -> dict:
    return equipment_card(load_equipment(request, caller, "e.serial_number", serial))


@v1.get("/equipment/{equipment_id}")
def equipment_by_id(equipment_id: str, request: Request, caller: Caller = Depends(get_caller)) -> dict:
    return equipment_card(load_equipment(request, caller, "e.equipment_id", equipment_id))


@v1.get("/equipment/{equipment_id}/service-history")
def service_history(equipment_id: str, request: Request, caller: Caller = Depends(get_caller)) -> list[dict]:
    load_equipment(request, caller, "e.equipment_id", equipment_id)
    return rows(request,
                "SELECT service_order_id, date, symptom, error_code, diagnosis, work_done, part_id,"
                " technician_id, billing, labor_hours FROM service_history"
                " WHERE equipment_id = %s ORDER BY date DESC", (equipment_id,))


def load_partner(request: Request, caller: Caller, partner_id: str) -> dict:
    row = one(request, "SELECT * FROM partner WHERE partner_id = %s", (partner_id,), "Партнёр не найден")
    check_object(request, caller, object_type="partner", object_id=partner_id,
                 territory_id=row["territory_id"], partner_id=partner_id)
    return row


@v1.get("/partners/{partner_id}")
def partner(partner_id: str, request: Request, caller: Caller = Depends(get_caller)) -> dict:
    return load_partner(request, caller, partner_id)


@v1.get("/partners/{partner_id}/contracts")
def contracts(partner_id: str, request: Request, active_on: dt.date | None = None,
              caller: Caller = Depends(get_caller)) -> list[dict]:
    load_partner(request, caller, partner_id)
    query = "SELECT * FROM contract WHERE partner_id = %s"
    params: tuple = (partner_id,)
    if active_on is not None:
        query += " AND valid_from <= %s AND valid_to >= %s"
        params += (active_on, active_on)
    return rows(request, query + " ORDER BY contract_id", params)


def model_tokens(designation: str) -> list[str]:
    tokens = "".join(c if c.isalnum() else " " for c in designation.lower()).split()
    with_digits = [t for t in tokens if len(t) >= 2 and any(c.isdigit() for c in t)]
    return with_digits or [t for t in tokens if len(t) >= 3]


@v1.get("/partners/{partner_id}/equipment")
def partner_equipment(partner_id: str, request: Request, model: str | None = None,
                      caller: Caller = Depends(get_caller)) -> list[dict]:
    """Оборудование партнёра; с параметром model — по обозначению модели.

    Сопоставление — как у диспетчера: все значимые части обозначения (с
    цифрами, а при их отсутствии — слова от трёх букв) входят в производителя
    и наименование модели. «M4103dw» и «HP LaserJet M4103» находят
    «HP LaserJet Pro M4103dw». Пустой список — факт, а не отказ; отказ —
    для партнёра вне зоны ответственности.
    """
    load_partner(request, caller, partner_id)
    found = [equipment_card(r) for r in rows(
        request, EQUIPMENT_CARD + " WHERE e.partner_id = %s ORDER BY e.equipment_id", (partner_id,))]
    if model is not None:
        tokens = model_tokens(model)
        found = [e for e in found if tokens and all(
            t in f"{e['model']['manufacturer']} {e['model']['name']}".lower() for t in tokens)]
    return found


@v1.get("/service-centers")
def service_centers(territory_id: str, request: Request, caller: Caller = Depends(get_caller)) -> list[dict]:
    check_territory(request, caller, territory_id, "сервисные центры")
    return rows(request, """
        SELECT c.center_id, c.name, c.is_internal, c.service_org_id, c.supplier_id, c.territory_id, c.address,
               COALESCE((SELECT array_agg(brand ORDER BY brand) FROM service_center_brand b
                          WHERE b.center_id = c.center_id), '{}') AS brands,
               COALESCE((SELECT array_agg(skill_id ORDER BY skill_id) FROM service_center_skill s
                          WHERE s.center_id = c.center_id), '{}') AS skill_ids
          FROM service_center c WHERE c.territory_id = %s ORDER BY c.center_id""", (territory_id,))


@v1.get("/technicians")
def technicians(territory_id: str, request: Request, caller: Caller = Depends(get_caller)) -> list[dict]:
    check_territory(request, caller, territory_id, "исполнители")
    return rows(request, """
        SELECT t.technician_id, t.full_name, t.service_org_id, t.territory_id, t.schedule, t.busy_until,
               COALESCE((SELECT array_agg(skill_id ORDER BY skill_id) FROM technician_skill s
                          WHERE s.technician_id = t.technician_id), '{}') AS skill_ids
          FROM technician t WHERE t.territory_id = %s ORDER BY t.technician_id""", (territory_id,))


@v1.get("/parts")
def parts(model_id: str, request: Request, caller: Caller = Depends(get_caller)) -> list[dict]:
    return rows(request, """
        SELECT p.part_id, p.name, p.stock, p.warehouse_territory_id, p.price_rub, p.lead_time_days
          FROM part p JOIN part_model pm USING (part_id)
         WHERE pm.model_id = %s ORDER BY p.part_id""", (model_id,))


@v1.get("/models/{model_id}/price")
def product_price(model_id: str, request: Request, caller: Caller = Depends(get_caller)) -> dict:
    return one(request, "SELECT model_id, price_rub FROM model WHERE model_id = %s", (model_id,),
               "Модель не найдена")


class DraftRequest(BaseModel):
    idempotency_key: str = Field(min_length=8)
    equipment_id: str
    execution_mode: Literal["remote", "onsite", "workshop"]
    fulfillment_type: Literal["internal", "external"]
    service_center_id: str | None = None
    technician_id: str | None = None
    warranty_preliminary: Literal["warranty", "paid"]
    decision_ref: str
    facts_snapshot: dict


DRAFT_COLUMNS = ("document_id, idempotency_key, equipment_id, partner_id, execution_mode, fulfillment_type,"
                 " service_center_id, technician_id, warranty_preliminary, decision_ref, facts_snapshot,"
                 " created_by, created_at")


@v1.post("/work-order-drafts", status_code=201)
def create_draft(body: DraftRequest, request: Request, caller: Caller = Depends(get_caller)):
    if caller.role not in ROLES_CREATING_DRAFTS:
        deny(request, caller, reason=f"роль «{caller.role}» не создаёт документы",
             object_type="work_order_draft", object_id=body.idempotency_key)
    eq = load_equipment(request, caller, "e.equipment_id", body.equipment_id)
    request_hash = hashlib.sha256(
        json.dumps(body.model_dump(), sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()

    def existing(conn) -> dict | None:
        return conn.cursor(row_factory=dict_row).execute(
            f"SELECT {DRAFT_COLUMNS}, request_hash FROM work_order_draft WHERE idempotency_key = %s",
            (body.idempotency_key,)).fetchone()

    def replay(found: dict) -> JSONResponse:
        if found.pop("request_hash") != request_hash:
            raise HTTPException(status_code=409, detail="Ключ идемпотентности уже использован с другим содержимым")
        audit(request, event_type="draft_replayed", caller=caller, object_type="work_order_draft",
              object_id=found["document_id"], object_territory_id=eq["territory_id"])
        return JSONResponse(status_code=200, content=json.loads(json.dumps({**found, "replayed": True}, default=str)))

    with request.app.state.pool.connection() as conn:
        found = existing(conn)
    if found:
        return replay(found)

    try:
        with request.app.state.pool.connection() as conn:
            created = conn.cursor(row_factory=dict_row).execute(
                f"""INSERT INTO work_order_draft (document_id, idempotency_key, request_hash, equipment_id,
                        partner_id, execution_mode, fulfillment_type, service_center_id, technician_id,
                        warranty_preliminary, decision_ref, facts_snapshot, created_by)
                    VALUES ('WO-' || lpad(nextval('work_order_draft_seq')::text, 6, '0'),
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {DRAFT_COLUMNS}""",
                (body.idempotency_key, request_hash, body.equipment_id, eq["partner_id"], body.execution_mode,
                 body.fulfillment_type, body.service_center_id, body.technician_id, body.warranty_preliminary,
                 body.decision_ref, json.dumps(body.facts_snapshot, ensure_ascii=False, default=str),
                 caller.user_id)).fetchone()
    except psycopg.errors.UniqueViolation:
        # Параллельный запрос с тем же ключом успел раньше: возвращаем его документ.
        with request.app.state.pool.connection() as conn:
            return replay(existing(conn))

    audit(request, event_type="draft_created", caller=caller, object_type="work_order_draft",
          object_id=created["document_id"], object_territory_id=eq["territory_id"])
    return {**created, "replayed": False}


@v1.get("/work-order-drafts/{document_id}")
def get_draft(document_id: str, request: Request, caller: Caller = Depends(get_caller)) -> dict:
    draft = one(request, f"SELECT {DRAFT_COLUMNS} FROM work_order_draft WHERE document_id = %s",
                (document_id,), "Документ не найден")
    load_equipment(request, caller, "e.equipment_id", draft["equipment_id"])
    return draft


app.include_router(v1)

# Второй HTTP-контракт над теми же данными и проверками.
from app.odata import router as integration_router
app.include_router(integration_router)
