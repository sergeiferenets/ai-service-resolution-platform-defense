"""Предварительная квалификация гарантии (PRD, раздел 5.5).

Выполняется только по правилу W-01 — сроку от даты продажи. Остальные
правила гарантии определяет инженер при физической диагностике, поэтому
результат всегда предварительный и подлежит подтверждению.

Признак риска (PRD, раздел 5.10): упоминание падения, жидкости, вскрытия
или неоригинальных расходников отмечается для диспетчера, но вывода о
негарантийности не делается — решение принимается при осмотре.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from typing import Literal

from backend.domain.models import Equipment

RULE_ID = "W-01"


@dataclass(frozen=True)
class WarrantyResult:
    status: Literal["warranty", "paid"]
    preliminary: bool
    rule_id: str
    valid_until: dt.date
    basis: str
    risk_flags: tuple[str, ...]


def add_months(day: dt.date, months: int) -> dt.date:
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def prequalify(equipment: Equipment, today: dt.date, months_by_type: dict[str, int],
               risk_signals: frozenset[str] = frozenset()) -> WarrantyResult:
    equipment_type = equipment.model.equipment_type
    if equipment_type not in months_by_type:
        # Молча подставленный срок исказил бы квалификацию.
        raise ValueError(f"Нет гарантийного срока W-01 для типа «{equipment_type}»")
    months = months_by_type[equipment_type]
    valid_until = add_months(equipment.sale_date, months)
    status = "warranty" if today <= valid_until else "paid"
    basis = (f"{RULE_ID}: продано {equipment.sale_date.isoformat()}, срок {months} мес., "
             f"действует по {valid_until.isoformat()} — {'гарантийный' if status == 'warranty' else 'срок истёк'}")
    if risk_signals:
        basis += "; признак риска отмечен, вывод о негарантийности не делается"
    return WarrantyResult(status, True, RULE_ID, valid_until, basis, tuple(sorted(risk_signals)))
