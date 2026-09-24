"""Разбиение документа: разделы, целые таблицы, устойчивые идентификаторы."""

from __future__ import annotations

import pytest

from backend.knowledge.chunks import Document, parse_document, split

HEADER = """---
doc_id: DOC-021
title: Памятка инженера
doc_type: Внутренний технический бюллетень
confidentiality_level: Внутренний
equipment_class: Принтеры и МФУ
model_refs: [MOD-003]
source: На основе DOC-001–DOC-005
---
"""

BODY = """## Ошибки узла закрепления

| Код | Что делать |
|---|---|
| 50.2 | Выключить, дать остыть, проверить соединения печки |
| 50.4 | Проверить питание, заменить печку после диагностики |

Сброс счётчика выполняется сервисной процедурой.

## Замятия бумаги

Открыть указанную зону, извлечь лист целиком.
"""


def document(body: str = BODY) -> Document:
    return Document(doc_id="DOC-021", title="Памятка инженера", doc_type="Бюллетень",
                    confidentiality_level="Внутренний", equipment_class="Принтеры и МФУ",
                    model_refs=("MOD-003",), source="источник", body=body)


def test_sections_become_chunks():
    chunks = split(document())
    assert [c.section_title for c in chunks] == ["Ошибки узла закрепления", "Замятия бумаги"]
    assert [c.section_no for c in chunks] == [1, 2]


def test_error_code_table_is_not_split():
    # Код и что с ним делать не должны попасть в разные фрагменты.
    chunks = split(document(), max_chars=80)
    table = next(c for c in chunks if "50.2" in c.content)
    assert "50.4" in table.content and "Что делать" in table.content


def test_long_section_splits_by_paragraphs():
    body = "## Раздел\n\n" + "\n\n".join(f"Абзац {i} " + "текст " * 40 for i in range(4))
    chunks = split(document(body), max_chars=400)
    assert len(chunks) > 1 and all(c.section_title == "Раздел" for c in chunks)


def test_point_id_is_stable():
    first, again = split(document())[0], split(document())[0]
    assert first.point_id == again.point_id
    assert first.point_id != split(document())[1].point_id


def test_payload_carries_access_metadata():
    payload = split(document())[0].payload()
    assert payload["confidentiality_level"] == "Внутренний" and payload["model_ref"] == ["MOD-003"]
    assert payload["document_id"] == "DOC-021" and payload["territory_id"] is None


def test_front_matter_is_required(tmp_path):
    good = tmp_path / "doc.md"
    good.write_text(HEADER + BODY, encoding="utf-8")
    parsed = parse_document(good)
    assert (parsed.doc_id, parsed.confidentiality_level, parsed.model_refs) == ("DOC-021", "Внутренний", ("MOD-003",))

    bad = tmp_path / "bad.md"
    bad.write_text("## Раздел\nтекст", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_document(bad)
