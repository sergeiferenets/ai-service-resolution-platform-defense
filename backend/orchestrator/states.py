"""Состояния обращения и допустимые переходы.

Источник — docs/architecture/sequence-and-states.md, раздел 4. Значения
совпадают с именами состояний диаграммы; тест tests/unit/test_states.py
сверяет таблицу переходов с диаграммой ребро в ребро.

Точка приостановки для уточнения (ADR-0008) предусмотрена моделью:
состояние ОжиданиеУточнения и его переходы есть в таблице. Сама
приостановка с возобновлением в срезе не реализована — см. machine.py.
"""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    ACCEPTED = "Принято"
    ANALYSIS = "Разбор"
    AWAITING_CLARIFICATION = "ОжиданиеУточнения"
    DEGRADED = "ДеградированныйРежим"
    RECOMMENDATION_NO_FACTS = "РекомендацияБезФактов"
    RECOMMENDATION_READY = "РекомендацияГотова"
    AWAITING_CONFIRMATION = "ОжиданиеПодтверждения"
    AWAITING_APPROVAL = "ОжиданиеСогласования"
    DRAFT_CREATED = "ЧерновикСоздан"
    ESCALATED = "Эскалировано"
    REJECTED = "Отклонено"


S = State

TRANSITIONS: dict[State, frozenset[State]] = {
    S.ACCEPTED: frozenset({S.REJECTED, S.ESCALATED, S.ANALYSIS}),
    S.ANALYSIS: frozenset({S.AWAITING_CLARIFICATION, S.ESCALATED, S.RECOMMENDATION_READY, S.DEGRADED}),
    S.AWAITING_CLARIFICATION: frozenset({S.ANALYSIS, S.ESCALATED}),
    S.DEGRADED: frozenset({S.RECOMMENDATION_NO_FACTS}),
    S.RECOMMENDATION_NO_FACTS: frozenset({S.ESCALATED}),
    S.RECOMMENDATION_READY: frozenset({S.AWAITING_CONFIRMATION}),
    S.AWAITING_CONFIRMATION: frozenset({S.REJECTED, S.AWAITING_APPROVAL, S.DRAFT_CREATED}),
    S.AWAITING_APPROVAL: frozenset({S.DRAFT_CREATED, S.REJECTED}),
    S.DRAFT_CREATED: frozenset(),
    S.ESCALATED: frozenset(),
    S.REJECTED: frozenset(),
}

TERMINAL = frozenset(s for s, targets in TRANSITIONS.items() if not targets)


class IllegalTransition(Exception):
    pass


def check_transition(current: State, target: State) -> None:
    if target not in TRANSITIONS[current]:
        raise IllegalTransition(f"Переход «{current.value}» → «{target.value}» не предусмотрен моделью состояний")
