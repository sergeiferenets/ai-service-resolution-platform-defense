"""Настройки мока учётной системы из окружения.

Секреты читаются только из файлов по переменным *_FILE — Docker secrets,
docs/security/security.md, раздел 5. Значение секрета в переменной
окружения не принимается.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _secret(name: str) -> str:
    path = os.environ.get(f"{name}_FILE")
    if not path:
        raise RuntimeError(f"Не задан {name}_FILE: секреты передаются только файлом")
    return Path(path).read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    db_name: str
    maintenance_db: str
    service_token: str


def load_settings() -> Settings:
    return Settings(
        db_host=os.environ.get("POSTGRES_HOST", "postgres"),
        db_port=int(os.environ.get("POSTGRES_PORT", "5432")),
        db_user=os.environ["POSTGRES_USER"],
        db_password=_secret("POSTGRES_PASSWORD"),
        db_name=os.environ.get("MOCK_ERP_DB", "mock_erp"),
        # База, через которую создаётся база мока, если её ещё нет.
        maintenance_db=os.environ.get("POSTGRES_DB", "postgres"),
        service_token=_secret("MOCK_ERP_TOKEN"),
    )
