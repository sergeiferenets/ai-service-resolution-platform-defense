"""Разреженная ветвь: токены, коды ошибок, стемминг, устойчивость хеша."""

from __future__ import annotations

from backend.knowledge.bm25 import Bm25, term_id

BM25 = Bm25()


def test_error_codes_survive_tokenization():
    # Код ошибки — главный ключ к разделу руководства (ADR-0003, C-1).
    assert "50.2" in BM25.tokens("Принтер выдаёт ошибку 50.2 и не печатает")
    assert "13.b2.d1" in BM25.tokens("на дисплее 13.B2.D1")
    assert "c6000" in BM25.tokens("Call service C6000")
    assert "hpl-m4103-77842" in BM25.tokens("серийный HPL-M4103-77842")


def test_stopwords_and_stemming():
    tokens = BM25.tokens("Принтеры не печатают, и на листе полосы")
    assert "принтер" in tokens and "полос" in tokens
    assert "не" not in tokens and "и" not in tokens
    # Разные формы одного слова дают один терм: иначе «полосы» не найдут «полоса».
    assert BM25.tokens("полоса")[0] == BM25.tokens("полосами")[0]


def test_query_values_are_ones_because_idf_is_on_the_server():
    sparse = BM25.query("ошибка 50.2 печка")
    assert sparse.values == [1.0] * len(sparse.values) and len(sparse.indices) == 3
    assert sparse.indices == sorted(sparse.indices)


def test_document_values_saturate_with_frequency():
    once = BM25.document("печка")
    many = BM25.document("печка " * 20)
    assert many.values[0] > once.values[0]
    assert many.values[0] < 3.0          # насыщение, а не линейный рост


def test_term_id_is_stable_between_instances():
    assert term_id("печк") == Bm25().query("печка").indices[0] == BM25.query("печка").indices[0]


def test_empty_text_gives_empty_vector():
    assert BM25.document("").indices == [] and BM25.query("  ").indices == []
