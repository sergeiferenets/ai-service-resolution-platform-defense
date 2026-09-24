"""Справочник кодов ошибок (02_error_codes.csv) и проверка кода из обращения.

Коды в справочнике записаны шаблонами:
- альтернативы через « / »: `#844 / #846`, `Add toner / Replace toner`;
- подстановки в числовых кодах: `50.xx`, `13.xx.yy` — любая группа символов;
- `####` — четыре цифры: `Send error ####`;
- диапазон: `Internal Error 03–12`.

Сравнение без учёта регистра; префиксы, которые движок правил отбрасывает
(`Call service`, `Internal Error`), отбрасываются и здесь: «C6000» и
«Call service C6000» — один код.

Код вне справочника не отбрасывается: результат «не найден», и код идёт
дальше как свободный признак. Хеш файла входит в rules_version.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from data.loader.reference import REF

ERROR_CODES_CSV = REF / "02_error_codes.csv"


@dataclass(frozen=True)
class CodeEntry:
    code: str
    model_id: str | None          # None — для всех моделей
    description: str
    required_competence: str
    typical_part: str


@dataclass(frozen=True)
class CodeCheck:
    code: str
    status: Literal["в справочнике", "не найден"]
    entry_code: str | None
    description: str | None
    typical_part: str | None


def _normalize(value: str, prefixes: tuple[str, ...]) -> str:
    value = " ".join(value.split()).lower()
    for prefix in prefixes:
        if value.startswith(prefix.lower()):
            value = value[len(prefix):].strip()
    return value


def _matcher(alternative: str, prefixes: tuple[str, ...]) -> Callable[[str], bool]:
    alt = _normalize(alternative, prefixes)
    span = re.fullmatch(r"(.*?)(\d+)\s*[–-]\s*(\d+)", alt)
    if span:
        head, low, high = span.group(1), int(span.group(2)), int(span.group(3))
        return lambda code: (code.startswith(head) and code[len(head):].strip().isdigit()
                             and low <= int(code[len(head):].strip()) <= high)
    if re.fullmatch(r"[\dxy.]+", alt):
        pattern = re.sub(r"[xy]+", "[0-9a-z]+", re.escape(alt))
    else:
        pattern = re.escape(alt).replace(re.escape("####"), r"\d{4}")
    compiled = re.compile(pattern)
    return lambda code: compiled.fullmatch(code) is not None


class ErrorCodeDirectory:
    def __init__(self, entries: list[CodeEntry], digest: str, prefixes: tuple[str, ...]):
        self.entries = entries
        self.digest = digest
        self._prefixes = prefixes
        self._matchers = [[_matcher(a, prefixes) for a in re.split(r"\s+/\s+", e.code)] for e in entries]

    @classmethod
    def load(cls, prefixes: tuple[str, ...], path: Path = ERROR_CODES_CSV) -> "ErrorCodeDirectory":
        raw = path.read_bytes()
        with path.open(encoding="utf-8") as f:
            entries = [CodeEntry(code=row["error_code"].strip(),
                                 model_id=None if row["model_id или Все"].strip() == "Все" else row["model_id или Все"].strip(),
                                 description=row["Расшифровка"], required_competence=row["Требуемая компетенция"],
                                 typical_part=row["Типовая запчасть"]) for row in csv.DictReader(f)]
        return cls(entries, hashlib.sha256(raw).hexdigest()[:12], prefixes)

    @staticmethod
    def parts_for(typical_part: str | None, parts) -> list:
        """Запчасти под типовую запчасть из справочника кодов.

        Сопоставление по значимым словам наименования: «Узел закрепления
        (печка)» находит «Узел закрепления (печка) для HP M404». Так
        подозреваемая запчасть берётся из справочника, а не из догадки
        модели, и остаётся проверяемой."""
        if not typical_part:
            return []
        words = [w for w in re.split(r"[^\w]+", typical_part.lower()) if len(w) >= 5]
        if not words:
            return []
        return [p for p in parts if any(w in p.name.lower() for w in words)]

    def check(self, code: str, model_id: str | None) -> CodeCheck:
        normalized = _normalize(code, self._prefixes)
        for entry, matchers in zip(self.entries, self._matchers):
            if model_id is not None and entry.model_id not in (None, model_id):
                continue
            if any(m(normalized) for m in matchers):
                return CodeCheck(code, "в справочнике", entry.code, entry.description, entry.typical_part or None)
        return CodeCheck(code, "не найден", None, None, None)
