"""Настройки платформы из окружения.

Секреты читаются только из файлов по переменным *_FILE — Docker secrets,
docs/security/security.md, раздел 5. Значение секрета в переменной
окружения не принимается.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


def _secret(name: str) -> str:
    path = os.environ.get(f"{name}_FILE")
    if not path:
        raise RuntimeError(f"Не задан {name}_FILE: секреты передаются только файлом")
    return Path(path).read_text(encoding="utf-8").strip()


def _optional_secret(name: str) -> str | None:
    return _secret(name) if os.environ.get(f"{name}_FILE") else None


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    db_name: str
    maintenance_db: str
    mock_erp_url: str
    mock_erp_token: str
    erp_base_url: str
    code_commit: str
    # Сервис инференса (ADR-0001, ADR-0005). Ключ обязателен только там, где
    # вызываются модели: тестам на заглушках он не нужен.
    llm_base_url: str
    llm_model: str
    llm_model_id: str
    vision_base_url: str
    vision_model: str
    vision_model_id: str
    vllm_api_key: str | None
    # Поиск по корпусу (ADR-0003): одна коллекция на обе ветви.
    embed_base_url: str
    embed_model: str
    embed_model_id: str
    qdrant_url: str
    qdrant_collection: str
    qdrant_api_key: str | None
    bm25_language: str
    llm_model_revision: str | None = None
    vision_model_revision: str | None = None
    embed_model_revision: str | None = None


def model_revision(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None  # Служебные команды без инференса могут работать без моделей.
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError(f"{name}: требуется immutable Hugging Face commit SHA (40 hex)")
    return value


def load_settings() -> Settings:
    return Settings(
        db_host=os.environ.get("POSTGRES_HOST", "postgres"),
        db_port=int(os.environ.get("POSTGRES_PORT", "5432")),
        db_user=os.environ["POSTGRES_USER"],
        db_password=_secret("POSTGRES_PASSWORD"),
        # PLATFORM_DB переопределяет базу для тестов: прогон не трогает рабочую.
        db_name=os.environ.get("PLATFORM_DB") or os.environ.get("POSTGRES_DB", "servicedesk"),
        # База, через которую создаётся тестовая, если её ещё нет.
        maintenance_db=os.environ.get("POSTGRES_DB", "postgres"),
        mock_erp_url=os.environ.get("MOCK_ERP_URL", "http://mock-erp:8100"),
        mock_erp_token=_secret("MOCK_ERP_TOKEN"),
        erp_base_url=os.environ.get("ERP_BASE_URL", "http://mock-erp:8100/sap/opu/odata4/sap/zai_service/0001/"),
        # Коммит кода входит в версию набора артефактов; задаётся при сборке образа.
        code_commit=os.environ.get("CODE_COMMIT", "unknown"),
        llm_base_url=os.environ.get("LLM_BASE_URL", "http://vllm-llm:8000/v1"),
        llm_model=os.environ.get("LLM_SERVED_NAME", "qwen3-8b"),
        llm_model_id=os.environ.get("LLM_MODEL_ID", "Qwen/Qwen3-8B-AWQ"),
        vision_base_url=os.environ.get("VISION_BASE_URL", "http://vllm-vision:8000/v1"),
        vision_model=os.environ.get("VISION_SERVED_NAME", "qwen2.5-vl-7b"),
        vision_model_id=os.environ.get("VISION_MODEL_ID", "Qwen/Qwen2.5-VL-7B-Instruct-AWQ"),
        vllm_api_key=_optional_secret("VLLM_API_KEY"),
        embed_base_url=os.environ.get("EMBEDDINGS_BASE_URL", "http://vllm-embeddings:8000/v1"),
        embed_model=os.environ.get("EMBED_SERVED_NAME", "bge-m3"),
        embed_model_id=os.environ.get("EMBED_MODEL_ID", "BAAI/bge-m3"),
        qdrant_url=os.environ.get("QDRANT_URL", "http://qdrant:6333"),
        qdrant_collection=os.environ.get("QDRANT_COLLECTION", "doc_chunks"),
        qdrant_api_key=_optional_secret("QDRANT_API_KEY"),
        bm25_language=os.environ.get("BM25_LANGUAGE", "russian"),
        llm_model_revision=model_revision("LLM_MODEL_REVISION"),
        vision_model_revision=model_revision("VISION_MODEL_REVISION"),
        embed_model_revision=model_revision("EMBED_MODEL_REVISION"),
    )
