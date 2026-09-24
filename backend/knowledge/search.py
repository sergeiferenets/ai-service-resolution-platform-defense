"""Гибридный поиск: плотная ветвь, разреженная ветвь, слияние RRF (ADR-0003).

Права применяются в самом запросе, до ранжирования, и одним и тем же
условием к обеим ветвям и к слиянию. Раздельная фильтрация недопустима:
она позволила бы получить закрытый фрагмент через одну ветвь в обход
другой (ADR-0003, Compliance; критерий приёмки AC-4).

Уровни конфиденциальности — PRD, раздел 3.3:
- «Публичный» — всем ролям, включая представителя партнёра;
- «Внутренний» — только сотрудникам сервисной организации;
- «Партнёрский» — своей территории, а представителю партнёра — только
  документы его партнёра.
Территория фрагмента, если она задана, должна входить в территории
пользователя.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from backend.domain.access import INTERNAL, PARTNER, PUBLIC, STAFF_ROLES, is_staff
from backend.domain.models import AuthContext
from backend.knowledge.bm25 import Bm25
from backend.knowledge.qdrant import QdrantClient

CANDIDATES = 40

__all__ = ["CANDIDATES", "Fragment", "INTERNAL", "PARTNER", "PUBLIC", "Retriever", "STAFF_ROLES",
           "access_filter", "scope_conditions"]


@dataclass(frozen=True)
class Fragment:
    chunk_id: str
    document_id: str
    title: str
    section_no: int
    section_title: str
    content: str
    confidentiality_level: str
    source: str
    model_refs: tuple[str, ...]
    score: float

    @property
    def reference(self) -> str:
        return f"{self.document_id}#{self.section_no}"


def access_filter(ctx: AuthContext) -> dict:
    """Единое условие доступа: уровень конфиденциальности и территория."""
    levels: list[dict] = [{"must": [{"key": "confidentiality_level", "match": {"value": PUBLIC}}]}]
    if is_staff(ctx):
        levels.append({"must": [{"key": "confidentiality_level", "match": {"value": INTERNAL}}]})
        levels.append({"must": [{"key": "confidentiality_level", "match": {"value": PARTNER}},
                                {"key": "territory_id", "match": {"any": sorted(ctx.territories)}}]})
    elif ctx.partner_id is not None:
        levels.append({"must": [{"key": "confidentiality_level", "match": {"value": PARTNER}},
                                {"key": "partner_id", "match": {"value": ctx.partner_id}}]})
    # Территория фрагмента, если задана, должна входить в территории пользователя.
    territory = {"should": [{"is_empty": {"key": "territory_id"}},
                            {"key": "territory_id", "match": {"any": sorted(ctx.territories)}}]}
    return {"must": [{"should": levels}, territory]}


def scope_conditions(model_ref: str | None, equipment_class: str | None) -> list[dict]:
    scope = []
    if model_ref:
        scope.append({"key": "model_ref", "match": {"value": model_ref}})
    if equipment_class:
        scope.append({"key": "equipment_class", "match": {"value": equipment_class}})
    return scope


class Retriever:
    def __init__(self, qdrant: QdrantClient, embed: Callable[[list[str]], list[list[float]]], bm25: Bm25,
                 *, candidates: int = CANDIDATES):
        self._qdrant = qdrant
        self._embed = embed
        self._bm25 = bm25
        self._candidates = candidates

    def build_query(self, ctx: AuthContext, query: str, *, dense: list[float], model_ref: str | None,
                    equipment_class: str | None, limit: int) -> dict:
        query_filter = access_filter(ctx)
        query_filter["must"].extend(scope_conditions(model_ref, equipment_class))
        sparse = self._bm25.query(query)
        # Один и тот же фильтр — в обе ветви и в слияние.
        return {
            "prefetch": [
                {"query": dense, "using": "dense", "filter": query_filter, "limit": self._candidates},
                {"query": sparse.as_query(), "using": "bm25", "filter": query_filter, "limit": self._candidates},
            ],
            "query": {"fusion": "rrf"},
            "filter": query_filter,
            "limit": limit,
            "with_payload": True,
        }

    def search(self, ctx: AuthContext, query: str, *, model_ref: str | None = None,
               equipment_class: str | None = None, limit: int = 5) -> list[Fragment]:
        dense = self._embed([query])[0]
        body = self.build_query(ctx, query, dense=dense, model_ref=model_ref, equipment_class=equipment_class,
                                limit=limit)
        return [self._fragment(point) for point in self._qdrant.query(body)]

    @staticmethod
    def _fragment(point: dict) -> Fragment:
        payload = point["payload"]
        return Fragment(chunk_id=str(point.get("id", "")), document_id=payload["document_id"], title=payload["title"],
                        section_no=payload["section_no"], section_title=payload["section_title"],
                        content=payload["content"], confidentiality_level=payload["confidentiality_level"],
                        source=payload["source"], model_refs=tuple(payload.get("model_ref") or ()),
                        score=point.get("score", 0.0))
