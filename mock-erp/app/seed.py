"""Заполнение мока учётной системы данными из справочников.

    python -m app.seed --if-empty   заполнить, только если база пуста (при старте контейнера)
    python -m app.seed --reset      очистить всё, включая черновики и аудит, и заполнить заново

Данные берутся из data/reference и data/synthetic через data.loader.reference —
то же чтение, что и у загрузчика платформы.
"""

from __future__ import annotations

import argparse

import psycopg

from app.config import load_settings
from app.db import conninfo, ensure_database, migrate
from data.loader.reference import Reference, load_reference

TABLES = [
    "audit_event", "work_order_draft", "user_territory", "app_user", "service_history", "part_model", "part",
    "service_center_skill", "service_center_brand", "service_center", "contract", "equipment",
    "technician_skill", "technician", "service_organization", "model", "skill", "partner", "territory",
]


def fill(conn: psycopg.Connection, ref: Reference) -> dict[str, int]:
    def many(query: str, data: list[tuple]) -> int:
        with conn.cursor() as cur:
            cur.executemany(query, data)
        return len(data)

    n = {}
    n["territory"] = many("INSERT INTO territory VALUES (%s, %s)",
                          [(t.territory_id, t.name) for t in ref.territories.values()])
    n["partner"] = many("INSERT INTO partner VALUES (%s, %s, %s, %s, %s, %s, %s)", [
        (p.partner_id, p.name, p.kind, p.territory_id, p.address, p.contact_person, p.phone)
        for p in ref.partners.values()])
    n["skill"] = many("INSERT INTO skill VALUES (%s, %s, %s)",
                      [(s.skill_id, s.name, s.level) for s in ref.skills.values()])
    n["model"] = many("INSERT INTO model VALUES (%s, %s, %s, %s, %s)", [
        (m.model_id, m.manufacturer, m.name, m.equipment_type, ref.product_prices[m.model_id].price_rub)
        for m in ref.models.values()])
    n["service_organization"] = many("INSERT INTO service_organization VALUES (%s, %s, %s, %s)", [
        (o.service_org_id, o.name, o.territory_id, o.service_kind) for o in ref.service_orgs.values()])
    n["technician"] = many("INSERT INTO technician VALUES (%s, %s, %s, %s, %s, %s)", [
        (t.technician_id, t.full_name, t.service_org_id, t.base_territory_id, t.schedule, t.busy_until)
        for t in ref.technicians.values()])
    many("INSERT INTO technician_skill VALUES (%s, %s)",
         [(t.technician_id, s) for t in ref.technicians.values() for s in t.skill_ids])
    n["equipment"] = many("INSERT INTO equipment VALUES (%s, %s, %s, %s, %s, %s, %s, NULL)", [
        (e.equipment_id, e.serial_number, e.model_id, e.partner_id, e.sale_date, e.location, e.status)
        for e in ref.equipment.values()])
    n["contract"] = many("INSERT INTO contract VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL)", [
        (c.contract_id, c.partner_id, c.kind, c.valid_from, c.valid_to, c.reaction_hours, c.resolution_sla,
         c.coverage) for c in ref.contracts.values()])
    n["service_center"] = many("INSERT INTO service_center VALUES (%s, %s, %s, %s, %s, %s, %s)", [
        (c.center_id, c.name, c.is_internal, c.service_org_id, c.supplier_id, c.territory_id, c.address)
        for c in ref.service_centers.values()])
    many("INSERT INTO service_center_brand VALUES (%s, %s)",
         [(c.center_id, b) for c in ref.service_centers.values() for b in c.brands])
    many("INSERT INTO service_center_skill VALUES (%s, %s)",
         [(c.center_id, s) for c in ref.service_centers.values() for s in c.skill_ids])
    n["part"] = many("INSERT INTO part VALUES (%s, %s, %s, %s, %s, %s)", [
        (p.part_id, p.name, p.stock, p.warehouse_territory_id, p.price_rub, p.lead_time_days)
        for p in ref.parts.values()])
    many("INSERT INTO part_model VALUES (%s, %s)", [(p.part_id, m) for p in ref.parts.values() for m in p.model_ids])
    n["service_history"] = many("INSERT INTO service_history VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", [
        (h.service_order_id, h.equipment_id, h.date, h.symptom, h.error_code, h.diagnosis, h.work_done,
         h.part_id, h.technician_id, h.billing, h.labor_hours) for h in ref.service_history])
    n["app_user"] = many("INSERT INTO app_user VALUES (%s, %s, %s, %s)", [
        (u.user_id, u.full_name, u.role, u.partner_id) for u in ref.users.values()])
    n["user_territory"] = many(
        "INSERT INTO user_territory VALUES (%s, %s) ON CONFLICT DO NOTHING",
        sorted({(a.user_id, a.territory_id) for a in ref.territory_access}))
    return n


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--if-empty", action="store_true")
    mode.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    ensure_database(settings)
    migrate(settings)
    ref = load_reference()
    with psycopg.connect(conninfo(settings)) as conn:
        if args.if_empty and conn.execute("SELECT count(*) FROM partner").fetchone()[0] > 0:
            print("seed: база уже заполнена, пропускаю")
            return
        conn.execute("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")
        conn.execute("ALTER SEQUENCE work_order_draft_seq RESTART WITH 1")
        counts = fill(conn, ref)
    print("seed: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
