"""Документ корпуса и разбиение на фрагменты (ADR-0003).

Разбиение — по разделам документа. Строки таблиц кодов ошибок сохраняются
целиком: код ошибки и что с ним делать не должны оказаться в разных
фрагментах, иначе поиск по коду вернёт фрагмент без ответа.

Файл корпуса — Markdown с заголовком в формате YAML: идентификатор документа,
уровень конфиденциальности, класс оборудования, модели, источник. Значения
проверяются загрузчиком по справочникам: выдуманный идентификатор документа
в корпус не попадает.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

NAMESPACE = uuid.UUID("6f0a4b0e-3d1c-4a2b-9f6e-2b7a1c5d8e30")
SECTION = re.compile(r"^##\s+(.*)$", re.M)
MAX_CHARS = 1200


@dataclass(frozen=True)
class Document:
    doc_id: str
    title: str
    doc_type: str
    confidentiality_level: str
    equipment_class: str
    model_refs: tuple[str, ...]
    source: str
    body: str
    territory_id: str | None = None
    partner_id: str | None = None


@dataclass(frozen=True)
class Chunk:
    document: Document
    section_no: int
    section_title: str
    content: str

    @property
    def point_id(self) -> str:
        return str(uuid.uuid5(NAMESPACE, f"{self.document.doc_id}:{self.section_no}"))

    def payload(self) -> dict:
        d = self.document
        return {"document_id": d.doc_id, "title": d.title, "doc_type": d.doc_type,
                "confidentiality_level": d.confidentiality_level, "equipment_class": d.equipment_class,
                "model_ref": list(d.model_refs), "source": d.source, "territory_id": d.territory_id,
                "partner_id": d.partner_id, "section_no": self.section_no, "section_title": self.section_title,
                "content": self.content}


def parse_document(path: Path) -> Document:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{path.name}: нет заголовка YAML")
    _, header, body = text.split("---", 2)
    meta = yaml.safe_load(header)
    missing = [k for k in ("doc_id", "title", "doc_type", "confidentiality_level", "equipment_class", "source")
               if not meta.get(k)]
    if missing:
        raise ValueError(f"{path.name}: в заголовке нет полей: {', '.join(missing)}")
    return Document(doc_id=meta["doc_id"], title=meta["title"], doc_type=meta["doc_type"],
                    confidentiality_level=meta["confidentiality_level"], equipment_class=meta["equipment_class"],
                    model_refs=tuple(meta.get("model_refs") or ()), source=meta["source"],
                    territory_id=meta.get("territory_id"), partner_id=meta.get("partner_id"),
                    body=body.strip())


def _blocks(section_body: str) -> list[str]:
    """Абзацы и таблицы. Таблица — подряд идущие строки со знаком «|» —
    остаётся одним блоком и не делится между фрагментами."""
    blocks: list[str] = []
    current: list[str] = []
    in_table = False
    for line in section_body.splitlines():
        is_row = line.lstrip().startswith("|")
        if not line.strip() and not in_table:
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        if is_row != in_table and current:
            blocks.append("\n".join(current).strip())
            current = []
        in_table = is_row
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


def split(document: Document, *, max_chars: int = MAX_CHARS) -> list[Chunk]:
    parts = SECTION.split(document.body)
    # До первого заголовка — вводная часть документа.
    sections: list[tuple[str, str]] = []
    if parts[0].strip():
        sections.append((document.title, parts[0].strip()))
    sections += [(title.strip(), body.strip()) for title, body in zip(parts[1::2], parts[2::2])]

    chunks: list[Chunk] = []
    for title, body in sections:
        buffer: list[str] = []
        size = 0
        for block in _blocks(body):
            if buffer and size + len(block) > max_chars:
                chunks.append(Chunk(document, len(chunks) + 1, title, "\n\n".join(buffer)))
                buffer, size = [], 0
            buffer.append(block)
            size += len(block)
        if buffer:
            chunks.append(Chunk(document, len(chunks) + 1, title, "\n\n".join(buffer)))
    return chunks


def corpus_documents(corpus_dir: Path) -> list[Document]:
    return [parse_document(p) for p in sorted(corpus_dir.glob("*.md"))]
