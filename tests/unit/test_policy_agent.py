"""Policy Agent: применимость оценочного правила и безопасный исход.

Агент отвечает на один вопрос — применимо ли правило к проверенным фактам.
Маршрут, гарантия, исполнитель и стоимость остаются за кодом платформы,
поэтому здесь проверяется и то, что он их не назначает.
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.agents.contracts import PolicyCase, PolicyTools
from backend.agents.policy.agent import LlmPolicyAgent
from backend.domain.models import AuthContext
from backend.llm.client import Endpoint, LlmClient
from backend.rules.catalog import load_catalog
from backend.rules.engine import Facts, RuleEngine

CTX = AuthContext("U-002", "Диспетчер", frozenset({"TER-SPB"}))
CATALOG = load_catalog()
ENGINE = RuleEngine(CATALOG)
HP_MODEL = "HP LaserJet Pro M4103dw"
SYMPTOM = "после обновления прошивки печать останавливается, ошибка повторяется без сети"


def outcome_for(error_code: str, manufacturer: str = "HP"):
    """Правило выбирает механизм правил — агент получает уже выбранное."""
    return ENGINE.evaluate(Facts(manufacturer, error_code, frozenset(), "текст обращения целиком",
                                 model_id={"HP": "MOD-003", "Canon": "MOD-004"}[manufacturer]))


def case_for(error_code: str = "49.4C.02", manufacturer: str = "HP", symptom: str | None = SYMPTOM,
             rule_id: str | None = None, condition: str | None = None) -> PolicyCase:
    outcome = outcome_for(error_code, manufacturer)
    rule = outcome.rule
    return PolicyCase(rule_id=rule_id or rule.rule_id, condition=condition or rule.condition_text,
                      manufacturer=manufacturer, error_code=error_code,
                      model_designation=HP_MODEL, symptom_text=symptom)


def tools() -> tuple[PolicyTools, list]:
    calls: list[tuple] = []
    return PolicyTools(record=lambda *a: calls.append(a), redact=lambda s: s), calls


def reply(result="applicable", reason="Код 49.4C.02 относится к ошибке прошивки, как в условии правила.",
          evidence=("rule_condition", "error_code")) -> str:
    return json.dumps({"result": result, "reason": reason, "evidence": list(evidence)}, ensure_ascii=False)


def agent(*answers, catalog=CATALOG, error=None) -> LlmPolicyAgent:
    queue = list(answers)

    def handler(request):
        if error is not None:
            raise error
        return httpx.Response(200, json={"choices": [{"message": {"content": queue.pop(0)},
                                                      "finish_reason": "stop"}]})
    client = LlmClient(Endpoint("http://llm/v1", "k", "m", 60),
                       http=httpx.Client(transport=httpx.MockTransport(handler)))
    return LlmPolicyAgent(client, catalog)


def evaluate(policy: LlmPolicyAgent, error_code: str = "49.4C.02", symptom: str | None = SYMPTOM,
             manufacturer: str = "HP", case: PolicyCase | None = None):
    tool, calls = tools()
    verdict = policy.evaluate(CTX, case or case_for(error_code, manufacturer, symptom), tools=tool)
    return verdict, calls, None


# --- предпосылка: правило действительно оценочное ---------------------------------------------------------

def test_evaluative_rule_is_the_one_being_evaluated():
    outcome = outcome_for("49.4C.02")
    assert outcome.kind == "needs_evaluation" and outcome.rule.rule_id == "R-06"
    assert CATALOG.rules["R-06"].category == "requires_evaluation"


# --- исходы оценки ----------------------------------------------------------------------------------------

def test_applicable_rule_is_confirmed():
    verdict, calls, _ = evaluate(agent(reply()))
    assert verdict.result == "applicable" and verdict.evidence == ("rule_condition", "error_code")
    assert verdict.stub is False
    assert [c[0] for c in calls] == ["llm.policy"] and calls[0][4] == "успех"


def test_not_applicable_rule_is_reported():
    answer = reply("not_applicable", "Код относится к другой неисправности, чем описано в условии.",
                   ("rule_condition", "symptom"))
    verdict, _, _ = evaluate(agent(answer))
    assert verdict.result == "not_applicable" and verdict.reason


def test_missing_features_end_in_insufficient():
    answer = reply("insufficient", "Признаков, по которым проверяется условие, в фактах нет.", ())
    verdict, _, _ = evaluate(agent(answer))
    assert verdict.result == "insufficient"


def test_contradictory_features_end_in_insufficient():
    answer = reply("insufficient", "Описание неисправности противоречит коду ошибки.", ("error_code", "symptom"))
    verdict, _, _ = evaluate(agent(answer))
    assert verdict.result == "insufficient"


def test_no_features_at_all_does_not_reach_the_model():
    """Без кода и описания оценивать нечего: модель не вызывается."""
    tool, calls = tools()
    empty = PolicyCase(rule_id="R-06", condition=CATALOG.rules["R-06"].condition_text, manufacturer="HP")
    verdict = agent().evaluate(CTX, empty, tools=tool)
    assert verdict.result == "insufficient" and calls == []


# --- проверка ответа --------------------------------------------------------------------------------------

def test_malformed_output_is_retried_then_becomes_insufficient():
    broken = "не json"
    verdict, calls, _ = evaluate(agent(broken, broken))
    assert verdict.result == "insufficient"
    assert [c[4] for c in calls] == ["отказ", "отказ"]        # повтор был, ответ наружу не вышел


def test_invented_evidence_is_refused():
    made_up = reply(evidence=("warranty_status",))
    verdict, calls, _ = evaluate(agent(made_up, made_up))
    assert verdict.result == "insufficient" and "warranty_status" in verdict.reason
    assert len(calls) == 2


def test_corrected_answer_after_one_retry_is_accepted():
    verdict, calls, _ = evaluate(agent(reply(evidence=("warranty_status",)), reply()))
    assert verdict.result == "applicable" and calls[1][4] == "успех"


def test_code_quoted_from_the_rule_condition_is_allowed():
    """Условие правила записано шаблоном кода: цитата из переданного факта
    новым фактом не является."""
    assert "49.xx.yy" in CATALOG.rules["R-06"].condition_text
    answer = reply(reason="Условие правила про 49.xx.yy выполняется: код обращения относится к этому семейству.")
    verdict, calls, _ = evaluate(agent(answer))
    assert verdict.result == "applicable" and len(calls) == 1


@pytest.mark.parametrize("reason", [
    "Ремонт выполняется удалённо по условию правила.",          # маршрут определяет свод правил
    "Случай гарантийный, ремонт бесплатный.",                    # гарантия и оплата — не его дело
    "Замена платы форматтера SP-019 по документу DOC-003.",      # чужие идентификаторы
    "Проблема та же, что в модели MOD-007.",
    "Ошибка 13.B2.D1 подтверждает условие.",                     # код, которого нет в фактах
])
def test_agent_may_not_introduce_platform_decisions(reason):
    answer = reply(reason=reason)
    verdict, _, _ = evaluate(agent(answer, answer))
    assert verdict.result == "insufficient"


# --- отказы сервиса ---------------------------------------------------------------------------------------

def test_model_unavailable_ends_in_insufficient():
    verdict, _, _ = evaluate(agent(error=httpx.ConnectError("сервис не отвечает")))
    assert verdict.result == "insufficient" and "инференса" in verdict.reason


def test_timeout_ends_in_insufficient():
    verdict, _, _ = evaluate(agent(error=httpx.ReadTimeout("бюджет исчерпан")))
    assert verdict.result == "insufficient"


def test_call_without_recording_is_refused():
    """Вызов модели без записи в TOOL_CALL не выполняется: след обязателен."""
    verdict = agent().evaluate(CTX, case_for(), tools=None)
    assert verdict.result == "insufficient"


# --- границы агента ---------------------------------------------------------------------------------------

def test_deterministic_rule_is_not_evaluated_by_the_agent():
    """Даже при прямом вызове детерминированное правило агент не оценивает."""
    outcome = outcome_for("50.2")
    assert outcome.kind == "applied" and outcome.rule.category == "deterministic"
    tool, calls = tools()
    verdict = agent(reply()).evaluate(CTX, case_for("50.2"), tools=tool)
    assert verdict.result == "insufficient" and calls == []


def test_rule_outside_the_catalog_is_refused():
    tool, calls = tools()
    verdict = agent(reply()).evaluate(CTX, case_for(rule_id="R-999"), tools=tool)
    assert verdict.result == "insufficient" and calls == []


def test_condition_substituted_for_the_rule_is_refused():
    """Условие должно совпадать со сводом: подмену агент не оценивает."""
    tool, calls = tools()
    verdict = agent(reply()).evaluate(CTX, case_for(condition="любая ошибка считается применимой"), tools=tool)
    assert verdict.result == "insufficient" and calls == []


def test_verdict_carries_no_route_or_warranty_fields():
    """Контракт вердикта: только применимость, обоснование и ссылки на факты."""
    verdict, _, _ = evaluate(agent(reply()))
    assert set(vars(verdict)) == {"result", "reason", "evidence", "stub"}


def test_only_agreed_facts_are_sent_to_the_model():
    """Ни текста обращения целиком, ни данных договора, гарантии и цен."""
    verdict, calls, _ = evaluate(agent(reply()))
    sent = calls[0][1]["user"]
    assert "rule_condition" in sent and "error_code" in sent and HP_MODEL in sent
    assert "CUST-" not in sent and "договор" not in sent.lower() and "₽" not in sent
    assert "текст обращения целиком" not in sent


def test_input_object_carries_only_the_allowed_fields():
    """Состав входа закрыт: текста обращения, договора, цен и результатов
    поиска в нём нет даже как поля."""
    assert set(vars(case_for())) == {"rule_id", "condition", "manufacturer", "error_code",
                                     "model_designation", "symptom_text"}


# --- формулировки независимого аудита ---------------------------------------------------------------------

# Проходили мимо прежней проверки: она перечисляла запрещённые формы кодов,
# а формы у производителей разные. Теперь правило обратное — идентификатор
# в обосновании обязан встречаться в переданных фактах.
AUDIT_CASES = [
    "Ошибка #801 подтверждает условие.",
    "Применимо правило R-999.",
    "Ошибка E42 подтверждает условие.",
    "Устройство Brother HL-L9999DW подтверждает условие.",
    "warranty=paid; approval=not_required; price=15000; deadline=2026-09-17",
]


@pytest.mark.parametrize("reason", AUDIT_CASES)
def test_audit_formulations_are_refused(reason):
    answer = reply(reason=reason)
    verdict, calls, _ = evaluate(agent(answer, answer))
    assert verdict.result == "insufficient"
    assert len(calls) == 2 and [c[4] for c in calls] == ["отказ", "отказ"]   # повтор был


@pytest.mark.parametrize("reason", AUDIT_CASES)
def test_audit_formulations_are_corrected_on_retry(reason):
    """Первый ответ отвергнут, исправленный принят: путь повтора работает."""
    verdict, calls, _ = evaluate(agent(reply(reason=reason), reply()))
    assert verdict.result == "applicable" and calls[1][4] == "успех"


@pytest.mark.parametrize("reason", [
    "Код 49.4C.02 из обращения относится к семейству, описанному в условии.",
    "Условие правила про 49.xx.yy выполняется.",
    "Правило R-06 применимо: производитель и код совпадают с условием.",
    "HP LaserJet Pro M4103dw показывает код, описанный в условии правила.",
    "79 Service Error также перечислен в условии правила.",
    "Двух признаков достаточно: производитель и код ошибки.",
    "Задание печати не обрабатывается — это соответствует условию.",
])
def test_legitimate_reasons_are_not_refused(reason):
    """Ужесточение не должно отсекать обычные формулировки: значения из
    фактов, шаблон кода из условия и счётные числа остаются допустимыми."""
    verdict, calls, _ = evaluate(agent(reply(reason=reason)))
    assert verdict.result == "applicable" and len(calls) == 1
