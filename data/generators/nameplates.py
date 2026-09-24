"""Снимки идентификационных табличек для набора оценки.

Рисует табличку по карточке оборудования — производитель, модель, серийный
номер, штрихкод Code 39 — и портит её до правдоподобного снимка: поворот,
неравномерная яркость, шум, размытие, сжатие JPEG. Детерминировано:
фиксированный seed, собственный генератор шума; повторный запуск даёт те же
файлы.

Три группы:
- readable_found   — табличка читаемая, номер есть в справочнике;
- readable_unknown — табличка читаемая, номера в справочнике нет;
- unreadable       — номер со снимка прочитать нельзя.

    python data/generators/nameplates.py
"""

from __future__ import annotations

import csv
import io
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

GENERATOR_VERSION = "1"
SEED = 20260915

REPO_ROOT = Path(__file__).resolve().parents[2]
REF = REPO_ROOT / "data" / "reference"
SYN = REPO_ROOT / "data" / "synthetic"
OUT = REPO_ROOT / "data" / "eval" / "nameplates"

READABLE_FOUND = ["EQ-0011", "EQ-0001", "EQ-0002", "EQ-0003", "EQ-0009", "EQ-0012", "EQ-0013",
                  "EQ-0004", "EQ-0005", "EQ-0006"]
# Номера, которых в справочнике нет: соседняя цифра и чужой аппарат той же модели.
READABLE_UNKNOWN = [("EQ-0011", "HPL-M4103-77843"), ("EQ-0002", "KYO-M2135-40917"),
                    ("EQ-0009", "CAN-MF463-12093"), ("EQ-0001", "PNT-M6500W-88310")]
UNREADABLE = ["EQ-0011", "EQ-0012", "EQ-0002"]

# Code 39: девять элементов «штрих, пробел, …», n — узкий, w — широкий.
CODE39 = {
    "0": "nnnwwnwnn", "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn", "4": "nnnwwnnnw",
    "5": "wnnwwnnnn", "6": "nnwwwnnnn", "7": "nnnwnnwnw", "8": "wnnwnnwnn", "9": "nnwwnnwnn",
    "A": "wnnnnwnnw", "B": "nnwnnwnnw", "C": "wnwnnwnnn", "D": "nnnnwwnnw", "E": "wnnnwwnnn",
    "F": "nnwnwwnnn", "G": "nnnnnwwnw", "H": "wnnnnwwnn", "I": "nnwnnwwnn", "J": "nnnnwwwnn",
    "K": "wnnnnnnww", "L": "nnwnnnnww", "M": "wnwnnnnwn", "N": "nnnnwnnww", "O": "wnnnwnnwn",
    "P": "nnwnwnnwn", "Q": "nnnnnnwww", "R": "wnnnnnwwn", "S": "nnwnnnwwn", "T": "nnnnwnwwn",
    "U": "wwnnnnnnw", "V": "nwwnnnnnw", "W": "wwwnnnnnn", "X": "nwnnwnnnw", "Y": "wwnnwnnnn",
    "Z": "nwwnwnnnn", "-": "nwnnnnwnw", ".": "wwnnnnwnn", " ": "nwwnnnwnn", "*": "nwnnwnwnn",
}


def read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def barcode(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, height: int, narrow: int) -> None:
    for ch in f"*{text.upper()}*":
        for i, element in enumerate(CODE39[ch]):
            width = narrow * (3 if element == "w" else 1)
            if i % 2 == 0:
                draw.rectangle([x, y, x + width - 1, y + height], fill=(20, 20, 20))
            x += width
        x += narrow


def plate(manufacturer: str, model: str, serial: str, rng: random.Random) -> Image.Image:
    img = Image.new("RGB", (900, 520), rng.choice([(236, 236, 230), (212, 214, 217), (246, 242, 230)]))
    draw = ImageDraw.Draw(img)
    draw.rectangle([6, 6, 893, 513], outline=(60, 60, 60), width=4)
    draw.text((40, 28), manufacturer, font=ImageFont.load_default(size=60), fill=(25, 25, 25))
    mid = ImageFont.load_default(size=34)
    draw.text((40, 118), f"Model: {model}", font=mid, fill=(30, 30, 30))
    draw.text((40, 172), "Rated: 220-240V ~ 50/60Hz 4.5A", font=ImageFont.load_default(size=24), fill=(55, 55, 55))
    draw.text((40, 222), f"S/N: {serial}", font=mid, fill=(20, 20, 20))
    barcode(draw, 40, 285, serial, 120, 2)
    draw.text((40, 440), "Made in China", font=ImageFont.load_default(size=24), fill=(70, 70, 70))
    return img


def degrade(img: Image.Image, rng: random.Random, heavy: bool) -> tuple[bytes, dict]:
    params = {"angle": round(rng.uniform(-25, 25) if heavy else rng.uniform(-7, 7), 1),
              "shade": 0.12 if heavy else round(rng.uniform(0.4, 0.6), 2),
              "noise": 0.4 if heavy else round(rng.uniform(0.06, 0.12), 2),
              "blur": 7.0 if heavy else round(rng.uniform(0.4, 1.0), 1),
              "quality": 10 if heavy else rng.randint(45, 70)}
    size = (1100, 760)
    ground = tuple([rng.randint(60, 120)] * 3)
    canvas = Image.new("RGB", size, ground)
    turned = img.rotate(params["angle"], expand=True, resample=Image.Resampling.BICUBIC, fillcolor=ground)
    canvas.paste(turned, ((size[0] - turned.width) // 2 + rng.randint(-30, 30),
                          (size[1] - turned.height) // 2 + rng.randint(-30, 30)))
    # Неравномерная яркость: одна сторона снимка в тени.
    mask = Image.linear_gradient("L").rotate(rng.choice([0, 90, 180, 270])).resize(size)
    canvas = Image.composite(canvas, ImageEnhance.Brightness(canvas).enhance(params["shade"]), mask)
    noise = Image.frombytes("L", size, rng.randbytes(size[0] * size[1])).convert("RGB")
    canvas = Image.blend(canvas, noise, params["noise"]).filter(ImageFilter.GaussianBlur(params["blur"]))
    if heavy:
        canvas = ImageEnhance.Contrast(canvas).enhance(0.3)
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=params["quality"])
    return buf.getvalue(), params


def main() -> None:
    rng = random.Random(SEED)
    models = {m["model_id"]: m for m in read(REF / "01_models.csv")}
    equipment = {e["equipment_id"]: e for e in read(REF / "12_equipment.csv") + read(SYN / "equipment_extra.csv")}
    known_serials = {e["Серийный номер"] for e in equipment.values()}

    cases = ([("readable_found", eid, equipment[eid]["Серийный номер"]) for eid in READABLE_FOUND]
             + [("readable_unknown", eid, serial) for eid, serial in READABLE_UNKNOWN]
             + [("unreadable", eid, equipment[eid]["Серийный номер"]) for eid in UNREADABLE])
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for number, (group, eid, serial) in enumerate(cases, 1):
        if (group == "readable_unknown") == (serial in known_serials):
            raise SystemExit(f"{eid}: номер {serial} не соответствует группе {group}")
        model = models[equipment[eid]["model_id"]]
        data, params = degrade(plate(model["Производитель"], model["Модель"], serial, rng), rng, group == "unreadable")
        name = f"plate-{number:02d}-{group}.jpg"
        (OUT / name).write_bytes(data)
        manifest.append({"file": name, "group": group, "equipment_id": eid, "partner_id": equipment[eid]["customer_id"],
                         "manufacturer": model["Производитель"], "model": model["Модель"], "serial_on_plate": serial,
                         "in_reference": serial in known_serials, "degradation": params})
        print(f"{name:34s} {serial:18s} {len(data) // 1024:4d} КБ")
    (OUT / "manifest.json").write_text(json.dumps(
        {"generator": "data/generators/nameplates.py", "generator_version": GENERATOR_VERSION, "seed": SEED,
         "plates": manifest}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
