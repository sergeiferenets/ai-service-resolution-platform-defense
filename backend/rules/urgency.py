"""Операционная срочность — отдельный инструмент (ADR-0004, раздел 3).

Рассчитывается после выбора технического маршрута и на маршрут не влияет.
Одинаковый код ошибки на резервном устройстве и на единственном аппарате,
обеспечивающем отгрузку документов, даёт одинаковый маршрут и разную
срочность.

Входы:
- urgency_hint правила — 1 самый срочный, 3 наименее;
- влияние на деятельность партнёра — сообщается при регистрации;
- время реакции по договору, если договор есть.

Уровень: сумма баллов подсказки (1→2, 2→1, 3→0) и влияния (высокое→2,
среднее→1, низкое→0, не указано→1). 4 — P1, 3 — P2, 2 — P3, меньше — P4.
Целевое время реакции берётся из договора; без договора — по уровню.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Impact = Literal["высокое", "среднее", "низкое"]

HINT_POINTS = {1: 2, 2: 1, 3: 0}
IMPACT_POINTS = {"высокое": 2, "среднее": 1, "низкое": 0}
LEVEL_BY_SCORE = {4: "P1", 3: "P2", 2: "P3"}
DEFAULT_REACTION_HOURS = {"P1": 4, "P2": 8, "P3": 24, "P4": 48}


@dataclass(frozen=True)
class Urgency:
    level: str
    reaction_target_hours: int
    basis: tuple[str, ...]


def compute_urgency(urgency_hint: int | None, impact: Impact | None,
                    contract_reaction_hours: int | None) -> Urgency:
    hint_points = HINT_POINTS.get(urgency_hint, 1)
    impact_points = IMPACT_POINTS.get(impact, 1)
    level = LEVEL_BY_SCORE.get(hint_points + impact_points, "P4")
    basis = [
        f"подсказка правила {urgency_hint if urgency_hint is not None else 'не задана'} → {hint_points}",
        f"влияние на партнёра {impact or 'не указано'} → {impact_points}",
    ]
    if contract_reaction_hours is not None:
        target = contract_reaction_hours
        basis.append(f"время реакции по договору {contract_reaction_hours} ч")
    else:
        target = DEFAULT_REACTION_HOURS[level]
        basis.append(f"договора нет: время реакции по уровню {level} — {target} ч")
    return Urgency(level, target, tuple(basis))
