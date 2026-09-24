"""Версия набора артефактов (data-model.md, ARTIFACT_VERSION).

Результат зависит не только от кода, но и от моделей, промптов, свода
правил, корпуса и параметров поиска. Идентификатор версии — хеш всех полей:
изменение любого артефакта порождает новую версию, и каждый шаг процесса
ссылается на ту, под которой выполнен.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass

from backend.agents.stubs import STUB_REVISION

NOT_IN_SLICE = "нет в срезе"


@dataclass(frozen=True)
class ArtifactVersion:
    code_commit: str
    llm_model_revision: str
    vision_model_revision: str
    embedding_model_revision: str
    prompt_version: str
    rules_version: str
    corpus_version: str
    chunker_version: str
    retrieval_config_version: str

    @property
    def id(self) -> str:
        payload = json.dumps(dataclasses.asdict(self), sort_keys=True, ensure_ascii=False)
        return "av-" + hashlib.sha256(payload.encode()).hexdigest()[:12]


def composite_rules_version(*digests: str) -> str:
    """rules_version: свод правил, справочник кодов ошибок, словари входного
    контроля (data-model.md, раздел 3). Любое изменение одного из них меняет версию."""
    return "rules-" + hashlib.sha256("|".join(digests).encode()).hexdigest()[:12]


def build_artifacts(*, code_commit: str, rules_version: str, llm_model_revision: str, vision_model_revision: str,
                    prompt_versions: dict[str, str], embedding_model_revision: str = STUB_REVISION,
                    corpus_version: str = NOT_IN_SLICE, chunker_version: str = NOT_IN_SLICE,
                    retrieval_config_version: str = NOT_IN_SLICE) -> ArtifactVersion:
    """prompt_version — по каждому агенту и инструменту с моделью, заглушка —
    `stub` (data-model.md, раздел 3). Версия корпуса и параметры поиска входят
    в набор наравне с моделями: от них зависит, какие фрагменты попадут
    в контекст, а значит и результат разбора."""
    for revision in (llm_model_revision, vision_model_revision, embedding_model_revision):
        if revision != STUB_REVISION and not re.fullmatch(r"[0-9a-f]{40}", revision or ""):
            raise ValueError("Ревизия модели должна быть immutable commit SHA, а не именем репозитория")
    return ArtifactVersion(
        code_commit=code_commit,
        llm_model_revision=llm_model_revision,
        vision_model_revision=vision_model_revision,
        embedding_model_revision=embedding_model_revision,
        prompt_version="; ".join(f"{name}:{version}" for name, version in prompt_versions.items()),
        rules_version=rules_version,
        corpus_version=corpus_version,
        chunker_version=chunker_version,
        retrieval_config_version=retrieval_config_version,
    )


def slice_artifacts(code_commit: str, rules_version: str) -> ArtifactVersion:
    """Срез без языковой модели: вместо ревизий моделей и промптов — ревизия
    заглушек. Шаг, выполненный заглушкой, отличим по версии в любой записи."""
    return ArtifactVersion(
        code_commit=code_commit,
        llm_model_revision=STUB_REVISION,
        vision_model_revision=STUB_REVISION,
        embedding_model_revision=STUB_REVISION,
        prompt_version=STUB_REVISION,
        rules_version=rules_version,
        corpus_version=NOT_IN_SLICE,
        chunker_version=NOT_IN_SLICE,
        retrieval_config_version=NOT_IN_SLICE,
    )
