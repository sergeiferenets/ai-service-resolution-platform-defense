"""Схема структурированного вывода Policy Agent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RuleApplicability(BaseModel):
    """Применимо ли оценочное правило к фактам обращения.

    Маршрута в ответе нет: его определяет свод правил, а не модель.
    """
    model_config = ConfigDict(extra="forbid")
    result: Literal["applicable", "not_applicable", "insufficient"]
    reason: str = Field(max_length=300, description="Одно-два предложения: почему именно такой вывод")
    evidence: list[str] = Field(default_factory=list, max_length=4,
                                description="Имена переданных фактов, на которых основан вывод")
