"""Корпус согласован со справочниками и разбирается на фрагменты."""

from __future__ import annotations

import dataclasses

import pytest

from backend.knowledge.chunks import corpus_documents, split
from backend.knowledge.loader import CORPUS, CorpusError, corpus_version, validate

DOCUMENTS = corpus_documents(CORPUS)


def test_corpus_covers_both_mvp_classes_and_has_an_internal_bulletin():
    classes = {d.equipment_class for d in DOCUMENTS}
    assert classes == {"Принтеры и МФУ", "Ноутбуки"}          # PRD 4.1, расширять нельзя
    internal = [d for d in DOCUMENTS if d.confidentiality_level == "Внутренний"]
    assert internal and all(d.doc_type == "Внутренний технический бюллетень" for d in internal)


def test_corpus_matches_the_reference():
    validate(DOCUMENTS)


def test_invented_document_id_is_rejected():
    fake = dataclasses.replace(DOCUMENTS[0], doc_id="DOC-999")
    with pytest.raises(CorpusError, match="справочник"):
        validate([fake])


def test_confidentiality_must_match_the_reference():
    bulletin = next(d for d in DOCUMENTS if d.confidentiality_level == "Внутренний")
    with pytest.raises(CorpusError, match="уровень доступа"):
        validate([dataclasses.replace(bulletin, confidentiality_level="Публичный")])


def test_every_document_splits_into_chunks_with_sections():
    for document in DOCUMENTS:
        chunks = split(document)
        assert chunks, document.doc_id
        assert all(c.section_title and c.content for c in chunks)
        assert len({c.point_id for c in chunks}) == len(chunks)


def test_error_code_stays_with_its_instruction():
    hp = next(d for d in DOCUMENTS if d.doc_id == "DOC-003")
    fragment = next(c for c in split(hp) if "50.xx" in c.content)
    assert "узл" in fragment.content.lower() and "30 секунд" in fragment.content


def test_corpus_version_is_stable():
    assert corpus_version().startswith("corpus-") and corpus_version() == corpus_version()
