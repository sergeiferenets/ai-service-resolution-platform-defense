"""Досинтез данных, которых нет в справочниках.

Справочники (data/reference/) содержат образцы, заполненные вручную. Здесь
добавляется только то, без чего не работает сквозной сценарий, и ничего
сверх того:

- территории — производятся из регионов справочников, без случайности;
- внешние сервисные центры для Ярославля, Воронежа и Твери. Собственных
  мастерских там нет, поэтому гарантийные и мастерские работы уходят
  наружу существующей логикой маршрутизации, без новых правил;
- занятость инженеров. В справочнике «доступность» — график работы, а для
  подбора исполнителя нужно состояние на момент решения;
- цены изделий — вход правила согласования A-03 (стоимость ремонта против
  цены изделия);
- второй аппарат той же модели у одного партнёра. Без него нельзя
  проверить неоднозначность при определении оборудования по партнёру и
  модели (PRD 5.3): в справочнике у каждого партнёра модели не повторяются.

Всё детерминировано: фиксированный seed, стабильный порядок обхода.
Результат пишется в data/synthetic/ и восстанавливается повторным запуском.

    python data/generators/synthesize.py
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import random
from pathlib import Path

GENERATOR_VERSION = "2"
SEED = 20260914

REPO_ROOT = Path(__file__).resolve().parents[2]
REF = REPO_ROOT / "data" / "reference"
OUT = REPO_ROOT / "data" / "synthetic"

# Код территории — стабильный идентификатор для загрузчиков и тестов.
TERRITORY_CODES = {
    "Москва": "TER-MSK",
    "Санкт-Петербург": "TER-SPB",
    "Нижний Новгород": "TER-NVR",
    "Ярославль": "TER-YAR",
    "Воронеж": "TER-VRN",
    "Тверь": "TER-TVE",
}

# Внешние центры для территорий без собственной мастерской. Колонки
# совпадают со справочником 18_service_centers.csv, чтобы загрузчик
# объединял оба источника без отдельной ветки.
EXTERNAL_CENTERS = [
    ("SC-101", "Сервисный центр 101", "SO-YAR-REP", "SUP-101", "Ярославль"),
    ("SC-102", "Сервисный центр 102", "SO-VRN-REP", "SUP-102", "Воронеж"),
    ("SC-103", "Сервисный центр 103", "SO-TVE-REP", "SUP-103", "Тверь"),
]
EXTERNAL_CENTER_SKILLS = "SK-01;SK-02;SK-03"
EXTERNAL_CENTER_BRANDS = "HP;Canon;Kyocera;Pantum"

# Опора обязательного сценария: свободный собственный инженер Петербурга
# с компетенцией L3. Гарантийный случай по HP обязан уйти во внешний центр
# несмотря на него — это и проверяет тест.
ALWAYS_FREE = {"TECH-013"}

# Ориентир цены нового изделия по типу оборудования, ₽. Тип, которого нет
# в таблице, — ошибка, а не цена по умолчанию: молча подставленная цена
# исказила бы правило согласования A-03.
PRICE_BY_TYPE = {
    "МФУ лазерное (А4)": 45000,
    "Принтер лазерный (А4)": 30000,
    "Ноутбук": 90000,
    "Сервер (стоечный)": 450000,
    "Интерактивная панель": 250000,
    "Сканер поточный (профессиональный)": 350000,
    "Сканер сетевой (профессиональный)": 80000,
    "Система видеоконференцсвязи (ВКС)": 180000,
}

# Второй Canon i-SENSYS MF463dw у партнёра CUST-009 (у него уже есть EQ-0012):
# обращение «Canon MF463dw» без серийного номера находит два аппарата.
# Колонки — как в 12_equipment.csv.
EXTRA_EQUIPMENT = [
    ("EQ-0013", "CAN-MF463-70318", "MOD-004", "CUST-009", "2025-11-20",
     "Санкт-Петербург, площадка SPB-02", "В эксплуатации"),
]

# Дата, от которой отсчитывается занятость. Фиксирована, чтобы результат
# не зависел от дня запуска.
REFERENCE_DATE = dt.date(2026, 9, 14)


def read(name: str) -> list[dict[str, str]]:
    with (REF / name).open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write(name: str, header: list[str], rows: list[list[object]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / name).open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    print(f"{name:32s} {len(rows):4d} строк")


def main() -> None:
    rng = random.Random(SEED)

    regions = sorted(
        {r["Регион"] for r in read("11_partners.csv")}
        | {r["Регион"] for r in read("09_service_organizations.csv")}
    )
    unknown = [r for r in regions if r not in TERRITORY_CODES]
    if unknown:
        raise SystemExit(f"Нет кода территории для регионов: {unknown}")
    write(
        "territories.csv",
        ["territory_id", "name"],
        [[TERRITORY_CODES[r], r] for r in sorted(regions, key=lambda r: TERRITORY_CODES[r])],
    )

    ref_centers = read("18_service_centers.csv")
    existing_regions = {c["Регион"] for c in ref_centers}
    rows = []
    for center_id, name, so_id, supplier, region in EXTERNAL_CENTERS:
        if region in existing_regions:
            raise SystemExit(f"В справочнике уже есть центр в регионе {region}")
        rows.append([
            center_id, name, "Внешний АСЦ", so_id, supplier, region, "",
            EXTERNAL_CENTER_SKILLS, EXTERNAL_CENTER_BRANDS,
            "Синтезирован: собственной мастерской в регионе нет",
        ])
    write("service_centers_external.csv", list(ref_centers[0].keys()), rows)

    rows = []
    for t in sorted(read("14_technicians.csv"), key=lambda t: t["technician_id"]):
        tid = t["technician_id"]
        if tid in ALWAYS_FREE or rng.random() < 0.7:
            busy_until = ""
        else:
            busy_until = (REFERENCE_DATE + dt.timedelta(days=rng.randint(1, 10))).isoformat()
        rows.append([tid, busy_until])
    write("technician_availability.csv", ["technician_id", "busy_until"], rows)

    models = sorted(read("01_models.csv"), key=lambda m: m["model_id"])
    unknown_types = sorted({m["Тип оборудования"] for m in models} - PRICE_BY_TYPE.keys())
    if unknown_types:
        raise SystemExit(f"Нет ориентира цены для типов: {unknown_types}")
    rows = []
    for m in models:
        base = PRICE_BY_TYPE[m["Тип оборудования"]]
        price = round(base * rng.uniform(0.9, 1.1), -2)
        rows.append([m["model_id"], int(price)])
    write("product_prices.csv", ["model_id", "price_rub"], rows)

    # Без случайности и после всех случайных шагов: прежние файлы не меняются.
    equipment = read("12_equipment.csv")
    taken = {e["equipment_id"] for e in equipment} | {e["Серийный номер"] for e in equipment}
    clash = [r for r in EXTRA_EQUIPMENT if r[0] in taken or r[1] in taken]
    if clash:
        raise SystemExit(f"Синтетическое оборудование совпадает со справочником: {clash}")
    write("equipment_extra.csv", list(equipment[0].keys()), [list(r) for r in EXTRA_EQUIPMENT])

    (OUT / "meta.json").write_text(
        json.dumps(
            {
                "generator": "data/generators/synthesize.py",
                "generator_version": GENERATOR_VERSION,
                "seed": SEED,
                "reference_date": REFERENCE_DATE.isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
