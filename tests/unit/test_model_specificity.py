"""Регрессии границы модель + код и доказуемой специфичности условий."""
from dataclasses import replace

import pytest
import yaml

from backend.rules.catalog import CatalogError, Trigger, load_catalog, RULES_YAML
from backend.rules.engine import Facts, RuleEngine


def test_c3100_does_not_belong_to_p2040():
    engine = RuleEngine(load_catalog())
    result = engine.evaluate(Facts("Kyocera", "C3100", model_id="MOD-005"))
    assert result.kind == "human" and result.rule is None
    assert not any(m.rule_id == "R-09" for m in result.considered)
    positive = engine.evaluate(Facts("Kyocera", "C3100", model_id="MOD-002"))
    assert positive.rule.rule_id == "R-09" and positive.kind == "needs_evaluation"


@pytest.mark.parametrize("model_id", [None, "MOD-005", "MOD-999"])
def test_manufacturer_is_not_a_model(model_id):
    result = RuleEngine(load_catalog()).evaluate(Facts("HP", "50.2", model_id=model_id))
    assert result.kind == "human"


def test_wrong_model_can_use_general_symptom():
    result = RuleEngine(load_catalog()).evaluate(
        Facts("Kyocera", "C3100", frozenset({"paper_jam_single"}), model_id="MOD-005"))
    assert result.rule.rule_id == "R-03" and result.level == 4


def test_narrow_model_set_beats_broad_one_despite_urgency():
    catalog = load_catalog()
    broad = Trigger(2, manufacturer="Kyocera", code_pattern="^C6000$",
                    model_ids=frozenset({"MOD-002", "MOD-005"}))
    narrow = replace(broad, model_ids=frozenset({"MOD-005"}))
    rules = dict(catalog.rules)
    rules["R-02"] = replace(rules["R-02"], triggers=(broad,), urgency_hint=1)
    rules["R-08"] = replace(rules["R-08"], triggers=(narrow,), urgency_hint=3)
    result = RuleEngine(replace(catalog, rules=rules)).evaluate(
        Facts("Kyocera", "C6000", model_id="MOD-005"))
    assert result.rule.rule_id == "R-08"
    assert {m.rule_id for m in result.considered} == {"R-02", "R-08"}


def test_range_inclusion_and_incomparable_conditions():
    wide = Trigger(2, manufacturer="Pantum", code_range=(3, 12), model_ids=frozenset({"MOD-001"}))
    assert replace(wide, code_range=(5, 5)).more_specific_than(wide)
    assert not replace(wide, code_range=(11, 13)).more_specific_than(wide)
    # No guessed ordering of remote/onsite/workshop for incomparable rules.
    result = RuleEngine(load_catalog()).evaluate(Facts("Pantum", "11", model_id="MOD-001"))
    assert result.kind == "clarification" and result.rule is None


@pytest.mark.parametrize("models", [[], ["MOD-999"], ["MOD-005"]])
def test_catalog_rejects_missing_or_foreign_models(tmp_path, models):
    spec = yaml.safe_load(RULES_YAML.read_text(encoding="utf-8"))
    spec["rules"][1]["triggers"][0]["model_ids"] = models
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    with pytest.raises(CatalogError):
        load_catalog(yaml_path=path)
