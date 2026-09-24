"""Гибридный поиск против живых Qdrant и модели представлений (ADR-0003).

Окружение: QDRANT_URL, QDRANT_API_KEY_FILE, QDRANT_COLLECTION,
EMBEDDINGS_BASE_URL, EMBED_SERVED_NAME, VLLM_API_KEY_FILE. Корпус
загружается фикстурой: загрузка идемпотентна.
"""

from __future__ import annotations

import pytest

from backend.config import load_settings
from backend.domain.models import AuthContext
from backend.knowledge.bm25 import Bm25
from backend.knowledge.loader import clients, load
from backend.knowledge.search import INTERNAL, PUBLIC, Retriever

DISPATCHER = AuthContext("U-002", "Диспетчер", frozenset({"TER-SPB"}))
PARTNER_REP = AuthContext("U-019", "Клиент", frozenset({"TER-SPB"}), "CUST-008")


@pytest.fixture(scope="module")
def retriever():
    settings = load_settings()
    qdrant, embeddings = clients(settings)
    bm25 = Bm25(settings.bm25_language)
    load(qdrant, embeddings.embed, bm25)
    return Retriever(qdrant, embeddings.embed, bm25)


def documents(fragments) -> list[str]:
    return [f.document_id for f in fragments]


def test_exact_error_code_is_found(retriever):
    # Разреженная ветвь: редкий токен кода ошибки (ADR-0003, C-1). Код берётся
    # из корпуса: по коду, которого в документах нет, разреженная ветвь пуста,
    # и проверка превращается в проверку случайного порядка плотной ветви.
    found = retriever.search(DISPATCHER, "10.00.10 чип картриджа не читается", limit=5)
    assert documents(found)[0] == "DOC-003", documents(found)


def test_free_description_is_found(retriever):
    # Плотная ветвь: слова обращения не совпадают со словами руководства (C-2).
    found = retriever.search(DISPATCHER, "аппарат не захватывает бумагу, лист мнётся каждый раз", limit=5)
    assert found and any(d in documents(found) for d in ("DOC-003", "DOC-021", "DOC-002"))


def test_reference_case_puts_the_needed_source_in_the_top_five(retriever):
    found = retriever.search(DISPATCHER, "HP LaserJet, ошибка 50.2, не печатает", limit=5)
    assert "DOC-003" in documents(found)
    assert all(f.reference.startswith(f.document_id) for f in found)


def test_partner_representative_never_gets_an_internal_bulletin(retriever):
    query = "ошибка 50.2, узел закрепления, что делать"
    staff = retriever.search(DISPATCHER, query, limit=10)
    partner = retriever.search(PARTNER_REP, query, limit=10)
    assert INTERNAL in {f.confidentiality_level for f in staff}
    assert "DOC-021" in documents(staff)
    assert {f.confidentiality_level for f in partner} == {PUBLIC}
    assert "DOC-021" not in documents(partner)


def test_scope_narrows_to_the_model(retriever):
    found = retriever.search(DISPATCHER, "замятие бумаги", model_ref="MOD-004", limit=5)
    assert found and all("MOD-004" in f.model_refs for f in found)


def test_laptop_and_printer_classes_do_not_mix(retriever):
    found = retriever.search(DISPATCHER, "не включается, нет индикации", equipment_class="Ноутбуки", limit=5)
    assert found and {d for d in documents(found)} <= {"DOC-006", "DOC-009", "DOC-022"}
