"""Понятия конкретной учётной системы не выходят за пределы её адаптера (ADR-0006).

Проверяется код платформы и мока. Адаптеры конкретных поставщиков
(backend/adapters/<поставщик>*.py, кроме мока) из проверки исключены —
соответствие полей поставщика доменной модели живёт только там.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = re.compile(r"\b(SAP|OData|S/?4\s?HANA|HANA|BAPI)\b|business partner|_bp_id|\bbp_id\b", re.IGNORECASE)
VENDOR_ADAPTERS = {"odata.py", "s4.py", "sap_odata.py"}


def test_no_vendor_terms_outside_vendor_adapters():
    offenders = []
    for base in (ROOT / "backend", ROOT / "mock-erp" / "app"):
        for path in base.rglob("*.py"):
            if path.parent.name == "adapters" and path.name in VENDOR_ADAPTERS:
                continue
            if path == ROOT / "mock-erp" / "app" / "odata.py":
                continue  # Поставщик интеграционного контракта, не доменный код.
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                # Composition/configuration boundaries may name the chosen protocol.
                if path == ROOT / "backend/config.py" and "erp_base_url=" in line:
                    continue
                if path == ROOT / "mock-erp/app/main.py" and line.startswith("from app.odata import"):
                    continue
                if VENDOR.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
