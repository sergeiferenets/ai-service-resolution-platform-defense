"""Право на чтение сохранённого обращения (PRD, раздел 3.3).

Здесь только решение «доступно или нет». Ограничение самого материала —
в хранилище состояния (store.recommendation_snapshot, store.context_snapshot):
уровни доступа читателя передаются в запрос, и закрытый фрагмент не покидает
источник. Готовый ответ не редактируется — то же правило, что для поиска.
"""

from __future__ import annotations

from backend.domain.access import is_staff
from backend.domain.models import AuthContext


def can_read(req: dict, ctx: AuthContext) -> bool:
    """Право на чтение обращения по текущим правам читателя.

    Территория обращения известна после идентификации; до неё обращение
    доступно автору и сотрудникам. Отзыв территории закрывает доступ и
    автору: право определяется реестром сейчас, а не в момент создания.
    """
    territory = req.get("territory_id")
    if ctx.partner_id is not None and ctx.partner_id != req.get("external_partner_id"):
        return False
    if territory is None:
        return req.get("created_by") == ctx.user_id or is_staff(ctx)
    return territory in ctx.territories
