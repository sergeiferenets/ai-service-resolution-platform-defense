"""Срочность, гарантия, тип исполнения и кандидаты, согласование."""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from backend.domain.models import Equipment, ModelInfo, ServiceCenter, Technician
from backend.rules.catalog import load_catalog
from backend.rules.urgency import compute_urgency
from backend.tools.approval import DISPATCHER, SERVICE_MANAGER, assess_approval
from backend.tools.fulfillment import NO_BRAND, NO_SKILL, REJECTION_REASONS, UNAVAILABLE, decide_fulfillment
from backend.tools.warranty import prequalify
from data.loader.reference import load_reference

TODAY = dt.date(2026, 9, 14)


@pytest.fixture(scope="module")
def ref():
    return load_reference()


@pytest.fixture(scope="module")
def months():
    return load_catalog().warranty_months_by_type


def equipment(ref, equipment_id: str) -> Equipment:
    e = ref.equipment[equipment_id]
    m = ref.models[e.model_id]
    return Equipment(e.equipment_id, e.serial_number, ModelInfo(m.model_id, m.manufacturer, m.name, m.equipment_type),
                     e.partner_id, ref.partners[e.partner_id].territory_id, e.sale_date, e.location, e.status, None)


def centers(ref, territory_id: str) -> list[ServiceCenter]:
    return [ServiceCenter(c.center_id, c.name, c.is_internal, c.service_org_id, c.territory_id,
                          frozenset(c.brands), frozenset(c.skill_ids))
            for c in ref.service_centers.values() if c.territory_id == territory_id]


def technicians(ref, territory_id: str) -> list[Technician]:
    return [Technician(t.technician_id, t.full_name, t.service_org_id, t.base_territory_id,
                       frozenset(t.skill_ids), t.busy_until)
            for t in ref.technicians.values() if t.base_territory_id == territory_id]


# --- срочность -----------------------------------------------------------------------------------

def test_urgency_levels_and_contract_target():
    assert compute_urgency(1, "высокое", None).level == "P1"
    assert compute_urgency(1, "высокое", None).reaction_target_hours == 4
    assert compute_urgency(3, "низкое", None).level == "P4"
    assert compute_urgency(1, None, 24).reaction_target_hours == 24


# --- гарантия ------------------------------------------------------------------------------------

def test_spb_hp_is_preliminarily_warranty(ref, months):
    w = prequalify(equipment(ref, "EQ-0011"), TODAY, months)
    assert (w.status, w.preliminary, w.rule_id) == ("warranty", True, "W-01")
    assert w.valid_until == dt.date(2027, 2, 12)


def test_expired_warranty_is_paid(ref, months):
    assert prequalify(equipment(ref, "EQ-0003"), TODAY, months).status == "paid"


def test_risk_signal_does_not_change_status(ref, months):
    w = prequalify(equipment(ref, "EQ-0011"), TODAY, months, frozenset({"падение"}))
    assert w.status == "warranty" and w.risk_flags == ("падение",)


def test_unknown_equipment_type_is_an_error(ref):
    with pytest.raises(ValueError):
        prequalify(equipment(ref, "EQ-0011"), TODAY, {})


# --- исполнение и кандидаты -------------------------------------------------------------------------

def test_spb_hp_warranty_goes_external_despite_free_own_engineer(ref):
    d = decide_fulfillment(route="onsite", warranty=True, brand="HP", territory_id="TER-SPB",
                           required_skill="SK-03", centers=centers(ref, "TER-SPB"),
                           technicians=technicians(ref, "TER-SPB"), window_start=TODAY, part_available=True)
    assert (d.fulfillment_type, d.service_center_id, d.technician_id) == ("external", "SC-006", None)
    by_id = {c.candidate_id: c for c in d.candidates}
    assert by_id["SC-005"].rejection_reason == NO_BRAND and by_id["SC-005"].candidate_type == "сервисный центр"
    tech13 = by_id["TECH-013"]
    assert tech13.candidate_type == "исполнитель" and tech13.rejection_reason == NO_BRAND
    assert by_id["SC-006"].rejection_reason is None and by_id["SC-006"].rank == 1
    assert all(c.rejection_reason in REJECTION_REASONS for c in d.candidates if c.rejection_reason)


def test_yaroslavl_warranty_goes_to_external_center(ref):
    d = decide_fulfillment(route="onsite", warranty=True, brand="HP", territory_id="TER-YAR",
                           required_skill="SK-02", centers=centers(ref, "TER-YAR"),
                           technicians=technicians(ref, "TER-YAR"), window_start=TODAY)
    assert (d.fulfillment_type, d.service_center_id) == ("external", "SC-101")


def test_yaroslavl_paid_onsite_within_competence_stays_internal(ref):
    d = decide_fulfillment(route="onsite", warranty=False, brand="HP", territory_id="TER-YAR",
                           required_skill="SK-02", centers=centers(ref, "TER-YAR"),
                           technicians=technicians(ref, "TER-YAR"), window_start=TODAY)
    assert (d.fulfillment_type, d.technician_id) == ("internal", "TECH-009")


def test_missing_skill_and_busy_engineer_are_coded(ref):
    techs = technicians(ref, "TER-SPB")
    busy = [dataclasses.replace(t, busy_until=TODAY + dt.timedelta(days=3)) if t.technician_id == "TECH-013" else t
            for t in techs]
    d = decide_fulfillment(route="onsite", warranty=False, brand="HP", territory_id="TER-SPB",
                           required_skill="SK-03", centers=centers(ref, "TER-SPB"), technicians=busy,
                           window_start=TODAY)
    by_id = {c.candidate_id: c for c in d.candidates}
    assert by_id["TECH-013"].rejection_reason == UNAVAILABLE
    assert by_id["TECH-014"].rejection_reason == NO_SKILL


def test_contract_engineer_has_priority(ref):
    techs = technicians(ref, "TER-MSK")
    d = decide_fulfillment(route="onsite", warranty=False, brand="HP", territory_id="TER-MSK",
                           required_skill="SK-03", centers=centers(ref, "TER-MSK"), technicians=techs,
                           window_start=TODAY, contract_technician_id="TECH-004")
    chosen = next(c for c in d.candidates if c.rank == 1)
    assert (d.technician_id, chosen.selection_basis) == ("TECH-004", "договор")


# --- согласование --------------------------------------------------------------------------------

def test_warranty_repair_below_half_price_needs_no_approval():
    need = assess_approval(warranty=True, estimated_cost=8500, part_price=8500, part_lead_time_days=5,
                           product_price=43600)
    assert need.level is None and not need.requires_separate_approval


def test_warranty_repair_above_half_price_needs_service_manager():
    need = assess_approval(warranty=True, estimated_cost=30000, part_price=None, part_lead_time_days=None,
                           product_price=43600)
    assert (need.level, need.rule_ids) == (SERVICE_MANAGER, ("A-03",))


def test_paid_repair_is_confirmed_by_dispatcher():
    need = assess_approval(warranty=False, estimated_cost=3000, part_price=3000, part_lead_time_days=3,
                           product_price=43600)
    assert need.level == DISPATCHER and "A-08" in need.rule_ids
