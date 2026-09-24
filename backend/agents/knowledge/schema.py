"""Схема структурированного вывода Knowledge Agent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Hypothesis(BaseModel):
    """Гипотеза причины со ссылками на фрагменты, на которых она основана."""
    model_config = ConfigDict(extra="forbid")
    sufficient: bool = Field(description="Достаточно ли во фрагментах данных для гипотезы")
    hypothesis: str = Field(max_length=600)
    sources: list[str] = Field(max_length=3, description="Ссылки вида DOC-003#2 из списка фрагментов")
    evidence_level: Literal["код ошибки", "раздел документации", "аналогия"]
