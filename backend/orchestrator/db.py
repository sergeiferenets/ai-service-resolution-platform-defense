"""Подключение к хранилищу состояния и миграции схемы платформы."""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from backend.config import Settings

MIGRATIONS = Path(__file__).parent / "migrations"


def conninfo(s: Settings, dbname: str | None = None) -> str:
    return make_conninfo(host=s.db_host, port=s.db_port, user=s.db_user, password=s.db_password,
                         dbname=dbname or s.db_name)


def ensure_database(s: Settings) -> None:
    """Рабочую базу создаёт контейнер PostgreSQL; нужна только для тестовой."""
    with psycopg.connect(conninfo(s, s.maintenance_db), autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (s.db_name,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(s.db_name)))


def migrate(s: Settings) -> list[str]:
    applied_now: list[str] = []
    with psycopg.connect(conninfo(s)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.stem in applied:
                continue
            conn.execute(f.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.stem,))
            applied_now.append(f.stem)
    return applied_now


def make_pool(s: Settings) -> ConnectionPool:
    return ConnectionPool(conninfo(s), min_size=1, max_size=8, open=True)
