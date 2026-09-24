"""Пути идентификации через оркестратор — без модели: извлечение и чтение
таблички фиксированы заглушкой, проверка по справочникам настоящая.

Окружение — как у test_orchestrator.py (живой мок, база servicedesk_test).
"""

from __future__ import annotations

import datetime as dt
import os

import httpx
import pytest

from backend.adapters.sap_odata import SapODataAdapter
from backend.agents.context.agent import ContextAgent, LlmTextExtractor
from backend.agents.context.schema import PlateReading
from backend.agents.stubs import ContextAgentStub, StubPlateReader, stub_agents
from backend.config import load_settings
from backend.guardrails.input.check import find_injection, load_rules
from backend.llm.client import Endpoint, LlmClient
from backend.llm.structured import Image
from backend.orchestrator.artifacts import slice_artifacts
from backend.orchestrator.db import make_pool
from backend.orchestrator.machine import Orchestrator
from backend.orchestrator.seed import prepare
from backend.orchestrator.states import State
from backend.orchestrator.store import Store
from backend.rules.catalog import load_catalog
from backend.tools.error_codes import ErrorCodeDirectory

TODAY = dt.date(2026, 9, 14)


@pytest.fixture(scope="module")
def env():
    os.environ.setdefault("PLATFORM_DB", "servicedesk_test")
    settings = load_settings()
    prepare(settings)
    pool = make_pool(settings)
    yield Store(pool), SapODataAdapter(settings.erp_base_url, settings.mock_erp_token), load_catalog()
    pool.close()


def run(env, user: str, text: str, context=None, image: Image | None = None):
    store, erp, catalog = env
    orch = Orchestrator(store, erp, catalog, stub_agents(context), slice_artifacts("test", catalog.version),
                        today=lambda: TODAY)
    ctx = store.auth_context(user)
    rid = store.create_request(raw_text=text, channel="тест", impact="среднее", created_by=user,
                               partner_id=ctx.partner_id)
    if image is not None:
        store.save_attachment(rid, image)
    return orch, rid, orch.run(rid, ctx)


def output(store, rid, step: str) -> dict:
    return (store.step_output(rid, step) or {}).get("output") or {}


def test_without_serial_partner_and_model_reach_draft_as_derived(env):
    store = env[0]
    orch, rid, state = run(env, "U-019", "HP LaserJet Pro M4103dw, ошибка 50.2, не печатает",
                           ContextAgentStub(serial_number=None, model_designation="HP LaserJet Pro M4103dw"))
    assert state is State.AWAITING_CONFIRMATION
    assert store.identification(rid)["confidence_level"] == "выведен"
    assert orch.confirm(rid, store.auth_context("U-002"), True) is State.DRAFT_CREATED
    assert store.draft(store.decision(rid)["id"])["payload_snapshot"]["confidence_level"] == "выведен"


def test_ambiguous_model_escalates_with_candidates(env):
    store = env[0]
    _, rid, state = run(env, "U-020", "Canon MF463dw не сканирует",
                        ContextAgentStub(serial_number=None, error_code=None, model_designation="Canon MF463dw"))
    assert state is State.ESCALATED
    assert {c["equipment_id"] for c in output(store, rid, "identification")["candidates"]} == {"EQ-0012", "EQ-0013"}
    reason = store.get_request(rid)["status_reason"]
    assert "EQ-0012" in reason and "EQ-0013" in reason
    assert store.identification(rid) is None


def test_serial_not_found_is_stated_with_discrepancy_and_escalated(env):
    store = env[0]
    _, rid, state = run(env, "U-002", "HP, s/n HPL-M4103-77843, ошибка 50.2",
                        ContextAgentStub(serial_number="HPL-M4103-77843"))
    assert state is State.ESCALATED
    assert store.identification(rid) == {"serial_number": "HPL-M4103-77843", "confidence_level": "со слов",
                                         "discrepancy_flag": True, "source": "текст"}


def test_plate_found_is_recognized(env):
    store = env[0]
    plate = PlateReading(readable=True, manufacturer="HP", model_designation="LaserJet Pro M4103dw",
                         serial_number="HPL-M4103-77842")
    _, rid, state = run(env, "U-002", "Не печатает, ошибка 50.2, фото таблички приложено",
                        ContextAgentStub(serial_number=None, plate=plate), Image("image/jpeg", b"\xff\xd8photo"))
    assert state is State.AWAITING_CONFIRMATION
    assert (store.identification(rid)["confidence_level"], store.identification(rid)["source"]) == ("распознан", "изображение")


def test_liquid_via_safety_guard_gives_risk_flag(env):
    store = env[0]
    _, rid, state = run(env, "U-002", "Принтер HP залит водой после протечки, ошибка 50.2")
    assert state is State.ESCALATED
    assert output(store, rid, "safety_guard")["risk_signals"] == [
        {"kind": "жидкость", "quote": "залит", "source": "предохранитель"}]


def test_liquid_via_agent_gives_risk_flag_without_verdict(env):
    store = env[0]
    _, rid, state = run(env, "U-002", "На МФУ HP пролили кофе, высушили; ошибка 50.2",
                        ContextAgentStub(circumstances=(("жидкость", "пролили кофе"),)))
    assert state is State.AWAITING_CONFIRMATION
    rec = output(store, rid, "recommendation")
    assert rec["warranty"]["status"] == "warranty" and rec["warranty"]["risk_flags"] == ["жидкость"]
    assert rec["risk_signals"] == [{"kind": "жидкость", "quote": "пролили кофе", "source": "агент"}]


def test_two_schema_mismatches_escalate_without_raw_text(env):
    store, _, catalog = env
    garbage = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": "это не JSON"}, "finish_reason": "stop"}]}))
    rules = load_rules()
    agent = ContextAgent(LlmTextExtractor(LlmClient(Endpoint("http://llm/v1", "k", "m", 60),
                                                    http=httpx.Client(transport=garbage))),
                         StubPlateReader(), ErrorCodeDirectory.load(catalog.code_prefixes_ignored),
                         injection=lambda v: find_injection(v, rules))
    _, rid, state = run(env, "U-002", "Принтер HP не печатает, ошибка 50.2", agent)
    assert state is State.ESCALATED and "дважды" in store.get_request(rid)["status_reason"]
    step = next(s for s in store.steps(rid) if s["step_name"] == "identification")
    assert [(c["tool_name"], c["outcome"]) for c in step["tool_calls"]] == [("llm.context", "отказ")] * 2
    assert all(c["duration_ms"] >= 0 and c["response_payload"]["parsed"] is None for c in step["tool_calls"])
    assert store.identification(rid) is None and store.decision(rid) is None
