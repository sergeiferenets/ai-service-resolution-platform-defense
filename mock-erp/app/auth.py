"""Сервисный токен, контекст авторизации и проверка прав в источнике.

Правило ADR-0006: объект вне зоны ответственности — отказ, а не пустой
результат, и событие в аудите. Пустой результат неотличим от отсутствия
данных и скрывает ошибку разграничения.

Контекст авторизации приходит заголовками X-Auth-User и X-Auth-Territories.
Действующие территории — пересечение присланных с реестром прав учётной
системы: подменить территорию в заголовке нельзя.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request

ROLES_CREATING_DRAFTS = {"Диспетчер", "Сервис-менеджер"}


@dataclass(frozen=True)
class Caller:
    user_id: str
    role: str
    partner_id: str | None
    claimed: tuple[str, ...]
    effective: frozenset[str]


def require_service_token(request: Request, authorization: str | None = Header(default=None)) -> None:
    expected = request.app.state.settings.service_token
    presented = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий сервисный токен")


def audit(request: Request, *, event_type: str, caller: Caller | None, user_id: str | None = None,
          object_type: str | None = None, object_id: str | None = None,
          object_territory_id: str | None = None, detail: str | None = None) -> None:
    """Событие пишется своей транзакцией: откат запроса не должен его стереть."""
    with request.app.state.pool.connection() as conn:
        conn.execute(
            "INSERT INTO audit_event (event_type, user_id, endpoint, object_type, object_id,"
            " object_territory_id, claimed_territories, effective_territories, detail)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (event_type, caller.user_id if caller else user_id, request.url.path, object_type, object_id,
             object_territory_id, list(caller.claimed) if caller else [],
             sorted(caller.effective) if caller else [], detail),
        )


def deny(request: Request, caller: Caller | None, *, reason: str, object_type: str | None = None,
         object_id: str | None = None, object_territory_id: str | None = None,
         user_id: str | None = None) -> None:
    audit(request, event_type="access_denied", caller=caller, user_id=user_id, object_type=object_type,
          object_id=object_id, object_territory_id=object_territory_id, detail=reason)
    raise HTTPException(status_code=403, detail={
        "error": "access_denied", "reason": reason, "object_type": object_type, "object_id": object_id,
    })


def get_caller(request: Request, x_auth_user: str | None = Header(default=None),
               x_auth_territories: str | None = Header(default=None)) -> Caller:
    if not x_auth_user or x_auth_territories is None:
        raise HTTPException(
            status_code=400,
            detail="Нет контекста авторизации: заголовки X-Auth-User и X-Auth-Territories обязательны",
        )
    claimed = tuple(sorted({t.strip() for t in x_auth_territories.split(",") if t.strip()}))
    with request.app.state.pool.connection() as conn:
        user = conn.execute("SELECT role, partner_id FROM app_user WHERE user_id = %s", (x_auth_user,)).fetchone()
        registry = {r[0] for r in conn.execute(
            "SELECT territory_id FROM user_territory WHERE user_id = %s", (x_auth_user,))}
    if user is None:
        deny(request, None, reason="пользователь неизвестен учётной системе", user_id=x_auth_user)
    return Caller(user_id=x_auth_user, role=user[0], partner_id=user[1], claimed=claimed,
                  effective=frozenset(claimed) & registry)


def check_object(request: Request, caller: Caller, *, object_type: str, object_id: str,
                 territory_id: str, partner_id: str | None) -> None:
    if territory_id not in caller.effective:
        deny(request, caller, reason="объект другой территории", object_type=object_type,
             object_id=object_id, object_territory_id=territory_id)
    if caller.partner_id is not None and partner_id != caller.partner_id:
        deny(request, caller, reason="объект другого партнёра", object_type=object_type,
             object_id=object_id, object_territory_id=territory_id)


def check_territory(request: Request, caller: Caller, territory_id: str, what: str) -> None:
    if territory_id not in caller.effective:
        deny(request, caller, reason=f"{what}: территория вне зоны ответственности",
             object_type="territory", object_id=territory_id, object_territory_id=territory_id)
