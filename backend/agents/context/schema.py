"""Схемы структурированного вывода Context Agent.

Изменение схемы меняет версию запроса (prompt_version).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CircumstanceKind = Literal["падение", "жидкость", "вскрытие", "неоригинальные расходные материалы"]


class Circumstance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: CircumstanceKind
    quote: str = Field(max_length=200, description="Дословная цитата из текста обращения")


class TextExtraction(BaseModel):
    """Извлечение из текста обращения: только дословные значения."""
    model_config = ConfigDict(extra="forbid")
    serial_number: str | None = Field(max_length=40)
    model_designation: str | None = Field(max_length=80)
    error_code: str | None = Field(max_length=60)
    symptom_text: str | None = Field(max_length=400, description="Дословный фрагмент с описанием неисправности")
    circumstances: list[Circumstance] = Field(max_length=4)


class PlateReading(BaseModel):
    """Чтение идентификационной таблички с фотографии."""
    model_config = ConfigDict(extra="forbid")
    readable: bool
    manufacturer: str | None = Field(max_length=40)
    model_designation: str | None = Field(max_length=80)
    serial_number: str | None = Field(max_length=30)
