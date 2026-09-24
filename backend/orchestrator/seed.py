"""Загрузчик платформы: территории, пользователи и их доступ, свод правил.

Мастер-данных учётной системы здесь нет и быть не должно (data-model.md,
раздел 1): оборудование, партнёры, договоры, цены и остатки запрашиваются
через адаптер. Загрузка идемпотентна — повторный запуск приводит таблицы к
текущему состоянию файлов.

    python -m backend.orchestrator.seed
"""

from __future__ import annotations

import psycopg

from backend.config import Settings, load_settings
from backend.orchestrator.db import conninfo, ensure_database, migrate
from backend.rules.catalog import Catalog, RuleDef, load_catalog
from data.loader.reference import load_access_reference, load_decision_rules


def specificity(rule: RuleDef) -> str | None:
    levels = {t.level for t in rule.triggers}
    if 2 in levels:
        return "модель и код"
    if 4 in levels:
        return "общий симптом"
    return None


def seed(settings: Settings, catalog: Catalog | None = None) -> dict[str, int]:
    access = load_access_reference()
    catalog = catalog or load_catalog()
    texts = load_decision_rules()
    with psycopg.connect(conninfo(settings)) as conn:
        for t in access.territories.values():
            conn.execute("INSERT INTO territory (territory_id, name) VALUES (%s, %s)"
                         " ON CONFLICT (territory_id) DO UPDATE SET name = EXCLUDED.name", (t.territory_id, t.name))
        for u in access.users.values():
            conn.execute(
                "INSERT INTO app_user (user_id, display_name, role, partner_id, is_active) VALUES (%s, %s, %s, %s, true)"
                " ON CONFLICT (user_id) DO UPDATE SET display_name = EXCLUDED.display_name, role = EXCLUDED.role,"
                " partner_id = EXCLUDED.partner_id, is_active = true",
                (u.user_id, u.full_name, u.role, u.partner_id))
        # Пользователь, исчезнувший из справочника, отключается, а не удаляется:
        # на него ссылаются обращения и аудит.
        conn.execute("UPDATE app_user SET is_active = false WHERE NOT (user_id = ANY(%s))", (list(access.users),))
        conn.execute("DELETE FROM user_territory")
        pairs = sorted({(a.user_id, a.territory_id) for a in access.territory_access})
        for user_id, territory_id in pairs:
            conn.execute("INSERT INTO user_territory (user_id, territory_id) VALUES (%s, %s)", (user_id, territory_id))
        for r in catalog.rules.values():
            conn.execute(
                "INSERT INTO rule (id, condition_text, execution_mode, specificity, urgency_hint, rule_class,"
                " prescribed_actions, source_url, rules_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (id) DO UPDATE SET condition_text = EXCLUDED.condition_text,"
                " execution_mode = EXCLUDED.execution_mode, specificity = EXCLUDED.specificity,"
                " urgency_hint = EXCLUDED.urgency_hint, rule_class = EXCLUDED.rule_class,"
                " prescribed_actions = EXCLUDED.prescribed_actions, source_url = EXCLUDED.source_url,"
                " rules_version = EXCLUDED.rules_version",
                (r.rule_id, r.condition_text, texts[r.rule_id].decision_text, specificity(r), r.urgency_hint,
                 r.category, r.prescribed_actions, texts[r.rule_id].source, catalog.version))
    return {"territories": len(access.territories), "users": len(access.users),
            "user_territories": len(pairs), "rules": len(catalog.rules)}


def prepare(settings: Settings) -> dict[str, int]:
    if settings.db_name != settings.maintenance_db:
        ensure_database(settings)
    migrate(settings)
    return seed(settings)


def main() -> None:
    counts = prepare(load_settings())
    print("Справочники платформы загружены: " + ", ".join(f"{k} {v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
