"""Набор оценки Context Agent на живых моделях (узел с vLLM).

Окружение — как у интеграционных тестов плюс ключ и адреса сервиса
инференса: VLLM_API_KEY_FILE, LLM_BASE_URL, VISION_BASE_URL,
LLM_SERVED_NAME, VISION_SERVED_NAME (infra/.env). База — servicedesk_test.

Случаи — context_cases.yaml. Отдельно — точность чтения всех эталонных
табличек (data/eval/nameplates/manifest.json).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
from pathlib import Path

import pytest
import yaml

from backend.adapters.sap_odata import SapODataAdapter
from backend.agents.context.agent import ContextAgent, ContextTools, PlateUnavailable, VisionPlateReader
from backend.agents.factory import model_agents
from backend.config import load_settings
from backend.guardrails.input.check import load_rules
from backend.llm.client import Endpoint, LlmClient
from backend.llm.structured import Image
from backend.orchestrator.artifacts import build_artifacts, composite_rules_version
from backend.orchestrator.db import make_pool
from backend.orchestrator.machine import Orchestrator
from backend.orchestrator.seed import prepare
from backend.orchestrator.store import Store
from backend.rules.catalog import load_catalog
from backend.tools.error_codes import ErrorCodeDirectory

ROOT = Path(__file__).resolve().parents[2]
PLATES = ROOT / "data" / "eval" / "nameplates"
CASES = yaml.safe_load(Path(__file__).with_name("context_cases.yaml").read_text(encoding="utf-8"))["cases"]
TODAY = dt.date(2026, 9, 14)


@pytest.fixture(scope="module")
def env():
    os.environ.setdefault("PLATFORM_DB", "servicedesk_test")
    settings = load_settings()
    prepare(settings)
    pool = make_pool(settings)
    catalog, rules = load_catalog(), load_rules()
    codes = ErrorCodeDirectory.load(catalog.code_prefixes_ignored)
    agent_set = model_agents(settings, codes, rules, catalog)
    artifacts = build_artifacts(code_commit="eval", llm_model_revision=settings.llm_model_revision,
                                vision_model_revision=settings.vision_model_revision, embedding_model_revision=settings.embed_model_revision, prompt_versions=agent_set.prompt_versions,
                                rules_version=composite_rules_version(catalog.version, codes.digest, rules.digest))
    store = Store(pool)
    erp = SapODataAdapter(settings.erp_base_url, settings.mock_erp_token)
    # Модель изображений «недоступна»: адрес, на котором никто не слушает.
    down = dataclasses.replace(agent_set.agents, context=ContextAgent(
        agent_set.agents.context._extractor, VisionPlateReader(LlmClient(Endpoint("http://127.0.0.1:9/v1", "k", "m", 2))),
        codes, agent_set.agents.context._injection))
    make = lambda agents: Orchestrator(store, erp, catalog, agents, artifacts, today=lambda: TODAY, input_rules=rules)
    yield {"store": store, "live": make(agent_set.agents), "vision_down": make(down),
           "reader": agent_set.agents.context._plate}
    pool.close()


def observed(store: Store, rid) -> dict:
    ident = ((store.step_output(rid, "identification") or {}).get("output")) or {}
    safety = ((store.step_output(rid, "safety_guard") or {}).get("output")) or {}
    extraction = ident.get("extraction") or {}
    steps = store.steps(rid)
    return {
        "state": store.get_request(rid)["status"],
        "status": ident.get("status"), "confidence": ident.get("confidence_level"),
        "discrepancy": ident.get("discrepancy"), "serial": extraction.get("serial_number"),
        "error_code": extraction.get("error_code"), "code_status": (ident.get("code_check") or {}).get("status"),
        "risk": sorted({r["kind"] for r in safety.get("risk_signals", []) + ident.get("risk_signals", [])}),
        "candidates": len(ident.get("candidates", [])),
        "llm": any(c["tool_name"].startswith("llm.") for s in steps for c in s["tool_calls"]),
        "llm_durations": [c["duration_ms"] for s in steps for c in s["tool_calls"] if c["tool_name"].startswith("llm.")],
    }


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case(env, case):
    store = env["store"]
    orch = env["vision_down"] if case.get("vision") == "down" else env["live"]
    ctx = store.auth_context(case["user"])
    rid = store.create_request(raw_text=case["text"], channel="оценка", impact="среднее", created_by=case["user"],
                               partner_id=ctx.partner_id)
    if case.get("plate"):
        store.save_attachment(rid, Image("image/jpeg", (PLATES / case["plate"]).read_bytes()))
    orch.run(rid, ctx)
    got = observed(store, rid)
    print(f"\n{case['id']}: {json.dumps({k: v for k, v in got.items() if k != 'llm_durations'}, ensure_ascii=False)}")
    expect = dict(case["expect"])
    if "risk" in expect:
        expect["risk"] = sorted(expect["risk"])
    mismatches = {k: (v, got[k]) for k, v in expect.items() if got[k] != v}
    assert not mismatches, f"ожидалось / получено: {mismatches}"
    if got["llm"]:
        # Длительность записана у каждого вызова; отказ соединения может уложиться в 0 мс.
        assert all(isinstance(d, int) and d >= 0 for d in got["llm_durations"]), "вызов модели без длительности"


def test_plate_reading_accuracy(env):
    """Точность чтения эталонных табличек. Главное свойство безопасности:
    с нечитаемого снимка не должен «прочитаться» номер из справочника."""
    manifest = json.loads((PLATES / "manifest.json").read_text(encoding="utf-8"))["plates"]
    tools = ContextTools(erp=None, record=lambda *a: None, redact=lambda s: s)
    by_group: dict[str, list[bool]] = {}
    known = {p["serial_on_plate"] for p in manifest if p["in_reference"]}
    leaked = []
    for plate in manifest:
        try:
            reading = env["reader"].read(Image("image/jpeg", (PLATES / plate["file"]).read_bytes()), tools)
            serial = reading.serial_number if reading.readable else None
        except PlateUnavailable:
            serial = None
        exact = (serial or "").strip().upper() == plate["serial_on_plate"]
        by_group.setdefault(plate["group"], []).append(exact)
        if plate["group"] == "unreadable" and serial and serial.strip().upper() in known:
            leaked.append((plate["file"], serial))
        print(f"{plate['file']:34s} ожидалось {plate['serial_on_plate']:18s} прочитано {serial}")
    summary = {g: f"{sum(v)}/{len(v)}" for g, v in by_group.items()}
    print("точность по группам:", summary)
    assert not leaked, f"номер из справочника «прочитан» с нечитаемого снимка: {leaked}"
    assert sum(by_group["readable_found"]) >= 8, summary
