"""Мок учётной системы: разграничение доступа, аудит, идемпотентность.

Запускается против развёрнутого мока и его базы:
    MOCK_ERP_URL, MOCK_ERP_TOKEN_FILE,
    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD_FILE, MOCK_ERP_DB.
Перед прогоном база мока заполняется заново: python -m app.seed --reset.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest

URL = os.environ.get("MOCK_ERP_URL", "http://mock-erp:8100")
SPB_HP_SERIAL = "HPL-M4103-77842"   # EQ-0011, партнёр CUST-008, Санкт-Петербург


def _secret(name: str) -> str:
    return Path(os.environ[f"{name}_FILE"]).read_text(encoding="utf-8").strip()


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=URL, headers={"Authorization": f"Bearer {_secret('MOCK_ERP_TOKEN')}"},
                      timeout=10) as c:
        yield c


@pytest.fixture(scope="module")
def db():
    with psycopg.connect(host=os.environ.get("POSTGRES_HOST", "postgres"),
                         port=int(os.environ.get("POSTGRES_PORT", "5432")),
                         user=os.environ["POSTGRES_USER"], password=_secret("POSTGRES_PASSWORD"),
                         dbname=os.environ.get("MOCK_ERP_DB", "mock_erp"), autocommit=True) as conn:
        yield conn


def ctx(user: str, *territories: str) -> dict:
    return {"X-Auth-User": user, "X-Auth-Territories": ",".join(territories)}


def test_health_is_open():
    assert httpx.get(f"{URL}/health", timeout=10).json() == {"status": "ok"}


def test_service_token_required():
    r = httpx.get(f"{URL}/v1/equipment/by-serial/{SPB_HP_SERIAL}", headers=ctx("U-002", "TER-SPB"), timeout=10)
    assert r.status_code == 401


def test_auth_context_required(client):
    assert client.get(f"/v1/equipment/by-serial/{SPB_HP_SERIAL}").status_code == 400


def test_spb_dispatcher_gets_spb_equipment(client):
    r = client.get(f"/v1/equipment/by-serial/{SPB_HP_SERIAL}", headers=ctx("U-002", "TER-SPB"))
    assert r.status_code == 200
    card = r.json()
    assert card["equipment_id"] == "EQ-0011"
    assert card["territory_id"] == "TER-SPB"
    assert card["model"]["manufacturer"] == "HP"


def test_moscow_dispatcher_denied_and_audited(client, db):
    before = db.execute("SELECT count(*) FROM audit_event WHERE event_type = 'access_denied'"
                        " AND user_id = 'U-001' AND object_id = 'EQ-0011'").fetchone()[0]
    r = client.get(f"/v1/equipment/by-serial/{SPB_HP_SERIAL}", headers=ctx("U-001", "TER-MSK"))
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "access_denied"
    after = db.execute("SELECT count(*) FROM audit_event WHERE event_type = 'access_denied'"
                       " AND user_id = 'U-001' AND object_id = 'EQ-0011'").fetchone()[0]
    assert after == before + 1


def test_claimed_territory_outside_registry_is_ignored(client):
    # Диспетчер Москвы присылает чужую территорию: действует пересечение с реестром.
    r = client.get(f"/v1/equipment/by-serial/{SPB_HP_SERIAL}", headers=ctx("U-001", "TER-MSK", "TER-SPB"))
    assert r.status_code == 403


def test_partner_representative_sees_only_own_partner(client):
    assert client.get("/v1/equipment/EQ-0011", headers=ctx("U-019", "TER-SPB")).status_code == 200
    assert client.get("/v1/equipment/EQ-0011", headers=ctx("U-020", "TER-SPB")).status_code == 403


def test_unknown_user_denied(client):
    assert client.get("/v1/equipment/EQ-0011", headers=ctx("U-999", "TER-SPB")).status_code == 403


def test_service_centers_by_territory(client):
    centers = {c["center_id"]: c for c in client.get(
        "/v1/service-centers", params={"territory_id": "TER-SPB"}, headers=ctx("U-002", "TER-SPB")).json()}
    assert centers["SC-005"]["is_internal"] and "HP" not in centers["SC-005"]["brands"]
    assert not centers["SC-006"]["is_internal"] and "HP" in centers["SC-006"]["brands"]
    r = client.get("/v1/service-centers", params={"territory_id": "TER-SPB"}, headers=ctx("U-001", "TER-MSK"))
    assert r.status_code == 403


def _draft(key: str, **overrides) -> dict:
    body = {
        "idempotency_key": key, "equipment_id": "EQ-0011", "execution_mode": "onsite",
        "fulfillment_type": "external", "service_center_id": "SC-006", "technician_id": None,
        "warranty_preliminary": "warranty", "decision_ref": f"test-{key}",
        "facts_snapshot": {"error_code": "50.2"},
    }
    body.update(overrides)
    return body


def test_draft_is_idempotent(client, db):
    key = f"test-{uuid.uuid4()}"
    first = client.post("/v1/work-order-drafts", json=_draft(key), headers=ctx("U-002", "TER-SPB"))
    assert first.status_code == 201 and first.json()["replayed"] is False
    second = client.post("/v1/work-order-drafts", json=_draft(key), headers=ctx("U-002", "TER-SPB"))
    assert second.status_code == 200 and second.json()["replayed"] is True
    assert second.json()["document_id"] == first.json()["document_id"]
    assert db.execute("SELECT count(*) FROM work_order_draft WHERE idempotency_key = %s", (key,)).fetchone()[0] == 1


def test_same_key_with_other_content_conflicts(client):
    key = f"test-{uuid.uuid4()}"
    assert client.post("/v1/work-order-drafts", json=_draft(key), headers=ctx("U-002", "TER-SPB")).status_code == 201
    r = client.post("/v1/work-order-drafts", json=_draft(key, execution_mode="remote"),
                    headers=ctx("U-002", "TER-SPB"))
    assert r.status_code == 409


def test_partner_representative_cannot_create_drafts(client):
    r = client.post("/v1/work-order-drafts", json=_draft(f"test-{uuid.uuid4()}"), headers=ctx("U-019", "TER-SPB"))
    assert r.status_code == 403


def test_moscow_dispatcher_cannot_create_draft_for_spb_equipment(client):
    r = client.post("/v1/work-order-drafts", json=_draft(f"test-{uuid.uuid4()}"), headers=ctx("U-001", "TER-MSK"))
    assert r.status_code == 403
