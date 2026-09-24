"""Клиент к локальному сервису инференса (OpenAI-совместимый API vLLM).

Бюджет времени — ADR-0007: языковая модель 60 с, модель изображений 30 с.
Повторов на уровне транспорта нет: истечение бюджета или недоступность
сервиса — отказ вызова. Размыкатель к сервису инференса не применяется
(ADR-0007): без него процесс невозможен, отказ возвращается явно.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


class InferenceError(Exception):
    """Вызов модели не выполнен."""


class InferenceTimeout(InferenceError):
    """Бюджет времени исчерпан."""


class InferenceUnavailable(InferenceError):
    """Сервис не отвечает или отвечает ошибкой сервера."""


class InferenceRejected(InferenceError):
    """Сервис отверг запрос (ошибка 4xx): ошибка конфигурации или запроса."""


class InferenceContractError(InferenceError):
    """Ответ 200, но не по контракту OpenAI-совместимого API.

    Пустой choices, отсутствующее message, ответ не в JSON — сбой сервиса
    инференса, а не ответ модели. Отличается от отвергнутого разбора: повтор
    здесь бессмыслен, вызов считается невыполненным (ADR-0007).
    """


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    api_key: str
    model: str
    timeout_s: float


@dataclass(frozen=True)
class Completion:
    text: str
    finish_reason: str | None
    usage: dict[str, Any] = field(default_factory=dict)


class LlmClient:
    def __init__(self, endpoint: Endpoint, *, http: httpx.Client | None = None):
        self.endpoint = endpoint
        self._http = http or httpx.Client()

    def _post(self, path: str, payload: dict) -> dict:
        try:
            response = self._http.post(f"{self.endpoint.base_url.rstrip('/')}{path}", json=payload,
                                       headers={"Authorization": f"Bearer {self.endpoint.api_key}"},
                                       timeout=self.endpoint.timeout_s)
        except httpx.TimeoutException as exc:
            raise InferenceTimeout(f"бюджет {self.endpoint.timeout_s:g} с исчерпан") from exc
        except httpx.TransportError as exc:
            raise InferenceUnavailable(f"сервис недоступен: {exc}") from exc
        if response.status_code >= 500:
            raise InferenceUnavailable(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise InferenceRejected(f"HTTP {response.status_code}: {response.text[:500]}")
        try:
            body = response.json()
        except ValueError as exc:
            raise InferenceContractError(f"ответ не в формате JSON: {response.text[:200]}") from exc
        if not isinstance(body, dict):
            raise InferenceContractError(f"ответ не объект: {type(body).__name__}")
        return body

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Векторные представления для плотной ветви поиска (ADR-0003)."""
        data = self._post("/embeddings", {"model": self.endpoint.model, "input": texts}).get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise InferenceContractError(f"ожидалось {len(texts)} векторов, получено: {data!r:.120}")
        try:
            return [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
        except (KeyError, TypeError) as exc:
            raise InferenceContractError(f"вектор без поля embedding или index: {exc}") from exc

    def complete(self, messages: list[dict], *, max_tokens: int, temperature: float, disable_thinking: bool,
                 json_schema: dict | None = None, schema_name: str = "output") -> Completion:
        payload: dict[str, Any] = {"model": self.endpoint.model, "messages": messages,
                                   "temperature": temperature, "max_tokens": max_tokens}
        if disable_thinking:
            # Режим размышления отключается в каждом запросе, а не на сервере
            # (model-card, раздел 1): иначе блок рассуждения ломает разбор.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if json_schema is not None:
            # Принуждение схемы на стороне сервера экономит повторы; разбор и
            # проверка ответа остаются обязательными.
            payload["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": schema_name, "schema": json_schema}}
        data = self._post("/chat/completions", payload)
        # Ответ 200 с пустым или неполным choices — сбой сервиса, а не ответ
        # модели: вызов не выполнен, исход определён (ADR-0007).
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise InferenceContractError(f"ответ без выбора модели: choices={choices!r:.120}")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise InferenceContractError("ответ без message в выборе модели")
        return Completion(message.get("content") or "", choices[0].get("finish_reason"), data.get("usage") or {})
