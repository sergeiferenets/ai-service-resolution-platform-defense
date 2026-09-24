"""Структурированный вывод: схема в запросе → вызов → разбор → проверка.

Порядок:
1. схема вывода передаётся в запросе и, при enforce_schema, принуждается
   сервером на уровне генерации;
2. ответ разбирается и проверяется по схеме и дополнительной проверкой
   вызывающего (check) — ответ может соответствовать схеме, но содержать
   выдуманные значения;
3. при несоответствии — один повтор с перечнем ошибок;
4. второе несоответствие — отказ StructuredOutputRejected. Неразобранный
   текст дальше не передаётся ни при каких условиях.

Истечение бюджета и недоступность сервиса не повторяются — это отказ вызова.
Каждая попытка записывается вызывающей стороной через record: параметры,
ответ, результат разбора, длительность.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence, TypeVar

from pydantic import BaseModel, ValidationError

from backend.llm.client import InferenceError, LlmClient
from backend.llm.prompts import PromptSpec, render

T = TypeVar("T", bound=BaseModel)
Check = Callable[[BaseModel], list[str]]

SUCCESS, INVALID, FAILED = "успех", "отказ", "деградация"
_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


class CallRecorder(Protocol):
    def __call__(self, tool_name: str, request: dict, response: dict, duration_ms: int, outcome: str) -> None: ...


class StructuredOutputRejected(Exception):
    """Ответ дважды не прошёл разбор или проверку."""

    def __init__(self, name: str, errors: list[list[str]]):
        super().__init__(f"{name}: вывод не соответствует схеме после повтора: {errors[-1]}")
        self.name = name
        self.errors = errors


@dataclass(frozen=True)
class Image:
    content_type: str
    data: bytes

    @property
    def data_uri(self) -> str:
        return f"data:{self.content_type};base64,{base64.b64encode(self.data).decode()}"

    def describe(self) -> dict:
        return {"content_type": self.content_type, "bytes": len(self.data),
                "sha256": hashlib.sha256(self.data).hexdigest()}


def parse(text: str, schema: type[T], check: Check | None = None) -> tuple[T | None, list[str]]:
    body = _THINK.sub("", text).strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1)
    try:
        value = schema.model_validate_json(body)
    except ValidationError as exc:
        return None, [f"{'.'.join(str(p) for p in e['loc']) or 'ответ'}: {e['msg']}" for e in exc.errors()][:10]
    problems = check(value) if check else []
    return (None, problems) if problems else (value, [])


def run_structured(client: LlmClient, spec: PromptSpec, schema: type[T], variables: dict[str, str], *,
                   record: CallRecorder, check: Check | None = None, images: Sequence[Image] = (),
                   redact: Callable[[str], str] = lambda s: s) -> T:
    json_schema = schema.model_json_schema()
    version = spec.version(json_schema)
    tool = f"llm.{spec.name}"
    user_text = render(spec.user, variables)
    user_content: str | list[dict] = user_text if not images else (
        [{"type": "text", "text": user_text}]
        + [{"type": "image_url", "image_url": {"url": image.data_uri}} for image in images])
    messages = [{"role": "system", "content": render(spec.system, {"schema": json.dumps(json_schema, ensure_ascii=False)})},
                {"role": "user", "content": user_content}]
    history: list[list[str]] = []

    for attempt in (1, 2):
        request = {"model": client.endpoint.model, "prompt_version": version, "schema": schema.__name__,
                   "attempt": attempt, "params": spec.params(), "user": redact(user_text),
                   "images": [image.describe() for image in images],
                   "retry_errors": history[-1] if history else None}
        started = time.perf_counter()
        try:
            completion = client.complete(messages, max_tokens=spec.max_tokens, temperature=spec.temperature,
                                         disable_thinking=spec.disable_thinking,
                                         json_schema=json_schema if spec.enforce_schema else None,
                                         schema_name=schema.__name__)
        except InferenceError as exc:
            record(tool, request, {"error": type(exc).__name__, "detail": str(exc)}, _ms(started), FAILED)
            raise
        value, errors = parse(completion.text, schema, check)
        record(tool, request, {"text": redact(completion.text), "finish_reason": completion.finish_reason,
                               "usage": completion.usage, "parsed": value.model_dump() if value else None,
                               "errors": errors}, _ms(started), SUCCESS if value else INVALID)
        if value is not None:
            return value
        history.append(errors)
        messages = messages + [{"role": "assistant", "content": completion.text},
                               {"role": "user", "content": render(spec.retry, {"errors": "; ".join(errors)})}]
    raise StructuredOutputRejected(spec.name, history)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
