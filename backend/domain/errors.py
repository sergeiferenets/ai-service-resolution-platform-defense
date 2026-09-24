"""Доменные ошибки обращения к источнику фактов."""

from __future__ import annotations


class SourceError(Exception):
    """Базовая ошибка источника фактов."""


class AccessDenied(SourceError):
    """Объект вне зоны ответственности пользователя: отказ, а не пустой результат."""

    def __init__(self, reason: str, object_type: str | None = None, object_id: str | None = None):
        super().__init__(f"{reason}: {object_type} {object_id}")
        self.reason = reason
        self.object_type = object_type
        self.object_id = object_id


class NotFound(SourceError):
    pass


class IdempotencyConflict(SourceError):
    """Ключ идемпотентности уже использован с другим содержимым."""


class SourceUnavailable(SourceError):
    """Бюджет времени и допустимые повторы исчерпаны (ADR-0007)."""


class SourceContractError(SourceError):
    """Источник ответил не по контракту: ошибка конфигурации или интеграции."""


class ExecutionNotAuthorized(Exception):
    """Запись в учётную систему без сохранённого разрешения на исполнение.

    Разрешение выдаётся только решением человека (ADR-0006, PRD 5.1):
    подтверждением рекомендации или согласованием. Его отсутствие или
    несовпадение с текущим решением — отказ до обращения к источнику.
    """
