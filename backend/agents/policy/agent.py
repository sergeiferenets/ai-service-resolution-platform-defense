"""Policy Agent (ADR-0002, ADR-0004): применимость оценочного правила.

Третий агент платформы и самый узкий. Он получает одно правило, помеченное
в своде как требующее оценки, и проверенные факты обращения — и отвечает
только на вопрос применимости.

Чего он не делает: не выбирает способ выполнения работ (маршрут берётся из
самого правила), не квалифицирует гарантию, не подбирает исполнителя, не
считает стоимость, не обращается к учётной системе и к поиску, не создаёт
и не изменяет записи. Детерминированные правила его не касаются — их
целиком применяет механизм правил.

Безопасный исход: любое сомнение — «insufficient», и обращение уходит
человеку. К нему приводят отсутствие признаков, невалидный ответ после
повтора, ссылка на факт, которого не было, истечение бюджета и
недоступность сервиса инференса. Недостаток фактов не заменяется догадкой
модели (ADR-0004, раздел 6).
"""

from __future__ import annotations

import re

from backend.agents.contracts import PolicyCase, PolicyTools, PolicyVerdict
from backend.agents.knowledge.agent import CODE, DOC_REF, MODEL_REF, PART_REF, unsupported_claims
from backend.agents.policy.prompt import RULE_EVALUATION
from backend.agents.policy.schema import RuleApplicability
from backend.domain.models import AuthContext
from backend.llm.client import InferenceError, LlmClient
from backend.llm.structured import StructuredOutputRejected, run_structured
from backend.rules.catalog import Catalog

# Имена фактов: они же — допустимые значения evidence. Модель ссылается на
# то, что ей дали, и ссылка проверяется программно.
RULE_CONDITION, MANUFACTURER, ERROR_CODE, MODEL, SYMPTOM = (
    "rule_condition", "manufacturer", "error_code", "model", "symptom")
# Способ выполнения работ определяет свод правил. Упоминание маршрута в
# обосновании — признак того, что модель вышла за свою задачу.
ROUTE_WORDS = re.compile(r"\b(удал[её]нн|выезд|мастерск|onsite|remote|workshop)", re.I)
# Решения платформы: они принимаются кодом, и называть их агент не вправе ни
# словом, ни полем вида warranty=paid. Русские слова той же природы перекрыты
# проверкой неподтверждённых утверждений.
DECISION_FIELDS = ("warranty", "approval", "price", "cost", "deadline", "term", "sla", "route",
                   "discount", "payment", "invoice", "eta")
# Токен обоснования: слово вместе с внутренними разделителями идентификаторов
# (#801, R-999, 49.xx.yy, HL-L9999DW, 2026-09-17, warranty=paid).
TOKEN = re.compile(r"[#A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9./_=-]*")
# Одно-двузначное число — счёт, а не идентификатор: «два признака», «79».
PLAIN_NUMBER = re.compile(r"\d{1,2}")


def render_facts(facts: dict[str, str]) -> str:
    return "\n".join(f"- {name}: {value}" for name, value in facts.items())


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def identifier_problems(reason: str, given: str) -> list[str]:
    """Идентификаторы в обосновании — только из переданных фактов.

    Перечислять запрещённые виды идентификаторов бесполезно: их формы у
    производителей разные (#801, E42, C6000, HL-L9999DW). Поэтому правило
    обратное: всё, что выглядит идентификатором — содержит цифру или
    начинается с решётки, — обязано встречаться в том, что агенту передали.
    Тогда «49.xx.yy» из условия правила и «49.4C.02» из фактов проходят, а
    выдуманные коды, правила, модели, суммы и даты — нет.
    """
    haystack = normalize(given)
    problems = []
    for raw in TOKEN.findall(reason):
        token = normalize(raw).strip("._-=")
        if not token or not (any(c.isdigit() for c in token) or token.startswith("#")):
            continue
        if PLAIN_NUMBER.fullmatch(token):
            continue
        if token not in haystack:
            problems.append(f"reason: «{token}» нет среди переданных фактов")
    return problems


def decision_problems(reason: str) -> list[str]:
    """Поля решений платформы: warranty, approval, price, deadline и подобные."""
    named = sorted({field for field in DECISION_FIELDS
                    for token in TOKEN.findall(normalize(reason))
                    if token.split("=")[0].strip("._-") == field})
    return [f"reason: решение «{field}» принимает платформа, а не оценка правила" for field in named]


def applicability_check(known: dict[str, str], rule_id: str):
    """Ответ проверяем: ссылки — только на переданные факты, новых
    идентификаторов и решений платформы в обосновании быть не может.

    Код, встречающийся в переданных фактах, не является новым: условие
    правила само записано через шаблон кода («49.xx.yy»), и цитата из него
    законна. Идентификатор оцениваемого правила тоже передан агенту, поэтому
    ссылка на него допустима, а на любое другое правило — нет.
    """
    given = " ".join([rule_id, *known.values()])

    def check(value: RuleApplicability) -> list[str]:
        problems = [f"evidence: факта «{name}» не было среди переданных" for name in value.evidence
                    if name not in known]
        if value.result != "insufficient" and not value.evidence:
            problems.append("evidence: вывод должен опираться хотя бы на один переданный факт")
        problems += [f"reason: модели «{m}» нет среди фактов" for m in MODEL_REF.findall(value.reason)]
        problems += [f"reason: ссылка на документ «{d}» недопустима" for d in DOC_REF.findall(value.reason)]
        problems += [f"reason: артикул «{p}» назвать нельзя" for p in PART_REF.findall(value.reason)]
        problems += identifier_problems(value.reason, given)
        problems += decision_problems(value.reason)
        if ROUTE_WORDS.search(value.reason):
            problems.append("reason: способ выполнения работ определяет свод правил, а не оценка правила")
        problems += [f"reason: {reason}" for reason in unsupported_claims(value.reason)]
        return problems
    return check


class LlmPolicyAgent:
    IS_STUB = False

    def __init__(self, client: LlmClient, catalog: Catalog):
        self._client = client
        self._catalog = catalog

    @staticmethod
    def _insufficient(reason: str) -> PolicyVerdict:
        return PolicyVerdict("insufficient", reason)

    def evaluate(self, ctx: AuthContext, case: PolicyCase, *, tools: PolicyTools | None = None) -> PolicyVerdict:
        rule = self._catalog.rules.get(case.rule_id)
        if rule is None:
            return self._insufficient(f"правила {case.rule_id} нет в своде: оценивать нечего")
        if rule.category != "requires_evaluation":
            return self._insufficient(f"{rule.rule_id} не помечено как требующее оценки")
        if case.condition != rule.condition_text:
            # Оценивается то же правило, что в своде: иначе условие подменено.
            return self._insufficient(f"условие для {rule.rule_id} не совпадает со сводом")
        if tools is None:
            return self._insufficient("вызов модели не записывается: оценка не выполняется")
        if not case.error_code and not case.symptom_text:
            return self._insufficient("нет ни кода ошибки, ни описания неисправности")

        known = {RULE_CONDITION: rule.condition_text}
        for name, value in ((MANUFACTURER, case.manufacturer), (ERROR_CODE, case.error_code),
                            (MODEL, case.model_designation), (SYMPTOM, case.symptom_text)):
            if value:
                known[name] = value

        try:
            value = run_structured(
                self._client, RULE_EVALUATION, RuleApplicability,
                {"rule_id": rule.rule_id, "condition": rule.condition_text, "facts": render_facts(known)},
                record=tools.record, check=applicability_check(known, rule.rule_id), redact=tools.redact)
        except StructuredOutputRejected as rejected:
            return self._insufficient(f"ответ модели дважды не прошёл проверку: {rejected.errors[-1]}")
        except InferenceError as unavailable:
            return self._insufficient(f"сервис инференса: {unavailable}")

        return PolicyVerdict(value.result, value.reason, tuple(value.evidence))
