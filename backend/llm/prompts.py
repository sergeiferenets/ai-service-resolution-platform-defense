"""Шаблон запроса и его версия.

Версия запроса — хеш формулировок, схемы вывода и параметров вызова. Она
входит в prompt_version набора артефактов (data-model.md, раздел 3):
изменение любой части меняет версию.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@dataclass(frozen=True)
class PromptSpec:
    name: str
    revision: int
    system: str               # может содержать {schema}
    user: str                 # подстановки {имя}
    retry: str                # подстановка {errors}
    max_tokens: int = 512
    temperature: float = 0.0
    disable_thinking: bool = True
    enforce_schema: bool = True

    def params(self) -> dict:
        return {"max_tokens": self.max_tokens, "temperature": self.temperature,
                "disable_thinking": self.disable_thinking, "enforce_schema": self.enforce_schema}

    def version(self, json_schema: dict) -> str:
        payload = json.dumps({"spec": asdict(self), "schema": json_schema}, sort_keys=True, ensure_ascii=False)
        return f"{self.name}-{self.revision}-{hashlib.sha256(payload.encode()).hexdigest()[:12]}"


def render(template: str, variables: dict[str, str]) -> str:
    """Подстановка за один проход: подставленное значение повторно не
    разбирается, поэтому фигурные скобки во входных данных безопасны."""
    return _PLACEHOLDER.sub(lambda m: variables.get(m.group(1), m.group(0)), template)
