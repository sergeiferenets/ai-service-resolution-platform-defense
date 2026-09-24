"""Гибридный запрос: две ветви, слияние RRF, единый фильтр прав (ADR-0003)."""

from __future__ import annotations

import json

import httpx
import pytest

from backend.domain.models import AuthContext
from backend.knowledge.bm25 import Bm25
from backend.knowledge.qdrant import QdrantClient, QdrantEndpoint, RetrievalError
from backend.knowledge.search import INTERNAL, PARTNER, PUBLIC, Retriever, access_filter

DISPATCHER = AuthContext("U-002", "Диспетчер", frozenset({"TER-SPB"}))
ENGINEER = AuthContext("U-017", "Инженер", frozenset({"TER-SPB"}))
PARTNER_REP = AuthContext("U-019", "Клиент", frozenset({"TER-SPB"}), "CUST-008")


def levels(ctx) -> set[str]:
    return {branch["must"][0]["match"]["value"] for branch in access_filter(ctx)["must"][0]["should"]}


def test_partner_representative_sees_only_public_and_own_partner_documents():
    assert levels(PARTNER_REP) == {PUBLIC, PARTNER}
    partner_branch = next(b for b in access_filter(PARTNER_REP)["must"][0]["should"]
                          if b["must"][0]["match"]["value"] == PARTNER)
    assert partner_branch["must"][1] == {"key": "partner_id", "match": {"value": "CUST-008"}}


@pytest.mark.parametrize("ctx", [DISPATCHER, ENGINEER])
def test_staff_sees_internal_bulletins(ctx):
    assert levels(ctx) == {PUBLIC, INTERNAL, PARTNER}


def test_territory_of_a_fragment_must_match_the_user():
    territory = access_filter(DISPATCHER)["must"][1]["should"]
    assert territory[0] == {"is_empty": {"key": "territory_id"}}
    assert territory[1] == {"key": "territory_id", "match": {"any": ["TER-SPB"]}}


def retriever(handler=None) -> tuple[Retriever, list]:
    calls: list[dict] = []

    def default(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"result": {"points": [{"score": 0.5, "payload": {
            "document_id": "DOC-003", "title": "Руководство", "section_no": 2, "section_title": "Ошибки",
            "content": "50.2 — узел закрепления", "confidentiality_level": PUBLIC,
            "source": "https://example", "model_ref": ["MOD-003"]}}]}})

    client = QdrantClient(QdrantEndpoint("http://qdrant:6333", "key", "doc_chunks"),
                          http=httpx.Client(transport=httpx.MockTransport(handler or default)))
    return Retriever(client, lambda texts: [[0.1, 0.2, 0.3] for _ in texts], Bm25()), calls


def test_both_branches_run_and_are_merged_by_rrf():
    search, calls = retriever()
    fragments = search.search(DISPATCHER, "ошибка 50.2, не печатает", limit=5)
    body = calls[0]
    assert [p["using"] for p in body["prefetch"]] == ["dense", "bm25"]
    assert body["query"] == {"fusion": "rrf"} and body["limit"] == 5
    assert body["prefetch"][0]["query"] == [0.1, 0.2, 0.3]
    assert body["prefetch"][1]["query"]["indices"] and body["prefetch"][1]["query"]["values"] == [1.0, 1.0, 1.0]
    assert fragments[0].reference == "DOC-003#2"


def test_one_and_the_same_filter_goes_into_both_branches_and_the_fusion():
    search, calls = retriever()
    search.search(PARTNER_REP, "не печатает")
    body = calls[0]
    expected = access_filter(PARTNER_REP)
    assert body["filter"] == expected
    assert [p["filter"] for p in body["prefetch"]] == [expected, expected]


def test_scope_narrows_by_model_and_class_without_touching_access():
    search, calls = retriever()
    search.search(DISPATCHER, "50.2", model_ref="MOD-003", equipment_class="Принтеры и МФУ")
    conditions = calls[0]["filter"]["must"]
    assert {"key": "model_ref", "match": {"value": "MOD-003"}} in conditions
    assert {"key": "equipment_class", "match": {"value": "Принтеры и МФУ"}} in conditions
    assert conditions[:2] == access_filter(DISPATCHER)["must"]


def test_storage_failure_is_a_refusal_not_an_empty_result():
    search, _ = retriever(lambda request: httpx.Response(503, text="unavailable"))
    with pytest.raises(RetrievalError):
        search.search(DISPATCHER, "50.2")

    def timeout(request):
        raise httpx.ReadTimeout("slow")
    search, _ = retriever(timeout)
    with pytest.raises(RetrievalError):
        search.search(DISPATCHER, "50.2")
