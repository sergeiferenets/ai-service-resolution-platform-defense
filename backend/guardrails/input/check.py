"""Входной контроль текста обращения (security.md, раздел 3.1).

Работает правилами, до попадания текста в контекст модели:
1. технически некорректный ввод — пустой текст, превышение длины,
   недопустимые символы;
2. попытка внедрения инструкций — обращение отклоняется с уведомлением;
3. запрос вне предметной области — вежливый отказ без обработки;
4. персональные данные — помечаются позициями для журналирования. Текст не
   искажается: он несёт диагностическую информацию.

Словари — patterns.yaml; их хеш входит в rules_version. Тот же поиск
внедрения применяется к тексту, распознанному с изображения (раздел 3.2).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

PATTERNS = Path(__file__).with_name("patterns.yaml")
_INVALID_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f�]")

Category = Literal["technical", "injection", "out_of_domain"]


@dataclass(frozen=True)
class PiiMark:
    kind: str
    start: int
    end: int


@dataclass(frozen=True)
class InputVerdict:
    decision: Literal["pass", "reject"]
    category: Category | None
    reasons: tuple[str, ...]
    notice: str | None
    pii: tuple[PiiMark, ...]


@dataclass(frozen=True)
class InputRules:
    digest: str
    max_length: int
    injection: tuple[re.Pattern, ...]
    domain_words: tuple[str, ...]
    domain_patterns: tuple[re.Pattern, ...]
    pii: dict[str, re.Pattern]
    notices: dict[str, str]


def load_rules(path: Path = PATTERNS) -> InputRules:
    raw = path.read_bytes()
    spec = yaml.safe_load(raw)
    return InputRules(
        digest=hashlib.sha256(raw).hexdigest()[:12],
        max_length=int(spec["max_length"]),
        injection=tuple(re.compile(p, re.IGNORECASE | re.MULTILINE) for p in spec["injection"]),
        domain_words=tuple(w.lower() for w in spec["domain"]["words"]),
        domain_patterns=tuple(re.compile(p) for p in spec["domain"]["patterns"]),
        pii={kind: re.compile(p) for kind, p in spec["pii"].items()},
        notices=dict(spec["notices"]),
    )


def find_injection(text: str, rules: InputRules) -> list[str]:
    return [m.group(0)[:80] for p in rules.injection if (m := p.search(text))]


def in_domain(text: str, rules: InputRules) -> bool:
    low = text.lower()
    return any(w in low for w in rules.domain_words) or any(p.search(text) for p in rules.domain_patterns)


def mark_pii(text: str, rules: InputRules) -> tuple[PiiMark, ...]:
    marks = [PiiMark(kind, m.start(), m.end()) for kind, p in rules.pii.items() for m in p.finditer(text)]
    return tuple(sorted(marks, key=lambda m: (m.start, -m.end)))


def redact(text: str, marks: tuple[PiiMark, ...]) -> str:
    """Для журналов: помеченные фрагменты заменяются названием вида.
    Модели передаётся исходный текст."""
    parts, pos = [], 0
    for m in marks:
        if m.start < pos:
            continue
        parts += [text[pos:m.start], f"[{m.kind}]"]
        pos = m.end
    parts.append(text[pos:])
    return "".join(parts)


def redactor(text: str, marks: tuple[PiiMark, ...]) -> Callable[[str], str]:
    """Маскировка для журналов по значениям, а не по позициям: помеченный
    фрагмент заменяется и там, где текст вставлен в шаблон запроса."""
    fragments = sorted({(text[m.start:m.end], m.kind) for m in marks}, key=lambda f: -len(f[0]))

    def apply(value: str) -> str:
        for fragment, kind in fragments:
            value = value.replace(fragment, f"[{kind}]")
        return value
    return apply


def _reject(rules: InputRules, category: Category, reasons: list[str], pii=()) -> InputVerdict:
    return InputVerdict("reject", category, tuple(reasons), rules.notices[category], tuple(pii))


def check_input(text: str | None, rules: InputRules) -> InputVerdict:
    if not text or not text.strip():
        return _reject(rules, "technical", ["пустой текст"])
    if len(text) > rules.max_length:
        return _reject(rules, "technical", [f"длина {len(text)} больше {rules.max_length}"])
    if _INVALID_CHARS.search(text):
        return _reject(rules, "technical", ["недопустимые символы"])
    pii = mark_pii(text, rules)
    hits = find_injection(text, rules)
    if hits:
        return _reject(rules, "injection", [f"указание, адресованное системе: «{h}»" for h in hits], pii)
    if not in_domain(text, rules):
        return _reject(rules, "out_of_domain", ["нет признаков предметной области"], pii)
    return InputVerdict("pass", None, (), None, pii)
