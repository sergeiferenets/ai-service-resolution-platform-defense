"""Клиент векторного хранилища.

Бюджет времени по ADR-0007: поиск — 5 с, отказ вызова без повтора. Ответ
без источников не выдаётся, поэтому отказ поиска приводит к эскалации,
а не к ответу по внутреннему знанию модели.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

SEARCH_BUDGET_S = 5.0
LOAD_BUDGET_S = 60.0


class RetrievalError(Exception):
    """Хранилище не ответило или ответило ошибкой."""


@dataclass(frozen=True)
class QdrantEndpoint:
    base_url: str
    api_key: str
    collection: str


class QdrantClient:
    def __init__(self, endpoint: QdrantEndpoint, *, timeout_s: float = SEARCH_BUDGET_S,
                 http: httpx.Client | None = None):
        self.endpoint = endpoint
        self._timeout = timeout_s
        self._http = http or httpx.Client()

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.endpoint.base_url.rstrip('/')}/collections/{self.endpoint.collection}{path}"
        try:
            response = self._http.request(method, url, json=payload, timeout=self._timeout,
                                          headers={"api-key": self.endpoint.api_key})
        except httpx.TimeoutException as exc:
            raise RetrievalError(f"бюджет {self._timeout:g} с исчерпан: {method} {path}") from exc
        except httpx.TransportError as exc:
            raise RetrievalError(f"хранилище недоступно: {exc}") from exc
        if response.status_code >= 400:
            raise RetrievalError(f"HTTP {response.status_code}: {response.text[:300]}")
        return response.json()

    def query(self, body: dict) -> list[dict]:
        return self._request("POST", "/points/query", body)["result"]["points"]

    def upsert(self, points: list[dict]) -> None:
        self._request("PUT", "/points?wait=true", {"points": points})

    def delete_document(self, document_id: str) -> None:
        """Фрагменты документа удаляются перед загрузкой его новой редакции:
        иначе разделы, которых больше нет, остались бы в выдаче."""
        self._request("POST", "/points/delete?wait=true",
                      {"filter": {"must": [{"key": "document_id", "match": {"value": document_id}}]}})

    def count(self, query_filter: dict | None = None) -> int:
        body: dict[str, Any] = {"exact": True}
        if query_filter:
            body["filter"] = query_filter
        return self._request("POST", "/points/count", body)["result"]["count"]
