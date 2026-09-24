"""Интерфейсы агентов (ADR-0002) и их результаты.

Оркестратор зависит только от этих интерфейсов: заглушки и настоящие агенты
взаимозаменяемы. Агент не обращается к учётной системе сам — он получает
порт с записью вызовов от оркестратора (ADR-0002, дополнительное ограничение).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from backend.agents.context.agent import ContextResult, ContextTools
from backend.domain.models import AuthContext
from backend.llm.structured import CallRecorder, Image
from backend.rules.engine import Facts, RuleOutcome


@dataclass(frozen=True)
class KnowledgeTools:
    """Агенту без доступа к учётной системе нужны запись вызовов, аудит и
    маскировка персональных данных в журналах."""
    record: CallRecorder
    audit: Callable[[str, dict], None]
    redact: Callable[[str], str]


@dataclass(frozen=True)
class SourceRef:
    """Ссылка на фрагмент корпуса: проверяемая, с идентификатором фрагмента."""
    reference: str
    document_id: str
    chunk_id: str
    title: str
    section_title: str
    confidentiality_level: str


@dataclass(frozen=True)
class KnowledgeResult:
    """Knowledge Agent: гипотеза причины со ссылками на источники.

    sufficient: false означает, что во фрагментах ответа нет; рекомендация в
    этом случае не формируется, обращение уходит человеку.
    """
    hypothesis: str
    evidence_level: Literal["код ошибки", "раздел документации", "аналогия"]
    sources: tuple[SourceRef, ...]
    considered: tuple[str, ...]
    sufficient: bool
    stub: bool = False


@dataclass(frozen=True)
class PolicyTools:
    """Агент оценки правил не обращается ни к учётной системе, ни к поиску:
    ему нужны только запись вызова модели и маскировка персональных данных."""
    record: CallRecorder
    redact: Callable[[str], str]


@dataclass(frozen=True)
class PolicyCase:
    """Вход Policy Agent: ровно то, что нужно для оценки одного правила.

    Отдельный объект вместо Facts механизма правил: в Facts лежит исходный
    текст обращения, а он агенту не нужен и не передаётся. Ни договора, ни
    цен, ни результатов поиска, ни объектов учётной системы здесь нет и быть
    не может — состав полей закрыт.
    """
    rule_id: str
    condition: str
    manufacturer: str | None = None
    error_code: str | None = None
    model_designation: str | None = None
    symptom_text: str | None = None


@dataclass(frozen=True)
class PolicyVerdict:
    """Policy Agent: применимо ли оценочное правило к фактам обращения.

    Маршрут не выбирается: при «applicable» его берёт оркестратор из самого
    правила. «not_applicable» и «insufficient» ведут к человеку — агент не
    заменяет недостающие признаки догадкой.
    """
    result: Literal["applicable", "not_applicable", "insufficient"]
    reason: str
    evidence: tuple[str, ...] = ()
    stub: bool = False


class ContextAgent(Protocol):
    IS_STUB: bool

    def identify(self, ctx: AuthContext, text: str, *, image: Image | None, partner_id: str | None,
                 tools: ContextTools) -> ContextResult: ...


class KnowledgeAgent(Protocol):
    IS_STUB: bool

    def search(self, ctx: AuthContext, raw_text: str, *, tools: KnowledgeTools) -> KnowledgeResult: ...


class PolicyAgent(Protocol):
    IS_STUB: bool

    def evaluate(self, ctx: AuthContext, case: PolicyCase, *,
                 tools: PolicyTools | None = None) -> PolicyVerdict: ...


@dataclass(frozen=True)
class Agents:
    context: ContextAgent
    knowledge: KnowledgeAgent
    policy: PolicyAgent
