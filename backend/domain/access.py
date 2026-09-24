"""Правило видимости: роли, уровни конфиденциальности, территории (PRD 3.3).

Единственное место, где записано, кому какой уровень доступен. Им
пользуются и поиск (фильтр в запросе к хранилищу векторов, ADR-0003), и
чтение сохранённого обращения: иначе материал, закрытый в выдаче, может
оказаться открыт в сохранённом ответе.

Служебная роль — сотрудник сервисной организации без привязки к партнёру.
Представитель партнёра узнаётся по заполненному partner_id, а не по
названию роли: право определяется данными реестра, а не строкой.
"""

from __future__ import annotations

from backend.domain.models import AuthContext

PUBLIC = "Публичный"
INTERNAL = "Внутренний"
PARTNER = "Партнёрский"
STAFF_ROLES = frozenset({"Инженер", "Диспетчер", "Сервис-менеджер", "Администратор"})


def is_staff(ctx: AuthContext) -> bool:
    return ctx.role in STAFF_ROLES and ctx.partner_id is None


def visible_levels(ctx: AuthContext) -> list[str]:
    """Уровни конфиденциальности, доступные читателю.

    Передаётся в запрос к хранилищу состояния: закрытый материал не покидает
    источник, а не отбрасывается из готового ответа (ADR-0003, то же правило,
    что и для поиска).
    """
    if is_staff(ctx):
        return [PUBLIC, INTERNAL, PARTNER]
    if ctx.partner_id is not None:
        return [PUBLIC, PARTNER]
    return [PUBLIC]


def level_visible(level: str | None, ctx: AuthContext) -> bool:
    """Доступен ли уровень конфиденциальности этому пользователю.

    Уровень «Партнёрский» проверяется здесь только по роли: принадлежность
    документа партнёру и территории проверяет фильтр поиска, у которого есть
    поля фрагмента. При чтении сохранённого обращения этого достаточно:
    партнёрский материал по чужому партнёру в его обращение не попадает.
    """
    if level is None or level == PUBLIC:
        return True
    if level == INTERNAL:
        return is_staff(ctx)
    if level == PARTNER:
        return is_staff(ctx) or ctx.partner_id is not None
    return False
