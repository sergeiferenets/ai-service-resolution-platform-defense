"""Детерминированные проверки извлечённого Context Agent.

Извлечённое не принимается на веру (PRD 5.3: модель предлагает кандидата,
решение принимает справочник):
- каждое значение из текста должно дословно встречаться в тексте — с
  точностью до регистра, пробелов и вида тире. Выдуманное значение —
  несоответствие, и слой вызова модели делает повтор;
- номер с таблички должен быть правдоподобным идентификатором и не
  содержать внедрённых указаний (security.md, раздел 3.2);
- обозначение модели из текста не должно противоречить карточке.
"""

from __future__ import annotations

import re
from typing import Callable

from backend.agents.context.schema import TextExtraction

_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"})
_SERIAL = re.compile(r"[A-Z0-9][A-Z0-9\-/.]{3,39}")
TEXT_FIELDS = ("serial_number", "model_designation", "error_code", "symptom_text")


def normalize(value: str) -> str:
    return " ".join(value.translate(_DASHES).lower().replace("ё", "е").split())


def occurs_in(value: str, text: str) -> bool:
    return normalize(value) in normalize(text)


def text_check(text: str) -> Callable[[TextExtraction], list[str]]:
    def check(extraction: TextExtraction) -> list[str]:
        problems = [f"{name}: «{value}» отсутствует в тексте обращения"
                    for name in TEXT_FIELDS if (value := getattr(extraction, name)) and not occurs_in(value, text)]
        # Цитата приводится в сообщении: без неё модель при повторе не видит, что исправлять.
        problems += [f"circumstances.{i}.quote: цитата «{c.quote}» отсутствует в тексте обращения — "
                     "выпиши её дословно" for i, c in enumerate(extraction.circumstances) if not occurs_in(c.quote, text)]
        return problems
    return check


def plausible_serial(value: str) -> bool:
    """Правдоподобный идентификатор: 4–30 символов, буквы, цифры, разделители.
    Отсекаются заполнители, которые модель изображений выдаёт вместо
    нечитаемого номера: подряд идущие цифры и длинные повторы символа."""
    serial = value.strip().upper()
    if _SERIAL.fullmatch(serial) is None or len(serial) > 30:
        return False
    return not any(run in serial for run in ("0123456", "1234567", "2345678", "3456789")) \
        and re.search(r"(.)\1{5,}", serial) is None


def model_tokens(designation: str) -> list[str]:
    """Части обозначения с цифрами: «M4103dw» из «HP LaserJet M4103dw»."""
    tokens = "".join(c if c.isalnum() else " " for c in designation.lower()).split()
    return [t for t in tokens if len(t) >= 2 and any(c.isdigit() for c in t)]


def contradicts(designation: str | None, manufacturer: str, model_name: str) -> bool:
    """Обозначение из текста противоречит карточке, если хотя бы одна его
    часть с цифрами не входит в наименование. Без таких частей («принтер HP»)
    сравнивать нечего — противоречия нет."""
    if not designation:
        return False
    full = f"{manufacturer} {model_name}".lower()
    return any(t not in full for t in model_tokens(designation))
