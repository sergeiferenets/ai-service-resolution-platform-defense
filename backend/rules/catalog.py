"""Свод правил в машиночитаемом виде.

Два источника, у каждого своя роль:
- data/reference/04_decision_rules.csv — дословные поля правила: условие,
  решение, urgency_hint, предписанные действия. Текст не переписывается
  (ADR-0004, раздел 5);
- data/reference/rules.yaml — исполнимая часть: категория по ADR-0004,
  условия срабатывания с уровнем, требуемая компетенция.

Версия свода — хеш обоих файлов. Она входит в версию набора артефактов
(data-model.md, ARTIFACT_VERSION): изменение любого из них меняет версию.
"""

from __future__ import annotations

import hashlib
import csv
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from data.loader.reference import REF, DecisionRuleText, load_decision_rules

RULES_YAML = REF / "rules.yaml"
RULES_CSV = REF / "04_decision_rules.csv"

CATEGORIES = {"safety", "deterministic", "requires_evaluation"}

# Уровни ADR-0004, раздел 4. Уровень 3 (несколько равноправных кандидатов)
# и уровень 5 (передача человеку) — исходы, а не типы условий.
LEVELS = {"safety": 1, "model_code": 2, "symptom": 4}


@dataclass(frozen=True)
class Trigger:
    level: int
    symptom: str | None = None
    manufacturer: str | None = None
    code_pattern: str | None = None
    code_range: tuple[int, int] | None = None
    model_ids: frozenset[str] = field(default_factory=frozenset)

    def more_specific_than(self, other: "Trigger") -> bool:
        """Доказуемо более узкое условие: подмножество моделей и кодов.

        Разные регулярные выражения не ранжируем эвристически. Для них
        включение неизвестно, поэтому движок оставляет уточнение человеку.
        """
        if self.level != 2 or other.level != 2 or self.manufacturer != other.manufacturer:
            return False
        if not self.model_ids or not self.model_ids <= other.model_ids:
            return False
        same_code = (self.code_pattern, self.code_range) == (other.code_pattern, other.code_range)
        narrower_range = (self.code_range is not None and other.code_range is not None
                          and other.code_range[0] <= self.code_range[0] <= self.code_range[1] <= other.code_range[1]
                          and self.code_range != other.code_range)
        return (same_code and self.model_ids < other.model_ids) or narrower_range

    def describe(self) -> str:
        if self.symptom:
            return f"симптом {self.symptom}"
        code = self.code_pattern or f"{self.code_range[0]}–{self.code_range[1]}"
        return f"{self.manufacturer} [{', '.join(sorted(self.model_ids))}]: код {code}"


@dataclass(frozen=True)
class RuleDef:
    rule_id: str
    category: str
    route: str
    urgency_hint: int
    required_skill: str | None
    condition_text: str
    prescribed_actions: str
    triggers: tuple[Trigger, ...]


@dataclass(frozen=True)
class Catalog:
    version: str
    rules: dict[str, RuleDef]
    symptom_tags: frozenset[str]
    safety_keywords: tuple[str, ...]
    code_prefixes_ignored: tuple[str, ...]
    warranty_months_by_type: dict[str, int]
    # Ключевое слово предохранителя → признак риска по гарантии (PRD 5.10).
    safety_warranty_risk: dict[str, str] = field(default_factory=dict)

    @property
    def safety_rule(self) -> RuleDef:
        return next(r for r in self.rules.values() if r.category == "safety")


class CatalogError(Exception):
    pass


def load_catalog(yaml_path: Path = RULES_YAML, csv_path: Path = RULES_CSV,
                 texts: dict[str, DecisionRuleText] | None = None) -> Catalog:
    raw = yaml_path.read_bytes()
    spec = yaml.safe_load(raw)
    with (REF / "01_models.csv").open(encoding="utf-8") as f:
        known_models = {r["model_id"]: r["Производитель"] for r in csv.DictReader(f)}
    texts = texts if texts is not None else load_decision_rules()
    problems: list[str] = []
    rules: dict[str, RuleDef] = {}

    for r in spec["rules"]:
        rid = r["id"]
        text = texts.get(rid)
        if text is None:
            problems.append(f"{rid}: нет в справочнике правил")
            continue
        if r["category"] not in CATEGORIES:
            problems.append(f"{rid}: неизвестная категория {r['category']}")
        triggers = []
        for t in r["triggers"]:
            if t["level"] not in LEVELS:
                problems.append(f"{rid}: неизвестный уровень {t['level']}")
                continue
            if t.get("symptom") and t["symptom"] not in spec["symptom_tags"]:
                problems.append(f"{rid}: неизвестная метка симптома {t['symptom']}")
            rng = t.get("code_range")
            models = t.get("model_ids", [])
            if t["level"] == "model_code" and (not isinstance(models, list) or not models
                    or not all(isinstance(m, str) and m for m in models)
                    or not t.get("manufacturer") or not (t.get("code_pattern") or rng)):
                problems.append(f"{rid}: model_code требует явных model_ids, производителя и кода")
                continue
            if any(known_models.get(m) != t.get("manufacturer") for m in models):
                problems.append(f"{rid}: model_ids не соответствуют справочнику моделей и производителю")
                continue
            triggers.append(Trigger(
                level=LEVELS[t["level"]], symptom=t.get("symptom"), manufacturer=t.get("manufacturer"),
                code_pattern=t.get("code_pattern"), code_range=(int(rng[0]), int(rng[1])) if rng else None,
                model_ids=frozenset(models)))
        rules[rid] = RuleDef(
            rule_id=rid, category=r["category"], route=text.route, urgency_hint=text.urgency_hint,
            required_skill=r.get("required_skill"), condition_text=text.condition_text,
            prescribed_actions=text.prescribed_actions, triggers=tuple(triggers))

    if sum(1 for r in rules.values() if r.category == "safety") != 1:
        problems.append("в своде должно быть ровно одно правило безопасности")

    keywords: list[str] = []
    warranty_risk: dict[str, str] = {}
    for item in spec["safety_keywords"]:
        keyword = (item["keyword"] if isinstance(item, dict) else item).lower()
        keywords.append(keyword)
        if isinstance(item, dict) and item.get("warranty_risk"):
            warranty_risk[keyword] = item["warranty_risk"]
    if problems:
        raise CatalogError("Свод правил не прошёл проверку:\n  " + "\n  ".join(problems))

    digest = hashlib.sha256(raw + csv_path.read_bytes()).hexdigest()[:12]
    return Catalog(
        version=f"rules-{spec['version']}-{digest}",
        rules=rules,
        symptom_tags=frozenset(spec["symptom_tags"]),
        safety_keywords=tuple(keywords),
        code_prefixes_ignored=tuple(spec.get("code_prefixes_ignored", [])),
        warranty_months_by_type=dict(spec["warranty_months_by_type"]),
        safety_warranty_risk=warranty_risk,
    )
