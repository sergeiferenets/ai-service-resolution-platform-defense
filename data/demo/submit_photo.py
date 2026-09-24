"""Отправка обращения с фотографией таблички в API платформы.

    python data/demo/submit_photo.py data/demo/photos/plate.jpg --user U-019 --text "Не печатает"

Адрес API — переменная API_URL (по умолчанию http://127.0.0.1:8000, через
SSH-туннель к узлу).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path

import httpx

TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("photo", type=Path)
    parser.add_argument("--user", required=True, help="X-User-Id, например U-019")
    parser.add_argument("--text", default="Аппарат не работает, фото таблички приложено")
    parser.add_argument("--partner", help="customer_id, если обращение регистрирует сотрудник")
    args = parser.parse_args()

    body = {"text": args.text, "image_base64": base64.b64encode(args.photo.read_bytes()).decode(),
            "image_content_type": TYPES[args.photo.suffix.lower()]}
    if args.partner:
        body["partner_id"] = args.partner
    response = httpx.post(f"{os.environ.get('API_URL', 'http://127.0.0.1:8000')}/v1/requests", json=body,
                          headers={"X-User-Id": args.user}, timeout=120)
    print(response.status_code)
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
