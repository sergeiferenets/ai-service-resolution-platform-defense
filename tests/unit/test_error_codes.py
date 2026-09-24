"""Справочник кодов ошибок: шаблоны, диапазоны, альтернативы, область модели."""

from __future__ import annotations

import pytest

from backend.rules.catalog import load_catalog
from backend.tools.error_codes import ErrorCodeDirectory

CODES = ErrorCodeDirectory.load(load_catalog().code_prefixes_ignored)


@pytest.mark.parametrize("code, model_id, entry", [
    ("50.2", "MOD-003", "50.xx"),                         # подстановка
    ("13.B2.D1", "MOD-003", "13.xx.yy"),
    ("C6000", "MOD-002", "Call service C6000"),           # префикс отброшен
    ("Call service C6000", "MOD-005", "Call service C6000"),
    ("Internal Error 05", "MOD-001", "Internal Error 03–12"),   # диапазон
    ("#846", "MOD-004", "#844 / #846"),                   # альтернатива
    ("Send error 1102", "MOD-002", "Send error ####"),    # четыре цифры
    ("50.2", None, "50.xx"),                              # модель неизвестна — весь справочник
])
def test_known_codes_are_found(code, model_id, entry):
    check = CODES.check(code, model_id)
    assert (check.status, check.entry_code) == ("в справочнике", entry)


@pytest.mark.parametrize("code, model_id", [
    ("8AF30001", "MOD-003"),      # код вне справочника
    ("C6600", "MOD-002"),
    ("F000", "MOD-005"),
    ("50.2", "MOD-002"),          # код другой модели
    ("Send error 12", "MOD-002"),
])
def test_unknown_codes_are_kept_as_free_features(code, model_id):
    check = CODES.check(code, model_id)
    assert (check.code, check.status, check.entry_code) == (code, "не найден", None)


def test_digest_is_stable():
    assert len(CODES.digest) == 12 and CODES.digest == ErrorCodeDirectory.load(()).digest
