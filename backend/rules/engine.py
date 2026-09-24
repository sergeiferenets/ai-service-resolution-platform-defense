"""Механизм правил: порядок применения строго по ADR-0004, раздел 4.

    1  безопасность               — немедленная эскалация, перекрывает всё
    2  конкретная модель и код     — правило производителя для этого кода
    3  несколько равноправных      — диагностическое уточнение (ADR-0008)
    4  общий симптом               — при отсутствии специфического кода
    5  неоднозначность / нет правила — передача человеку

Первый сработавший уровень определяет исход. Номер правила и urgency_hint в
порядке применения не участвуют: побеждает более специфичное условие, а не
меньшее число. Симптом при сработавшем коде не отбрасывается — он остаётся
в перечне рассмотренных как дополнительный признак.

Движок работает со структурированными признаками. Извлечение признаков из
текста обращения — задача агентов; исключение — предохранитель безопасности,
который по правилу проверяет исходный текст до всякого рассуждения
(security.md, раздел 3.3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from backend.rules.catalog import Catalog, RuleDef, Trigger

OutcomeKind = Literal["safety_escalation", "applied", "needs_evaluation", "clarification", "human"]


@dataclass(frozen=True)
class Facts:
    manufacturer: str | None
    error_code: str | None
    symptoms: frozenset[str] = field(default_factory=frozenset)
    text: str = ""
    model_id: str | None = None


@dataclass(frozen=True)
class Match:
    rule_id: str
    level: int
    trigger: str


@dataclass(frozen=True)
class RuleOutcome:
    kind: OutcomeKind
    level: int
    rule: RuleDef | None
    considered: tuple[Match, ...]
    reason: str

    @property
    def prescribed_actions(self) -> str | None:
        """Дословно из справочника — без переформулирования."""
        return self.rule.prescribed_actions if self.rule else None


class RuleEngine:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    def safety_text_hit(self, text: str) -> str | None:
        hits = self.safety_text_hits(text)
        return hits[0] if hits else None

    def safety_text_hits(self, text: str) -> list[str]:
        low = (text or "").lower()
        return [k for k in self.catalog.safety_keywords if k in low]

    def normalize_code(self, code: str | None) -> str | None:
        if not code:
            return None
        value = code.strip()
        for prefix in self.catalog.code_prefixes_ignored:
            if value.lower().startswith(prefix.lower()):
                value = value[len(prefix):].strip()
        return value

    def _trigger_matches(self, t: Trigger, facts: Facts, code: str | None) -> bool:
        if t.symptom:
            return t.symptom in facts.symptoms
        if not code or not facts.manufacturer or t.manufacturer.lower() != facts.manufacturer.lower():
            return False
        if not facts.model_id or facts.model_id not in t.model_ids:
            return False
        if t.code_pattern:
            return re.match(t.code_pattern, code, re.IGNORECASE) is not None
        if t.code_range:
            return code.isdigit() and t.code_range[0] <= int(code) <= t.code_range[1]
        return False

    def _matches(self, facts: Facts) -> list[Match]:
        code = self.normalize_code(facts.error_code)
        found = []
        for rule in self.catalog.rules.values():
            for t in rule.triggers:
                if self._trigger_matches(t, facts, code):
                    found.append(Match(rule.rule_id, t.level, t.describe()))
        return found

    def evaluate(self, facts: Facts) -> RuleOutcome:
        matches = self._matches(facts)
        considered = tuple(sorted(matches, key=lambda m: (m.level, m.rule_id)))
        safety = self.catalog.safety_rule

        # Уровень 1. Перекрывает всё, в том числе совпадения по коду.
        keyword = self.safety_text_hit(facts.text)
        if keyword or any(m.level == 1 for m in matches):
            basis = f"ключевое слово «{keyword}» в тексте обращения" if keyword else "признак опасности"
            return RuleOutcome("safety_escalation", 1, safety, considered, f"Безопасность: {basis}")

        # Уровни 2 и 4: первый уровень, на котором есть совпадения, определяет исход.
        for level in (2, 4):
            ids = sorted({m.rule_id for m in matches if m.level == level})
            if level == 2 and len(ids) > 1:
                code = self.normalize_code(facts.error_code)
                candidates = [(rid, t) for rid in ids for t in self.catalog.rules[rid].triggers
                              if t.level == level and self._trigger_matches(t, facts, code)]
                # Сравниваем включение условий, а не числовой вес или длину regex.
                ids = sorted({rid for rid, t in candidates if not any(
                    other != rid and narrower.more_specific_than(t) for other, narrower in candidates)})
            if len(ids) == 1:
                rule = self.catalog.rules[ids[0]]
                kind: OutcomeKind = "applied" if rule.category == "deterministic" else "needs_evaluation"
                return RuleOutcome(kind, level, rule, considered,
                                   f"{rule.rule_id}: уровень {level}, категория {rule.category}")
            if len(ids) > 1:
                return RuleOutcome("clarification", 3, None, considered,
                                   f"Несколько равноправных кандидатов уровня {level}: {', '.join(ids)}")

        return RuleOutcome("human", 5, None, considered, "Подходящего правила нет: передача человеку")
