"""ЗАГЛУШКИ АГЕНТОВ. Языковая модель не вызывается.

Knowledge Agent и Policy Agent пока заменены заглушками с фиксированным
результатом. Context Agent реализован (backend/agents/context/); его
заглушка оставлена для тестов оркестратора без модели: извлечение из текста
и чтение таблички в ней фиксированные, а проверка по справочникам —
настоящая, та же, что у агента.

Пометка на каждом уровне:
- класс или фабрика называется *Stub, объект несёт IS_STUB = True;
- результат несёт stub=True и попадает в снимок шага;
- версия набора артефактов содержит STUB_REVISION вместо ревизий моделей и
  промптов заглушек, так что их шаги отличимы в любой записи.
"""

from __future__ import annotations

import uuid

from backend.agents.context.agent import ContextAgent, ContextTools, PlateUnavailable
from backend.agents.context.schema import Circumstance, PlateReading, TextExtraction
from backend.agents.contracts import (Agents, KnowledgeResult, KnowledgeTools, PolicyCase,
                                      PolicyVerdict, SourceRef)
from backend.knowledge.chunks import NAMESPACE as CHUNK_NAMESPACE
from backend.domain.models import AuthContext
from backend.guardrails.input.check import find_injection, load_rules
from backend.llm.structured import Image
from backend.rules.catalog import load_catalog
from backend.rules.engine import Facts, RuleOutcome
from backend.tools.error_codes import ErrorCodeDirectory

STUB_REVISION = "stub-agents-1"

# Эталонный случай: HP LaserJet Pro M4103dw (EQ-0011) петербургского
# партнёра CUST-008, продан 12.02.2026, код 50.2.
REFERENCE_SERIAL = "HPL-M4103-77842"
REFERENCE_CODE = "50.2"


class StubTextExtractor:
    """ЗАГЛУШКА извлечения из текста: возвращает заданное при создании."""

    IS_STUB = True

    def __init__(self, serial_number: str | None, error_code: str | None, model_designation: str | None,
                 circumstances: tuple[tuple[str, str], ...]):
        self._result = TextExtraction(serial_number=serial_number, model_designation=model_designation,
                                      error_code=error_code, symptom_text=None,
                                      circumstances=[Circumstance(kind=k, quote=q) for k, q in circumstances])

    def extract(self, text: str, tools: ContextTools) -> TextExtraction:
        return self._result


class StubPlateReader:
    """ЗАГЛУШКА чтения таблички: заданный результат или недоступность модели."""

    IS_STUB = True

    def __init__(self, reading: PlateReading | None = None):
        self._reading = reading

    def read(self, image: Image, tools: ContextTools) -> PlateReading:
        if self._reading is None:
            raise PlateUnavailable("заглушка: модель изображений не подключена")
        return self._reading


def ContextAgentStub(serial_number: str | None = REFERENCE_SERIAL, error_code: str | None = REFERENCE_CODE, *,  # noqa: N802
                     model_designation: str | None = None, circumstances: tuple[tuple[str, str], ...] = (),
                     plate: PlateReading | None = None) -> ContextAgent:
    """ЗАГЛУШКА Context Agent: фиксированное извлечение, настоящая проверка."""
    rules = load_rules()
    return ContextAgent(StubTextExtractor(serial_number, error_code, model_designation, circumstances),
                        StubPlateReader(plate), ErrorCodeDirectory.load(load_catalog().code_prefixes_ignored),
                        injection=lambda value: find_injection(value, rules))


class KnowledgeAgentStub:
    """ЗАГЛУШКА Knowledge Agent: фиксированная гипотеза и источник, поиска нет.

    Ссылка указывает на настоящий фрагмент корпуса: идентификатор фрагмента
    считается так же, как загрузчиком, иначе доказательство диагноза ссылалось
    бы в пустоту.
    """

    IS_STUB = True

    def __init__(self, hypothesis: str = "Неисправность узла закрепления (печки)",
                 document_id: str = "DOC-003", section_no: int = 1,
                 evidence_level: str = "код ошибки", sufficient: bool = True):
        reference = f"{document_id}#{section_no}"
        source = SourceRef(reference=reference, document_id=document_id,
                           chunk_id=str(uuid.uuid5(CHUNK_NAMESPACE, f"{document_id}:{section_no}")),
                           title="Руководство производителя", section_title="Коды ошибок",
                           confidentiality_level="Публичный")
        self._result = KnowledgeResult(hypothesis, evidence_level, (source,) if sufficient else (),
                                       (reference,), sufficient=sufficient, stub=True)

    def search(self, ctx: AuthContext, raw_text: str, *, tools: KnowledgeTools) -> KnowledgeResult:
        return self._result


class PolicyAgentStub:
    """ЗАГЛУШКА Policy Agent: признаков для оценки всегда недостаточно.

    Остаётся для модульных и изолированных тестов оркестратора, где языковая
    модель не нужна. В живом наборе агент настоящий (agents/policy/).
    """

    IS_STUB = True

    def evaluate(self, ctx: AuthContext, case: PolicyCase, *, tools=None) -> PolicyVerdict:
        return PolicyVerdict("insufficient", "заглушка: оценка правила требует языковой модели", (), stub=True)


def stub_agents(context: ContextAgent | None = None) -> Agents:
    return Agents(context=context or ContextAgentStub(), knowledge=KnowledgeAgentStub(), policy=PolicyAgentStub())
