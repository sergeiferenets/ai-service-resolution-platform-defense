"""Преобразование доменных записей в JSON для снимков шагов и вызовов."""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel


def to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, BaseModel):
        # Результаты агентов — модели pydantic; без этой ветки снимок получал строку вместо структуры.
        return to_jsonable(value.model_dump())
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        # Порядок множества не определён — сортировка делает снимок воспроизводимым.
        return sorted((to_jsonable(v) for v in value), key=str)
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
