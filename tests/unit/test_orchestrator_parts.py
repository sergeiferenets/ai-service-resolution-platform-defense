"""Части оркестратора, проверяемые без базы: заглушки, версия артефактов,
запись вызовов, выходной контроль, снимки, маскировка в журналах."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from backend.agents.stubs import STUB_REVISION, stub_agents
from backend.domain.errors import AccessDenied, SourceUnavailable
from backend.domain.models import AuthContext, Partner
from backend.guardrails.input.check import check_input, load_rules, redactor
from backend.orchestrator.artifacts import build_artifacts, composite_rules_version, slice_artifacts
from backend.orchestrator.machine import output_problems
from backend.orchestrator.recording import RecordingErp
from backend.orchestrator.serialize import to_jsonable
from backend.rules.catalog import load_catalog
from backend.tools.fulfillment import FulfillmentDecision

CTX = AuthContext("U-002", "Диспетчер", frozenset({"TER-SPB"}))


def test_every_stub_is_explicitly_marked():
    agents = stub_agents()
    assert all(agent.IS_STUB is True for agent in (agents.context, agents.knowledge, agents.policy))
    assert type(agents.knowledge).__name__.endswith("Stub") and type(agents.policy).__name__.endswith("Stub")


def test_artifact_version_carries_models_prompts_and_rules():
    a = build_artifacts(code_commit="abc", rules_version="rules-x", llm_model_revision="a" * 40,
                        vision_model_revision="b" * 40,
                        prompt_versions={"context": "context-1-aaa", "knowledge": STUB_REVISION})
    assert a.prompt_version == f"context:context-1-aaa; knowledge:{STUB_REVISION}"
    changed = build_artifacts(code_commit="abc", rules_version="rules-x", llm_model_revision="a" * 40,
                              vision_model_revision="b" * 40,
                              prompt_versions={"context": "context-1-bbb", "knowledge": STUB_REVISION})
    assert a.id != changed.id
    assert slice_artifacts("abc", "rules-x").llm_model_revision == STUB_REVISION


def test_rules_version_changes_with_any_part():
    base = composite_rules_version("rules-1-aaa", "codes", "guard")
    assert base.startswith("rules-")
    assert base != composite_rules_version("rules-1-aaa", "codes2", "guard")
    assert base != composite_rules_version("rules-1-aaa", "codes", "guard2")


def test_liquid_safety_keywords_carry_warranty_risk():
    risk = load_catalog().safety_warranty_risk
    assert risk == {"жидкост": "жидкость", "залит": "жидкость", "протечк": "жидкость"}


def test_output_control_catches_unknown_entities():
    catalog = load_catalog()
    fake = FulfillmentDecision("external", "SC-999", None, (), "")
    problems = output_problems(catalog=catalog, rule_id="R-99", route="teleport", fulfillment=fake, centers=[],
                               technicians=[], suspected_part_ids=("SP-000",), parts=[])
    assert len(problems) == 4


def test_snapshot_is_reproducible():
    assert to_jsonable(frozenset({"b", "a"})) == ["a", "b"]
    assert to_jsonable({"d": dt.date(2026, 9, 14), "u": uuid.UUID(int=1)}) == {
        "d": "2026-09-14", "u": "00000000-0000-0000-0000-000000000001"}


def test_agent_results_are_snapshotted_as_structures():
    from backend.agents.context.schema import Circumstance, TextExtraction
    value = TextExtraction(serial_number="HPL-M4103-77842", model_designation=None, error_code="50.2",
                           symptom_text=None, circumstances=[Circumstance(kind="жидкость", quote="пролили чай")])
    assert to_jsonable({"extraction": value})["extraction"] == {
        "serial_number": "HPL-M4103-77842", "model_designation": None, "error_code": "50.2", "symptom_text": None,
        "circumstances": [{"kind": "жидкость", "quote": "пролили чай"}]}


def test_redactor_masks_values_inside_a_prompt():
    text = "Не печатает. Телефон +7 000 000-00-01"
    mask = redactor(text, check_input(text, load_rules()).pii)
    assert mask(f"Текст обращения:\n<<<{text}>>>") == "Текст обращения:\n<<<Не печатает. Телефон [телефон]>>>"


class FakeStore:
    def __init__(self):
        self.calls, self.audits = [], []

    def record_tool_call(self, step_id, tool, request, response, duration_ms, outcome):
        self.calls.append((tool, request, to_jsonable(response), outcome))

    def audit(self, request_id, user_id, event_type, *, before=None, after=None):
        self.audits.append((event_type, user_id, after))


class Source:
    def get_partner(self, ctx, partner_id):
        return Partner(partner_id, "Партнёр", "Юрлицо", "TER-SPB")

    def get_equipment(self, ctx, equipment_id):
        raise AccessDenied("объект другой территории", "equipment", equipment_id)

    def product_price(self, ctx, model_id):
        raise SourceUnavailable("нет ответа")


def test_recording_writes_tool_calls_and_audits_denials():
    store = FakeStore()
    erp = RecordingErp(Source(), store, request_id=uuid.uuid4(), step_id=uuid.uuid4())
    assert erp.get_partner(CTX, "CUST-008").partner_id == "CUST-008"
    with pytest.raises(AccessDenied):
        erp.get_equipment(CTX, "EQ-0011")
    with pytest.raises(SourceUnavailable):
        erp.product_price(CTX, "MOD-003")
    assert [(c[0], c[3]) for c in store.calls] == [
        ("erp.get_partner", "успех"), ("erp.get_equipment", "отказ"), ("erp.product_price", "деградация")]
    assert store.calls[1][2]["http_status"] == 403
    assert store.calls[0][1]["auth"] == {"user_id": "U-002", "role": "Диспетчер", "territories": ["TER-SPB"]}
    assert [a[0] for a in store.audits] == ["access_denied"]


def test_recording_requires_auth_context():
    erp = RecordingErp(Source(), FakeStore(), request_id=uuid.uuid4(), step_id=uuid.uuid4())
    with pytest.raises(TypeError):
        erp.get_partner(None, "CUST-008")
