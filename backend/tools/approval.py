"""Необходимость и уровень согласования (06_approval_rules.csv).

Автоматически проверяются правила с вычислимым условием. Остальные —
расстояние выезда (A-07), просьба клиента о срочности (A-09), подменный
фонд (A-10) — требуют сведений, которых при регистрации нет, и здесь не
оцениваются.

Уровень «Диспетчер» закрывается его подтверждением рекомендации. Уровень
«Сервис-менеджер» переводит обращение в ожидание согласования.
"""

from __future__ import annotations

from dataclasses import dataclass

DISPATCHER = "Диспетчер"
SERVICE_MANAGER = "Сервис-менеджер"
RANK = {None: 0, DISPATCHER: 1, SERVICE_MANAGER: 2}


@dataclass(frozen=True)
class ApprovalNeed:
    level: str | None
    rule_ids: tuple[str, ...]
    basis: tuple[str, ...]

    @property
    def requires_separate_approval(self) -> bool:
        return self.level == SERVICE_MANAGER


def assess_approval(*, warranty: bool, estimated_cost: int | None, part_price: int | None,
                    part_lead_time_days: int | None, product_price: int) -> ApprovalNeed:
    hits: list[tuple[str, str, str]] = []
    if warranty:
        if estimated_cost is not None and estimated_cost > product_price * 0.5:
            hits.append(("A-03", SERVICE_MANAGER,
                         f"стоимость ремонта {estimated_cost} ₽ выше 50% цены изделия {product_price} ₽"))
    else:
        hits.append(("A-08", DISPATCHER, "негарантийный ремонт"))
        if part_price is not None and part_price > 15000:
            hits.append(("A-02", SERVICE_MANAGER, f"платная запчасть {part_price} ₽ дороже 15 000 ₽"))
        elif part_price is not None and part_price > 5000:
            hits.append(("A-01", DISPATCHER, f"платная запчасть {part_price} ₽ дороже 5 000 ₽"))
        if estimated_cost is not None and estimated_cost > product_price * 0.7:
            hits.append(("A-05", SERVICE_MANAGER,
                         f"стоимость ремонта {estimated_cost} ₽ выше 70% цены изделия {product_price} ₽"))
        elif estimated_cost is not None and estimated_cost > 10000:
            hits.append(("A-04", DISPATCHER, f"смета {estimated_cost} ₽ выше 10 000 ₽"))
    if part_lead_time_days is not None and part_lead_time_days > 14:
        hits.append(("A-06", DISPATCHER, f"срок поставки запчасти {part_lead_time_days} дн. больше 14"))

    level = max((h[1] for h in hits), key=RANK.get, default=None)
    return ApprovalNeed(level, tuple(h[0] for h in hits), tuple(h[2] for h in hits))
