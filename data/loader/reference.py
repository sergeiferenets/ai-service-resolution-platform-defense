"""Чтение справочников и досинтезированных данных в типизированные записи.

Единственное место, где исходные колонки справочников (с русскими именами
и именами из шаблона) превращаются в записи с нейтральными полями. Дальше —
загрузчики мока учётной системы и платформы — работают только с записями.

Модуль без внешних зависимостей: его импортируют и загрузчики, и тесты.
Ссылочная целостность проверяется при чтении: ошибка в справочнике должна
остановить загрузку, а не всплыть посреди разбора обращения.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DATA_DIR = Path(__file__).resolve().parents[1]
REF = DATA_DIR / "reference"
SYN = DATA_DIR / "synthetic"
DEMO = DATA_DIR / "demo"

ROUTES = {
    "Выезд инженера": "onsite",
    "Удалённо": "remote",
    "Эскалация": "escalate",
    "Вывоз в мастерскую": "workshop",
}


class ReferenceDataError(Exception):
    """Справочники противоречивы или неполны."""


@dataclass(frozen=True)
class Territory:
    territory_id: str
    name: str


@dataclass(frozen=True)
class Model:
    model_id: str
    manufacturer: str
    name: str
    equipment_type: str


@dataclass(frozen=True)
class ErrorCode:
    code: str
    model_id: str | None  # None — для всех моделей
    description: str
    remote_possible: str
    required_competence: str
    typical_part: str
    action: str


@dataclass(frozen=True)
class Skill:
    skill_id: str
    name: str
    level: int


@dataclass(frozen=True)
class DecisionRuleText:
    """Дословные поля правила из справочника."""
    rule_id: str
    condition_text: str
    decision_text: str
    route: str
    urgency_hint: int
    category_in_reference: str
    prescribed_actions: str
    in_mvp: bool
    source: str


@dataclass(frozen=True)
class User:
    user_id: str
    full_name: str
    role: str
    service_org_id: str | None
    base_territory_id: str
    partner_id: str | None
    skill_ids: tuple[str, ...]


@dataclass(frozen=True)
class TerritoryAccess:
    user_id: str
    territory_id: str
    access_type: str


@dataclass(frozen=True)
class ServiceOrganization:
    service_org_id: str
    name: str
    territory_id: str
    service_kind: str


@dataclass(frozen=True)
class Document:
    doc_id: str
    title: str
    doc_type: str
    confidentiality: str
    model_ids: tuple[str, ...]
    source: str
    in_corpus: bool


@dataclass(frozen=True)
class Partner:
    partner_id: str
    name: str
    kind: str
    territory_id: str
    address: str
    contact_person: str
    phone: str


@dataclass(frozen=True)
class Equipment:
    equipment_id: str
    serial_number: str
    model_id: str
    partner_id: str
    sale_date: dt.date
    location: str
    status: str


@dataclass(frozen=True)
class Contract:
    contract_id: str
    partner_id: str
    kind: str | None
    valid_from: dt.date | None
    valid_to: dt.date | None
    reaction_hours: int | None
    resolution_sla: str | None
    coverage: str | None


@dataclass(frozen=True)
class Technician:
    technician_id: str
    full_name: str
    service_org_id: str
    skill_ids: tuple[str, ...]
    base_territory_id: str
    schedule: str
    busy_until: dt.date | None


@dataclass(frozen=True)
class Part:
    part_id: str
    name: str
    model_ids: tuple[str, ...]
    stock: int
    warehouse_territory_id: str
    price_rub: int
    lead_time_days: int


@dataclass(frozen=True)
class ServiceHistoryEntry:
    service_order_id: str
    equipment_id: str
    date: dt.date
    symptom: str
    error_code: str
    diagnosis: str
    work_done: str
    part_id: str | None
    technician_id: str | None
    billing: str
    labor_hours: float | None


@dataclass(frozen=True)
class ServiceCenter:
    center_id: str
    name: str
    is_internal: bool
    service_org_id: str
    supplier_id: str | None
    territory_id: str
    address: str
    skill_ids: tuple[str, ...]
    brands: tuple[str, ...]
    note: str


@dataclass(frozen=True)
class ProductPrice:
    model_id: str
    price_rub: int


@dataclass
class Reference:
    territories: dict[str, Territory] = field(default_factory=dict)
    models: dict[str, Model] = field(default_factory=dict)
    error_codes: list[ErrorCode] = field(default_factory=list)
    skills: dict[str, Skill] = field(default_factory=dict)
    decision_rules: dict[str, DecisionRuleText] = field(default_factory=dict)
    users: dict[str, User] = field(default_factory=dict)
    territory_access: list[TerritoryAccess] = field(default_factory=list)
    service_orgs: dict[str, ServiceOrganization] = field(default_factory=dict)
    documents: dict[str, Document] = field(default_factory=dict)
    partners: dict[str, Partner] = field(default_factory=dict)
    equipment: dict[str, Equipment] = field(default_factory=dict)
    contracts: dict[str, Contract] = field(default_factory=dict)
    technicians: dict[str, Technician] = field(default_factory=dict)
    parts: dict[str, Part] = field(default_factory=dict)
    service_history: list[ServiceHistoryEntry] = field(default_factory=list)
    service_centers: dict[str, ServiceCenter] = field(default_factory=dict)
    product_prices: dict[str, ProductPrice] = field(default_factory=dict)


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _none(value: str) -> str | None:
    value = value.strip()
    return None if value in ("", "—", "-") else value


def _date(value: str) -> dt.date | None:
    value = _none(value)
    return dt.date.fromisoformat(value[:10]) if value else None


def _list(value: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in (value or "").split(";") if x.strip())


def _hours(value: str) -> int | None:
    value = _none(value)
    if not value:
        return None
    m = re.match(r"^(\d+)\s*час", value)
    return int(m.group(1)) if m else None


def _float(value: str) -> float | None:
    value = _none(value)
    return float(value.replace(",", ".")) if value else None


def _read_territories(syn_dir: Path) -> dict[str, Territory]:
    return {row["territory_id"]: Territory(row["territory_id"], row["name"])
            for row in _read(syn_dir / "territories.csv")}


def _territory_lookup(territories: dict[str, Territory], problems: list[str]) -> Callable[[str, str], str]:
    by_name = {t.name: t.territory_id for t in territories.values()}

    def territory(name: str, where: str) -> str:
        tid = by_name.get(name.strip())
        if tid is None:
            problems.append(f"{where}: неизвестный регион «{name}»")
            return ""
        return tid

    return territory


def _read_decision_rules(ref_dir: Path, problems: list[str]) -> dict[str, DecisionRuleText]:
    rules: dict[str, DecisionRuleText] = {}
    for row in _read(ref_dir / "04_decision_rules.csv"):
        decision = row["Решение"].strip()
        if decision not in ROUTES:
            problems.append(f"{row['rule_id']}: неизвестное решение «{decision}»")
            continue
        rules[row["rule_id"]] = DecisionRuleText(
            rule_id=row["rule_id"], condition_text=row["Условие (как в жизни)"], decision_text=decision,
            route=ROUTES[decision], urgency_hint=int(row["Приоритет"]),
            category_in_reference=row["Детерминированное или требует оценки"],
            prescribed_actions=row["Комментарий / действие"], in_mvp=row["В MVP"].startswith("Да"),
            source=row["Основание / источник"],
        )
    return rules


def _read_users(ref_dir: Path, territory: Callable[[str, str], str]) -> dict[str, User]:
    return {row["user_id"]: User(
        user_id=row["user_id"], full_name=row["ФИО (условное)"], role=row["Роль"],
        service_org_id=_none(row["Сервисная организация"]),
        base_territory_id=territory(row["Базовый регион"], row["user_id"]),
        partner_id=_none(row["customer_id"]), skill_ids=_list(row["Компетенции (skill_id через ;)"]))
        for row in _read(ref_dir / "08_users.csv")}


def _read_access(ref_dir: Path, territory: Callable[[str, str], str]) -> list[TerritoryAccess]:
    return [TerritoryAccess(row["user_id"], territory(row["Регион"], f"доступ {row['user_id']}"), row["Тип доступа"])
            for row in _read(ref_dir / "19_territory_access.csv")]


def _raise_if(problems: list[str]) -> None:
    if problems:
        raise ReferenceDataError("Справочники не прошли проверку:\n  " + "\n  ".join(problems))


@dataclass(frozen=True)
class AccessReference:
    """Справочники платформы: территории, пользователи, доступ — без мастер-данных."""
    territories: dict[str, Territory]
    users: dict[str, User]
    territory_access: list[TerritoryAccess]


def load_decision_rules(ref_dir: Path = REF) -> dict[str, DecisionRuleText]:
    """Тексты правил — всё, что своду правил нужно из справочников."""
    problems: list[str] = []
    rules = _read_decision_rules(ref_dir, problems)
    _raise_if(problems)
    return rules


def load_access_reference(ref_dir: Path = REF, syn_dir: Path = SYN) -> AccessReference:
    """Для загрузчика платформы. Читает три файла и не требует остальных:
    образ платформы не содержит мастер-данных учётной системы (data-model.md,
    раздел 1) и не должен от них зависеть."""
    problems: list[str] = []
    territories = _read_territories(syn_dir)
    territory = _territory_lookup(territories, problems)
    users = _read_users(ref_dir, territory)
    access = _read_access(ref_dir, territory)
    problems += [f"доступ: пользователь {a.user_id} не найден" for a in access if a.user_id not in users]
    _raise_if(problems)
    return AccessReference(territories, users, access)


def load_reference(ref_dir: Path = REF, syn_dir: Path = SYN) -> Reference:
    r = Reference()
    problems: list[str] = []
    r.territories = _read_territories(syn_dir)
    territory = _territory_lookup(r.territories, problems)

    for row in _read(ref_dir / "01_models.csv"):
        r.models[row["model_id"]] = Model(row["model_id"], row["Производитель"], row["Модель"], row["Тип оборудования"])

    for row in _read(ref_dir / "02_error_codes.csv"):
        scope = row["model_id или Все"].strip()
        r.error_codes.append(ErrorCode(
            code=row["error_code"], model_id=None if scope == "Все" else scope,
            description=row["Расшифровка"], remote_possible=row["Решается удалённо?"],
            required_competence=row["Требуемая компетенция"], typical_part=row["Типовая запчасть"],
            action=row["Что делать"],
        ))

    for row in _read(ref_dir / "03_skills.csv"):
        r.skills[row["skill_id"]] = Skill(row["skill_id"], row["Название компетенции"], int(row["Уровень"]))

    for row in _read(ref_dir / "04_decision_rules.csv"):
        decision = row["Решение"].strip()
        if decision not in ROUTES:
            problems.append(f"{row['rule_id']}: неизвестное решение «{decision}»")
            continue
        r.decision_rules[row["rule_id"]] = DecisionRuleText(
            rule_id=row["rule_id"], condition_text=row["Условие (как в жизни)"], decision_text=decision,
            route=ROUTES[decision], urgency_hint=int(row["Приоритет"]),
            category_in_reference=row["Детерминированное или требует оценки"],
            prescribed_actions=row["Комментарий / действие"], in_mvp=row["В MVP"].startswith("Да"),
            source=row["Основание / источник"],
        )

    for row in _read(ref_dir / "09_service_organizations.csv"):
        r.service_orgs[row["so_id"]] = ServiceOrganization(
            row["so_id"], row["Название"], territory(row["Регион"], row["so_id"]), row["Вид услуги"])

    for row in _read(ref_dir / "11_partners.csv"):
        r.partners[row["customer_id"]] = Partner(
            partner_id=row["customer_id"], name=row["Наименование (условное)"], kind=row["Тип"],
            territory_id=territory(row["Регион"], row["customer_id"]), address=row["Адрес"],
            contact_person=row["Контактное лицо"], phone=row["Телефон"])

    for row in _read(ref_dir / "08_users.csv"):
        r.users[row["user_id"]] = User(
            user_id=row["user_id"], full_name=row["ФИО (условное)"], role=row["Роль"],
            service_org_id=_none(row["Сервисная организация"]),
            base_territory_id=territory(row["Базовый регион"], row["user_id"]),
            partner_id=_none(row["customer_id"]), skill_ids=_list(row["Компетенции (skill_id через ;)"]))

    for row in _read(ref_dir / "19_territory_access.csv"):
        r.territory_access.append(TerritoryAccess(
            row["user_id"], territory(row["Регион"], f"доступ {row['user_id']}"), row["Тип доступа"]))

    for row in _read(ref_dir / "10_documents.csv"):
        r.documents[row["doc_id"]] = Document(
            doc_id=row["doc_id"], title=row["Название документа"], doc_type=row["Тип"],
            confidentiality=row["Уровень доступа"], model_ids=_list(row["К каким моделям относится"]),
            source=row["Источник"], in_corpus=row["В корпусе знаний MVP"].startswith("Да"))

    # Справочник, синтетическое дополнение (второй аппарат той же модели у
    # партнёра) и оборудование для демонстрации с настоящими фотографиями
    # табличек — последнее вносится вручную и может отсутствовать.
    equipment_files = [ref_dir / "12_equipment.csv", syn_dir / "equipment_extra.csv"]
    if (DEMO / "equipment_demo.csv").exists():
        equipment_files.append(DEMO / "equipment_demo.csv")
    serials: set[str] = set()
    for path in equipment_files:
        for row in _read(path):
            eid, serial = row["equipment_id"], row["Серийный номер"]
            if eid in r.equipment or serial in serials:
                problems.append(f"{eid}: идентификатор или серийный номер {serial} задан дважды ({path.name})")
                continue
            sale = _date(row["Дата продажи"])
            if sale is None:
                problems.append(f"{eid}: нет даты продажи")
                continue
            serials.add(serial)
            r.equipment[eid] = Equipment(
                equipment_id=eid, serial_number=serial, model_id=row["model_id"], partner_id=row["customer_id"],
                sale_date=sale, location=row["Место установки"], status=row["Статус"])

    for row in _read(ref_dir / "13_contracts.csv"):
        r.contracts[row["contract_id"]] = Contract(
            contract_id=row["contract_id"], partner_id=row["customer_id"], kind=_none(row["Тип"]),
            valid_from=_date(row["Действует с"]), valid_to=_date(row["Действует по"]),
            reaction_hours=_hours(row["SLA реакции"]), resolution_sla=_none(row["SLA решения"]),
            coverage=_none(row["Что покрывает"]))

    busy = {row["technician_id"]: _date(row["busy_until"]) for row in _read(syn_dir / "technician_availability.csv")}
    for row in _read(ref_dir / "14_technicians.csv"):
        tid = row["technician_id"]
        if tid not in busy:
            problems.append(f"{tid}: нет записи о занятости в synthetic/technician_availability.csv")
        r.technicians[tid] = Technician(
            technician_id=tid, full_name=row["ФИО (условное)"], service_org_id=row["Сервисная организация"],
            skill_ids=_list(row["Компетенции (skill_id через ;)"]),
            base_territory_id=territory(row["База (город)"], tid), schedule=row["Доступность"],
            busy_until=busy.get(tid))

    for row in _read(ref_dir / "15_parts.csv"):
        r.parts[row["part_id"]] = Part(
            part_id=row["part_id"], name=row["Наименование"], model_ids=_list(row["Подходит к model_id (через ;)"]),
            stock=int(row["Остаток на складе"]), warehouse_territory_id=territory(row["Склад"], row["part_id"]),
            price_rub=int(row["Цена, ₽"]), lead_time_days=int(row["Срок поставки, дней"]))

    for row in _read(ref_dir / "16_service_history.csv"):
        r.service_history.append(ServiceHistoryEntry(
            service_order_id=row["service_order_id"], equipment_id=row["equipment_id"], date=_date(row["Дата"]),
            symptom=row["Симптом"], error_code=row["error_code"], diagnosis=row["Диагноз"],
            work_done=row["Что сделали"], part_id=_none(row["part_id"]), technician_id=_none(row["technician_id"]),
            billing=row["Гарантия или платно"], labor_hours=_float(row["Трудозатраты, ч"])))

    for path in (ref_dir / "18_service_centers.csv", syn_dir / "service_centers_external.csv"):
        for row in _read(path):
            cid = row["center_id"]
            if cid in r.service_centers:
                problems.append(f"{cid}: центр задан дважды")
            r.service_centers[cid] = ServiceCenter(
                center_id=cid, name=row["Наименование"], is_internal=row["Тип центра"].startswith("Внутренн"),
                service_org_id=row["service_org_id"], supplier_id=_none(row["supplier_bp_id"]),
                territory_id=territory(row["Регион"], cid), address=row["Адрес"],
                skill_ids=_list(row["Компетенции (skill_id через ;)"]), brands=_list(row["Авторизация по брендам"]),
                note=row["Комментарий"])

    for row in _read(syn_dir / "product_prices.csv"):
        r.product_prices[row["model_id"]] = ProductPrice(row["model_id"], int(row["price_rub"]))

    problems.extend(_check_links(r))
    if problems:
        raise ReferenceDataError("Справочники не прошли проверку:\n  " + "\n  ".join(problems))
    return r


def _check_links(r: Reference) -> list[str]:
    p: list[str] = []
    for e in r.equipment.values():
        if e.model_id not in r.models:
            p.append(f"{e.equipment_id}: модель {e.model_id} не найдена")
        if e.partner_id not in r.partners:
            p.append(f"{e.equipment_id}: партнёр {e.partner_id} не найден")
    for c in r.contracts.values():
        if c.partner_id not in r.partners:
            p.append(f"{c.contract_id}: партнёр {c.partner_id} не найден")
    for t in r.technicians.values():
        if t.service_org_id not in r.service_orgs:
            p.append(f"{t.technician_id}: сервисная организация {t.service_org_id} не найдена")
        p += [f"{t.technician_id}: компетенция {s} не найдена" for s in t.skill_ids if s not in r.skills]
    for u in r.users.values():
        if u.service_org_id and u.service_org_id not in r.service_orgs:
            p.append(f"{u.user_id}: сервисная организация {u.service_org_id} не найдена")
        if u.partner_id and u.partner_id not in r.partners:
            p.append(f"{u.user_id}: партнёр {u.partner_id} не найден")
    for a in r.territory_access:
        if a.user_id not in r.users:
            p.append(f"доступ: пользователь {a.user_id} не найден")
    for c in r.service_centers.values():
        if c.service_org_id not in r.service_orgs:
            p.append(f"{c.center_id}: сервисная организация {c.service_org_id} не найдена")
        p += [f"{c.center_id}: компетенция {s} не найдена" for s in c.skill_ids if s not in r.skills]
    for part in r.parts.values():
        p += [f"{part.part_id}: модель {m} не найдена" for m in part.model_ids if m not in r.models]
    for h in r.service_history:
        if h.equipment_id not in r.equipment:
            p.append(f"{h.service_order_id}: оборудование {h.equipment_id} не найдено")
    for e in r.error_codes:
        if e.model_id and e.model_id not in r.models:
            p.append(f"код {e.code}: модель {e.model_id} не найдена")
    p += [f"цена: модель {m} не найдена" for m in r.product_prices if m not in r.models]
    p += [f"модель {m}: нет цены изделия" for m in r.models if m not in r.product_prices]
    return p
