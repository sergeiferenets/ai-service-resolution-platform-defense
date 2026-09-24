"""Порядок применения правил по ADR-0004 и дословность предписаний."""

from __future__ import annotations

import dataclasses

import pytest

from backend.rules.catalog import load_catalog
from backend.rules.engine import Facts, RuleEngine
from data.loader.reference import load_reference


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


@pytest.fixture(scope="module")
def engine(catalog):
    return RuleEngine(catalog)


def test_catalog_covers_mvp_rules(catalog):
    assert len(catalog.rules) == 15
    assert catalog.safety_rule.rule_id == "R-01"
    assert catalog.version.startswith("rules-1-")


def test_hp_502_is_r02_deterministic_onsite(engine):
    out = engine.evaluate(Facts("HP", "50.2", model_id="MOD-003"))
    assert (out.kind, out.level, out.rule.rule_id, out.rule.route) == ("applied", 2, "R-02", "onsite")


def test_prescribed_actions_are_verbatim(engine):
    reference = load_reference().decision_rules["R-02"].prescribed_actions
    assert engine.evaluate(Facts("HP", "50.2", model_id="MOD-003")).prescribed_actions == reference


def test_code_beats_symptom_and_symptom_is_kept_as_feature(engine):
    out = engine.evaluate(Facts("HP", "50.2", frozenset({"print_defects"}), model_id="MOD-003"))
    assert out.rule.rule_id == "R-02" and out.level == 2
    assert any(m.rule_id == "R-10" and m.level == 4 for m in out.considered)


def test_urgency_hint_does_not_affect_order(catalog):
    # Меняем urgency_hint местами: исход тот же — порядок задаёт тип условия.
    rules = dict(catalog.rules)
    rules["R-02"] = dataclasses.replace(rules["R-02"], urgency_hint=3)
    rules["R-10"] = dataclasses.replace(rules["R-10"], urgency_hint=1)
    swapped = RuleEngine(dataclasses.replace(catalog, rules=rules))
    assert swapped.evaluate(Facts("HP", "50.2", frozenset({"print_defects"}), model_id="MOD-003")).rule.rule_id == "R-02"


def test_safety_keyword_overrides_code(engine):
    out = engine.evaluate(Facts("HP", "50.2", text="Ошибка 50.2, из принтера идёт дым", model_id="MOD-003"))
    assert (out.kind, out.level, out.rule.rule_id) == ("safety_escalation", 1, "R-01")


def test_danger_symptom_overrides_code(engine):
    assert engine.evaluate(Facts("HP", "50.2", frozenset({"danger"}), model_id="MOD-003")).kind == "safety_escalation"


def test_equal_candidates_lead_to_clarification(engine):
    # Pantum Internal Error 11 — в диапазоне R-02 (03–12) и R-09 (11–13).
    out = engine.evaluate(Facts("Pantum", "Internal Error 11", model_id="MOD-001"))
    assert (out.kind, out.level, out.rule) == ("clarification", 3, None)
    assert {m.rule_id for m in out.considered} == {"R-02", "R-09"}


def test_prefix_normalization(engine):
    assert engine.evaluate(Facts("Pantum", "Internal Error 05", model_id="MOD-001")).rule.rule_id == "R-02"
    assert engine.evaluate(Facts("Kyocera", "Call service C6000", model_id="MOD-002")).rule.rule_id == "R-02"


def test_requires_evaluation_goes_to_policy(engine):
    assert engine.evaluate(Facts("HP", "49.4C.02", model_id="MOD-003")).kind == "needs_evaluation"
    # R-07: справочник говорит «детерминированное», ADR-0004 — «требует оценки». ADR первичен.
    out = engine.evaluate(Facts("Canon", "#801", model_id="MOD-004"))
    assert (out.kind, out.rule.rule_id) == ("needs_evaluation", "R-07")


def test_code_of_other_manufacturer_does_not_match(engine):
    # Ложное сопоставление: код Kyocera на аппарате HP.
    assert engine.evaluate(Facts("HP", "C6000", model_id="MOD-003")).kind == "human"


def test_symptom_level(engine):
    out = engine.evaluate(Facts(None, None, frozenset({"paper_jam_single"})))
    assert (out.kind, out.level, out.rule.rule_id, out.rule.route) == ("applied", 4, "R-03", "remote")
    both = engine.evaluate(Facts(None, None, frozenset({"paper_jam_single", "paper_jam_repeated"})))
    assert both.kind == "clarification"


def test_no_rule_means_human(engine):
    out = engine.evaluate(Facts("HP", "99.99", model_id="MOD-003"))
    assert (out.kind, out.level, out.rule) == ("human", 5, None)
