"""Загрузка корпуса знаний в векторное хранилище (ADR-0003).

Корпус — data/corpus/*.md. Идентификатор документа, уровень доступа и модели
сверяются со справочниками: документ с выдуманным идентификатором или с
уровнем доступа, не совпадающим со справочником, в корпус не попадает.
Класс оборудования ограничен классами MVP (PRD, раздел 4.1).

Фрагменты получают устойчивые идентификаторы точек, поэтому повторная
загрузка обновляет корпус, а не плодит дубликаты. Разделы, исчезнувшие из
документа, удаляются перед записью его новой редакции.

    python -m backend.knowledge.loader
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

from backend.config import Settings, load_settings
from backend.knowledge.bm25 import Bm25
from backend.knowledge.chunks import Chunk, Document, corpus_documents, split
from backend.knowledge.qdrant import LOAD_BUDGET_S, QdrantClient, QdrantEndpoint
from backend.llm.client import Endpoint, LlmClient
from data.loader.reference import REF

CORPUS = REF.parent / "corpus"
EQUIPMENT_CLASSES = {"Принтеры и МФУ", "Ноутбуки"}
BATCH = 16


class CorpusError(Exception):
    """Корпус противоречит справочникам."""


def corpus_version(corpus_dir: Path = CORPUS) -> str:
    digest = hashlib.sha256()
    for path in sorted(corpus_dir.glob("*.md")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return f"corpus-{digest.hexdigest()[:12]}"


def _reference(name: str, key: str) -> dict[str, dict[str, str]]:
    with (REF / name).open(encoding="utf-8") as f:
        return {row[key]: row for row in csv.DictReader(f)}


def validate(documents: list[Document]) -> None:
    known_docs = _reference("10_documents.csv", "doc_id")
    known_models = _reference("01_models.csv", "model_id")
    problems: list[str] = []
    for d in documents:
        row = known_docs.get(d.doc_id)
        if row is None:
            problems.append(f"{d.doc_id}: нет в справочнике документов")
            continue
        if row["Уровень доступа"] != d.confidentiality_level:
            problems.append(f"{d.doc_id}: уровень доступа «{d.confidentiality_level}» "
                            f"не совпадает со справочником («{row['Уровень доступа']}»)")
        if d.equipment_class not in EQUIPMENT_CLASSES:
            problems.append(f"{d.doc_id}: класс оборудования «{d.equipment_class}» вне классов MVP")
        problems += [f"{d.doc_id}: модель {m} не найдена" for m in d.model_refs if m not in known_models]
    if problems:
        raise CorpusError("Корпус не прошёл проверку:\n  " + "\n  ".join(problems))


def points(chunks: list[Chunk], dense: list[list[float]], bm25: Bm25) -> list[dict]:
    return [{"id": chunk.point_id,
             "vector": {"dense": vector, "bm25": bm25.document(chunk.content).as_query()},
             "payload": chunk.payload()}
            for chunk, vector in zip(chunks, dense)]


def load(qdrant: QdrantClient, embed, bm25: Bm25, corpus_dir: Path = CORPUS) -> dict:
    documents = corpus_documents(corpus_dir)
    if not documents:
        raise CorpusError(f"В {corpus_dir} нет документов корпуса")
    validate(documents)

    loaded = {"документов": len(documents), "фрагментов": 0}
    for document in documents:
        chunks = split(document)
        vectors: list[list[float]] = []
        for start in range(0, len(chunks), BATCH):
            vectors += embed([c.content for c in chunks[start:start + BATCH]])
        qdrant.delete_document(document.doc_id)
        qdrant.upsert(points(chunks, vectors, bm25))
        loaded["фрагментов"] += len(chunks)
        print(f"{document.doc_id}: {len(chunks):3d} фрагментов — {document.title}")
    return loaded


def clients(settings: Settings) -> tuple[QdrantClient, LlmClient]:
    qdrant = QdrantClient(QdrantEndpoint(settings.qdrant_url, settings.qdrant_api_key or "",
                                         settings.qdrant_collection), timeout_s=LOAD_BUDGET_S)
    embeddings = LlmClient(Endpoint(settings.embed_base_url, settings.vllm_api_key or "",
                                    settings.embed_model, LOAD_BUDGET_S))
    return qdrant, embeddings


def main() -> None:
    settings = load_settings()
    qdrant, embeddings = clients(settings)
    counts = load(qdrant, embeddings.embed, Bm25(settings.bm25_language))
    total = qdrant.count()
    print("Корпус загружен: " + ", ".join(f"{k} {v}" for k, v in counts.items())
          + f"; точек в коллекции {total}; версия {corpus_version()}")


if __name__ == "__main__":
    main()
