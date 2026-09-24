"""Справочники читаются, ссылочно целостны и содержат опоры обязательных сценариев.

Опоры проверяются явно: если справочник поменяется так, что сценарий
перестанет воспроизводиться, тест должен упасть здесь, а не в сквозной
проверке с непонятной причиной.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
import yaml

from data.loader.reference import load_reference

RULES_YAML = Path(__file__).resolve().parents[2] / "data" / "reference" / "rules.yaml"


@pytest.fixture(scope="module")
def ref():
    return load_reference()


@pytest.fixture(scope="module")
def rules():
    return yaml.safe_load(RULES_YAML.read_text(encoding="utf-8"))


def test_six_territories(ref):
    assert set(ref.territories) == {"TER-MSK", "TER-SPB", "TER-NVR", "TER-YAR", "TER-VRN", "TER-TVE"}


def test_regions_without_own_workshop_have_only_external_centers(ref):
    for tid in ("TER-YAR", "TER-VRN", "TER-TVE"):
        centers = [c for c in ref.service_centers.values() if c.territory_id == tid]
        assert centers, tid
        assert not any(c.is_internal for c in centers), tid


def test_spb_hp_warranty_case_is_reproducible(ref):
    eq = ref.equipment["EQ-0011"]
    assert ref.models[eq.model_id].manufacturer == "HP"
    assert ref.partners[eq.partner_id].territory_id == "TER-SPB"
    # Гарантия по W-01: 12 месяцев от даты продажи.
    assert eq.sale_date + dt.timedelta(days=365) > dt.date(2026, 9, 14)

    spb = [c for c in ref.service_centers.values() if c.territory_id == "TER-SPB"]
    own = [c for c in spb if c.is_internal]
    ext = [c for c in spb if not c.is_internal]
    assert own and all("HP" not in c.brands for c in own)
    assert any("HP" in c.brands for c in ext)

    free_l3 = [t for t in ref.technicians.values()
               if t.base_territory_id == "TER-SPB" and "SK-03" in t.skill_ids and t.busy_until is None]
    assert free_l3, "нужен свободный собственный инженер L3 в Петербурге"


def test_moscow_dispatcher_has_no_access_to_spb(ref):
    assert ref.users["U-001"].role == "Диспетчер"
    msk = {a.territory_id for a in ref.territory_access if a.user_id == "U-001"}
    assert msk == {"TER-MSK"}
    spb = {a.territory_id for a in ref.territory_access if a.user_id == "U-002"}
    assert spb == {"TER-SPB"}


def test_rules_yaml_matches_reference(ref, rules):
    ids = [r["id"] for r in rules["rules"]]
    assert len(ids) == len(set(ids))
    mvp = {rid for rid, r in ref.decision_rules.items() if r.in_mvp}
    assert set(ids) == mvp, "в rules.yaml должны быть ровно правила области MVP"
    for r in rules["rules"]:
        assert r["category"] in {"safety", "deterministic", "requires_evaluation"}
        if "required_skill" in r:
            assert r["required_skill"] in ref.skills
        for t in r["triggers"]:
            assert t["level"] in {"safety", "model_code", "symptom"}
            if "symptom" in t:
                assert t["symptom"] in rules["symptom_tags"]
            if "manufacturer" in t:
                assert t["manufacturer"] in {m.manufacturer for m in ref.models.values()}
            if "code_pattern" in t:
                re.compile(t["code_pattern"])


def test_r02_is_deterministic_onsite_with_verbatim_actions(ref, rules):
    r02 = ref.decision_rules["R-02"]
    assert r02.route == "onsite"
    assert r02.prescribed_actions.startswith("Выключить и дать остыть. Не выполнять многократные сбросы.")
    assert next(r for r in rules["rules"] if r["id"] == "R-02")["category"] == "deterministic"


def test_every_model_type_has_warranty_term(ref, rules):
    types = {m.equipment_type for m in ref.models.values()}
    assert types <= set(rules["warranty_months_by_type"])
