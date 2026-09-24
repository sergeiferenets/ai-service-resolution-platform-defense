"""Тип исполнения, сервисный центр и кандидаты в исполнители (PRD, раздел 5.4).

Детерминированный инструмент: способ выполнения уже выбран правилом, здесь
по проверяемым признакам определяются вторая ось — внутреннее или внешнее
исполнение — и конкретный исполнитель.

Порядок проверок кандидата: территория, авторизация по бренду (для
гарантийного случая), компетенция, доступность в окне, наличие запчасти.
Первая не пройденная проверка даёт код причины отклонения из закрытого
перечня — не свободный текст (data-model.md, CANDIDATE).

Авторизация по бренду — свойство собственной мастерской. Гарантийный случай
исполняется внутри, только если в территории есть собственная мастерская,
авторизованная по бренду. Иначе работа уходит во внешний авторизованный
центр, даже при свободном собственном инженере.

Цепочка приоритетов назначения внутри выбранного типа: исполнитель из
договора, исполнитель из карточки оборудования, ранжирование.

Запчасти проверяются только для внутреннего исполнения: внешний центр
обеспечивает их сам.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from backend.domain.models import ServiceCenter, Technician

NO_BRAND = "нет авторизации по бренду"
NO_SKILL = "нет компетенции"
OTHER_TERRITORY = "другая территория"
UNAVAILABLE = "недоступен в окно"
NO_PART = "нет запчасти"
REJECTION_REASONS = (NO_BRAND, NO_SKILL, OTHER_TERRITORY, UNAVAILABLE, NO_PART)

TECHNICIAN = "исполнитель"
CENTER = "сервисный центр"


@dataclass(frozen=True)
class Candidate:
    candidate_type: Literal["исполнитель", "сервисный центр"]
    candidate_id: str
    rank: int | None
    selection_basis: Literal["договор", "карточка ИО", "ранжирование"] | None
    rejection_reason: str | None
    rationale: str


@dataclass(frozen=True)
class FulfillmentDecision:
    fulfillment_type: Literal["internal", "external"] | None
    service_center_id: str | None
    technician_id: str | None
    candidates: tuple[Candidate, ...]
    basis: str


def _center_rejection(c: ServiceCenter, *, territory_id: str, warranty: bool, brand: str,
                      required_skill: str | None, part_available: bool | None) -> str | None:
    if c.territory_id != territory_id:
        return OTHER_TERRITORY
    if warranty and brand not in c.brands:
        return NO_BRAND
    if required_skill and required_skill not in c.skill_ids:
        return NO_SKILL
    if c.is_internal and part_available is False:
        return NO_PART
    return None


def _technician_rejection(t: Technician, *, territory_id: str, internal_authorized: bool, warranty: bool,
                          required_skill: str | None, window_start: dt.date,
                          part_available: bool | None) -> str | None:
    if t.territory_id != territory_id:
        return OTHER_TERRITORY
    if warranty and not internal_authorized:
        return NO_BRAND
    if required_skill and required_skill not in t.skill_ids:
        return NO_SKILL
    if t.busy_until is not None and t.busy_until > window_start:
        return UNAVAILABLE
    if part_available is False:
        return NO_PART
    return None


def decide_fulfillment(*, route: str, warranty: bool, brand: str, territory_id: str,
                       required_skill: str | None, centers: list[ServiceCenter],
                       technicians: list[Technician], window_start: dt.date,
                       part_available: bool | None = None, contract_technician_id: str | None = None,
                       card_technician_id: str | None = None) -> FulfillmentDecision:
    if route == "remote":
        return FulfillmentDecision("internal", None, None, (),
                                   "Удалённо: консультация собственной сервисной организации")
    if route not in ("onsite", "workshop"):
        raise ValueError(f"Маршрут «{route}» не требует исполнителя")

    candidates: list[Candidate] = []
    internal_centers = sorted((c for c in centers if c.is_internal), key=lambda c: c.center_id)
    external_centers = sorted((c for c in centers if not c.is_internal), key=lambda c: c.center_id)
    check = dict(territory_id=territory_id, warranty=warranty, brand=brand, required_skill=required_skill,
                 part_available=part_available)

    eligible_internal_centers = []
    for c in internal_centers:
        reason = _center_rejection(c, **check)
        candidates.append(Candidate(CENTER, c.center_id, None, None, reason,
                                    f"Собственная мастерская {c.name}" + (f": {reason}" if reason else "")))
        if reason is None:
            eligible_internal_centers.append(c)
    internal_authorized = any(warranty and brand in c.brands and c.territory_id == territory_id
                              for c in internal_centers)

    # Внутреннее исполнение.
    selected_tech: Technician | None = None
    selected_center: ServiceCenter | None = None
    if route == "onsite":
        eligible = []
        for t in sorted(technicians, key=lambda t: t.technician_id):
            reason = _technician_rejection(t, territory_id=territory_id, internal_authorized=internal_authorized,
                                           warranty=warranty, required_skill=required_skill,
                                           window_start=window_start, part_available=part_available)
            if reason:
                candidates.append(Candidate(TECHNICIAN, t.technician_id, None, None, reason,
                                            f"Собственный инженер {t.technician_id}: {reason}"))
            else:
                eligible.append(t)
        ordered = _priority_order(eligible, contract_technician_id, card_technician_id)
        for rank, (t, basis) in enumerate(ordered, 1):
            candidates.append(Candidate(TECHNICIAN, t.technician_id, rank, basis, None,
                                        f"Собственный инженер {t.technician_id}, основание: {basis}"))
        if ordered:
            selected_tech = ordered[0][0]
            selected_center = next((c for c in eligible_internal_centers
                                    if c.service_org_id == selected_tech.service_org_id), None)
    elif eligible_internal_centers:
        selected_center = eligible_internal_centers[0]

    if selected_tech or selected_center:
        return FulfillmentDecision(
            "internal", selected_center.center_id if selected_center else None,
            selected_tech.technician_id if selected_tech else None, tuple(candidates),
            "Внутреннее исполнение: собственный исполнитель проходит все проверки")

    # Внешнее исполнение.
    eligible_external = []
    for c in external_centers:
        reason = _center_rejection(c, **{**check, "part_available": None})
        if reason:
            candidates.append(Candidate(CENTER, c.center_id, None, None, reason,
                                        f"Внешний центр {c.name}: {reason}"))
        else:
            eligible_external.append(c)
    for rank, c in enumerate(eligible_external, 1):
        candidates.append(Candidate(CENTER, c.center_id, rank, "ранжирование", None,
                                    f"Внешний центр {c.name}: авторизация и компетенция подтверждены"))
    if eligible_external:
        why = ("гарантийный случай, собственная мастерская не авторизована по бренду "
               f"{brand}" if warranty and not internal_authorized else "собственных исполнителей нет")
        return FulfillmentDecision("external", eligible_external[0].center_id, None, tuple(candidates),
                                   f"Внешнее исполнение: {why}")

    return FulfillmentDecision(None, None, None, tuple(candidates),
                               "Исполнитель не найден: решение передаётся человеку")


def _priority_order(eligible: list[Technician], contract_id: str | None,
                    card_id: str | None) -> list[tuple[Technician, str]]:
    by_id = {t.technician_id: t for t in eligible}
    ordered: list[tuple[Technician, str]] = []
    if contract_id in by_id:
        ordered.append((by_id.pop(contract_id), "договор"))
    if card_id in by_id:
        ordered.append((by_id.pop(card_id), "карточка ИО"))
    ordered += [(by_id[k], "ранжирование") for k in sorted(by_id)]
    return ordered
