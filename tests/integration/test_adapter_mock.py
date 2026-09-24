"""Адаптер против настоящего мока: соответствие реальных ответов доменной модели.

Окружение то же, что у test_mock_erp.py. Перед прогоном база мока
заполняется заново: python -m app.seed --reset.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from pathlib import Path

import pytest

from backend.adapters.cache import RequestScopedCache
from backend.adapters.mock_erp import MockErpAdapter
from backend.domain.errors import AccessDenied
from backend.domain.models import AuthContext, DraftRequest

SPB = AuthContext(user_id="U-002", role="Диспетчер", territories=frozenset({"TER-SPB"}))
MSK = AuthContext(user_id="U-001", role="Диспетчер", territories=frozenset({"TER-MSK"}))


@pytest.fixture(scope="module")
def erp():
    token = Path(os.environ["MOCK_ERP_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    return MockErpAdapter(os.environ.get("MOCK_ERP_URL", "http://mock-erp:8100"), token)


def test_equipment_and_partner(erp):
    eq = erp.find_equipment_by_serial(SPB, "HPL-M4103-77842")
    assert (eq.equipment_id, eq.model.manufacturer, eq.territory_id) == ("EQ-0011", "HP", "TER-SPB")
    assert eq.sale_date == dt.date(2026, 2, 12)
    assert erp.get_partner(SPB, eq.partner_id).territory_id == "TER-SPB"


def test_denial_is_raised_not_empty(erp):
    with pytest.raises(AccessDenied):
        erp.find_equipment_by_serial(MSK, "HPL-M4103-77842")


def test_centers_technicians_parts_price(erp):
    centers = {c.center_id: c for c in erp.list_service_centers(SPB, "TER-SPB")}
    assert "HP" not in centers["SC-005"].brands and centers["SC-005"].is_internal
    assert "HP" in centers["SC-006"].brands and not centers["SC-006"].is_internal
    tech = {t.technician_id: t for t in erp.list_technicians(SPB, "TER-SPB")}
    assert "SK-03" in tech["TECH-013"].skill_ids and tech["TECH-013"].busy_until is None
    assert any(p.part_id == "SP-014" for p in erp.list_parts(SPB, "MOD-003"))
    assert erp.product_price(SPB, "MOD-003") > 0


def test_contracts_filtered_by_date(erp):
    # У петербургского партнёра договора нет: пустой список — это факт, а не отказ.
    assert erp.list_contracts(SPB, "CUST-008", dt.date(2026, 9, 14)) == []


def test_draft_through_adapter_is_idempotent(erp):
    key = f"it-{uuid.uuid4()}"
    draft = DraftRequest(key, "EQ-0011", "onsite", "external", "SC-006", None, "warranty", key,
                         {"error_code": "50.2"})
    first = erp.create_draft(SPB, draft)
    second = erp.create_draft(SPB, draft)
    assert first.document_id == second.document_id
    assert (first.replayed, second.replayed) == (False, True)


REP_SPB = AuthContext(user_id="U-019", role="Клиент", territories=frozenset({"TER-SPB"}), partner_id="CUST-008")


def test_partner_equipment_by_model_designation(erp):
    assert [e.equipment_id for e in erp.find_partner_equipment(REP_SPB, "CUST-008", "HP LaserJet M4103")] == ["EQ-0011"]
    assert [e.equipment_id for e in erp.find_partner_equipment(SPB, "CUST-009", "Canon MF463dw")] == ["EQ-0012", "EQ-0013"]
    assert erp.find_partner_equipment(REP_SPB, "CUST-008", "Kyocera M2135dn") == []
    assert [e.equipment_id for e in erp.find_partner_equipment(REP_SPB, "CUST-008", None)] == ["EQ-0011"]


def test_partner_equipment_of_foreign_partner_is_denied(erp):
    with pytest.raises(AccessDenied):
        erp.find_partner_equipment(REP_SPB, "CUST-009", None)      # представитель чужого партнёра
    with pytest.raises(AccessDenied):
        erp.find_partner_equipment(MSK, "CUST-008", "M4103dw")      # другая территория


def test_cache_over_real_source(erp):
    cache = RequestScopedCache(erp)
    cache.get_equipment(SPB, "EQ-0011")
    cache.get_equipment(SPB, "EQ-0011")
    assert (cache.misses, cache.hits) == (1, 1)
