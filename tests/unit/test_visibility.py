"""Право на чтение обращения и уровни доступа читателя.

Ограничение самого материала выполняет хранилище запросом (store), поэтому
здесь проверяется решение о доступе и перечень уровней, который в этот
запрос уходит. Сам запрос — в интеграционных тестах.
"""

from __future__ import annotations

import pytest

from backend.api.visibility import can_read
from backend.domain.access import INTERNAL, PARTNER, PUBLIC, is_staff, level_visible, visible_levels
from backend.domain.models import AuthContext

DISPATCHER = AuthContext("U-002", "Диспетчер", frozenset({"TER-SPB"}))
ENGINEER_MSK = AuthContext("U-001", "Инженер", frozenset({"TER-MSK"}))
CLIENT = AuthContext("U-019", "Клиент", frozenset({"TER-SPB"}), "CUST-008")
OTHER_CLIENT = AuthContext("U-020", "Клиент", frozenset({"TER-SPB"}), "CUST-009")
REVOKED = AuthContext("U-002", "Диспетчер", frozenset())

REQUEST = {"id": "r-1", "created_by": "U-002", "territory_id": "TER-SPB", "external_partner_id": "CUST-008"}
NEW_REQUEST = {"id": "r-2", "created_by": "U-002", "territory_id": None, "external_partner_id": None}


# --- роли и уровни ---------------------------------------------------------------------------------------

def test_staff_is_role_without_partner():
    assert is_staff(DISPATCHER)
    assert not is_staff(CLIENT)
    # Служебная роль с привязкой к партнёру служебной не считается: право по данным реестра.
    assert not is_staff(AuthContext("U-050", "Инженер", frozenset({"TER-SPB"}), "CUST-008"))


def test_levels_of_a_reader():
    assert visible_levels(DISPATCHER) == [PUBLIC, INTERNAL, PARTNER]
    assert visible_levels(CLIENT) == [PUBLIC, PARTNER]
    # Роль вне перечня служебных и без партнёра: только публичное.
    assert visible_levels(AuthContext("U-099", "Наблюдатель", frozenset({"TER-SPB"}))) == [PUBLIC]


@pytest.mark.parametrize("level, staff, client", [
    (PUBLIC, True, True),
    (INTERNAL, True, False),
    (PARTNER, True, True),
])
def test_level_visibility_matches_the_list(level, staff, client):
    assert level_visible(level, DISPATCHER) is staff and (level in visible_levels(DISPATCHER)) is staff
    assert level_visible(level, CLIENT) is client and (level in visible_levels(CLIENT)) is client


def test_unknown_level_is_closed():
    assert not level_visible("Секретный", DISPATCHER)
    assert "Секретный" not in visible_levels(DISPATCHER)


# --- право на чтение -------------------------------------------------------------------------------------

def test_reader_of_own_territory():
    assert can_read(REQUEST, DISPATCHER)
    assert can_read(REQUEST, CLIENT)


def test_other_territory_is_closed():
    assert not can_read(REQUEST, ENGINEER_MSK)


def test_other_partner_is_closed():
    assert not can_read(REQUEST, OTHER_CLIENT)


def test_revoked_territory_closes_own_request():
    """Отзыв территории закрывает обращение и тому, кто его создал."""
    assert REQUEST["created_by"] == REVOKED.user_id
    assert not can_read(REQUEST, REVOKED)


def test_author_reads_request_before_identification():
    assert can_read(NEW_REQUEST, DISPATCHER)
    assert not can_read(NEW_REQUEST, CLIENT)
