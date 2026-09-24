"""Модель состояний совпадает с диаграммой sequence-and-states.md, раздел 4."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.orchestrator.states import TERMINAL, TRANSITIONS, IllegalTransition, State, check_transition

DOC = Path(__file__).resolve().parents[2] / "docs" / "architecture" / "sequence-and-states.md"


def diagram() -> tuple[set[tuple[str, str]], set[str]]:
    block = DOC.read_text(encoding="utf-8").split("stateDiagram-v2", 1)[1].split("```", 1)[0]
    edges, final = set(), set()
    for a, b in re.findall(r"^\s*(\S+)\s*-->\s*([^\s:]+)", block, re.M):
        if a == "[*]":
            continue
        if b == "[*]":
            final.add(a)
        else:
            edges.add((a, b))
    return edges, final


def test_transitions_match_diagram_edge_for_edge():
    edges, _ = diagram()
    code = {(a.value, b.value) for a, targets in TRANSITIONS.items() for b in targets}
    assert code == edges


def test_terminal_states_match_diagram():
    _, final = diagram()
    assert {s.value for s in TERMINAL} == final == {"ЧерновикСоздан", "Эскалировано", "Отклонено"}


def test_clarification_point_is_modelled():
    assert State.AWAITING_CLARIFICATION in TRANSITIONS[State.ANALYSIS]
    assert TRANSITIONS[State.AWAITING_CLARIFICATION] == {State.ANALYSIS, State.ESCALATED}


def test_draft_only_after_human():
    before_draft = {s for s, targets in TRANSITIONS.items() if State.DRAFT_CREATED in targets}
    assert before_draft == {State.AWAITING_CONFIRMATION, State.AWAITING_APPROVAL}


def test_illegal_transition_is_refused():
    with pytest.raises(IllegalTransition):
        check_transition(State.DRAFT_CREATED, State.ANALYSIS)
    with pytest.raises(IllegalTransition):
        check_transition(State.DEGRADED, State.RECOMMENDATION_READY)
