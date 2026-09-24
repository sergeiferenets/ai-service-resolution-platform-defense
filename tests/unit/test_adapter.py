"""Порт, адаптер мока и кэш на одно обращение — без сети, на подменном транспорте."""

from __future__ import annotations

import datetime as dt
import inspect
import typing

import httpx
import pytest

from backend.adapters.cache import RequestScopedCache
from backend.adapters.mock_erp import Budgets, MockErpAdapter
from backend.orchestrator.recording import RecordingErp
from backend.adapters.port import PORT_METHODS, ErpPort
from backend.domain.errors import AccessDenied, IdempotencyConflict, NotFound, SourceUnavailable
from backend.domain.models import AuthContext, DraftRequest

SPB = AuthContext(user_id="U-002", role="Диспетчер", territories=frozenset({"TER-SPB"}))
MSK = AuthContext(user_id="U-001", role="Диспетчер", territories=frozenset({"TER-MSK"}))
FAST = Budgets(read_timeout_s=0.1, write_timeout_s=0.1, retry_delay_s=0)

EQUIPMENT = {
    "equipment_id": "EQ-0011", "serial_number": "HPL-M4103-77842",
    "model": {"model_id": "MOD-003", "manufacturer": "HP", "name": "LaserJet Pro M4103dw",
              "equipment_type": "МФУ лазерное (А4)"},
    "partner_id": "CUST-008", "territory_id": "TER-SPB", "sale_date": "2026-02-12",
    "location": "СПб", "status": "В эксплуатации", "assigned_technician_id": None,
}


def adapter(handler) -> MockErpAdapter:
    client = httpx.Client(base_url="http://mock", transport=httpx.MockTransport(handler))
    return MockErpAdapter("http://mock", "token", budgets=FAST, client=client)


def draft(key: str = "decision-0001") -> DraftRequest:
    return DraftRequest(key, "EQ-0011", "onsite", "external", "SC-006", None, "warranty", key, {"code": "50.2"})


# --- контекст авторизации обязателен по сигнатуре --------------------------------------------

@pytest.mark.parametrize("impl", [ErpPort, MockErpAdapter, RequestScopedCache, RecordingErp])
def test_every_method_requires_auth_context_by_signature(impl):
    for name in PORT_METHODS:
        fn = getattr(impl, name)
        params = list(inspect.signature(fn).parameters.values())
        ctx = params[1]
        assert ctx.name == "ctx", f"{impl.__name__}.{name}: первый параметр — ctx"
        assert ctx.default is inspect.Parameter.empty, f"{impl.__name__}.{name}: ctx без значения по умолчанию"
        assert typing.get_type_hints(fn)["ctx"] is AuthContext, f"{impl.__name__}.{name}: ctx — AuthContext"


def test_call_without_context_is_rejected():
    a = adapter(lambda r: httpx.Response(200, json=EQUIPMENT))
    with pytest.raises(TypeError):
        a.get_equipment(None, "EQ-0011")
    with pytest.raises(TypeError):
        RequestScopedCache(a).get_equipment(None, "EQ-0011")


# --- адаптер -------------------------------------------------------------------------------------

def test_context_travels_in_headers_and_maps_to_domain():
    seen = {}

    def handler(request: httpx.Request):
        seen.update(request.headers)
        return httpx.Response(200, json=EQUIPMENT)

    eq = adapter(handler).find_equipment_by_serial(SPB, "HPL-M4103-77842")
    assert seen["x-auth-user"] == "U-002" and seen["x-auth-territories"] == "TER-SPB"
    assert seen["authorization"] == "Bearer token"
    assert eq.model.manufacturer == "HP" and eq.sale_date == dt.date(2026, 2, 12)


def test_forbidden_becomes_access_denied_not_empty_result():
    body = {"detail": {"error": "access_denied", "reason": "объект другой территории",
                       "object_type": "equipment", "object_id": "EQ-0011"}}
    with pytest.raises(AccessDenied) as e:
        adapter(lambda r: httpx.Response(403, json=body)).get_equipment(MSK, "EQ-0011")
    assert e.value.object_id == "EQ-0011"


def test_not_found():
    with pytest.raises(NotFound):
        adapter(lambda r: httpx.Response(404, json={"detail": "нет"})).get_equipment(SPB, "EQ-9999")


def test_read_retried_once_then_source_unavailable():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(SourceUnavailable):
        adapter(handler).get_equipment(SPB, "EQ-0011")
    assert len(calls) == 2


def test_server_error_retried_once():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=EQUIPMENT)

    assert adapter(handler).get_equipment(SPB, "EQ-0011").equipment_id == "EQ-0011"
    assert len(calls) == 2


def test_write_with_idempotency_key_is_retried():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(200, json={"document_id": "WO-000001", "idempotency_key": "decision-0001",
                                         "replayed": True})

    result = adapter(handler).create_draft(SPB, draft())
    assert result.document_id == "WO-000001" and result.replayed
    assert len(calls) == 2


def test_idempotency_conflict():
    with pytest.raises(IdempotencyConflict):
        adapter(lambda r: httpx.Response(409, json={"detail": "другое содержимое"})).create_draft(SPB, draft())


# --- кэш ---------------------------------------------------------------------------------------

def counting():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=EQUIPMENT)

    return calls, handler


def test_cache_reuses_answer_within_request_for_same_context():
    calls, handler = counting()
    cache = RequestScopedCache(adapter(handler))
    cache.get_equipment(SPB, "EQ-0011")
    cache.get_equipment(SPB, "EQ-0011")
    assert len(calls) == 1 and cache.hits == 1


def test_cache_key_includes_auth_context():
    calls, handler = counting()
    cache = RequestScopedCache(adapter(handler))
    other = AuthContext(user_id="U-019", role="Клиент", territories=frozenset({"TER-SPB"}), partner_id="CUST-008")
    cache.get_equipment(SPB, "EQ-0011")
    cache.get_equipment(other, "EQ-0011")
    assert len(calls) == 2


def test_denials_are_not_cached():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(403, json={"detail": {"reason": "объект другой территории"}})

    cache = RequestScopedCache(adapter(handler))
    for _ in range(2):
        with pytest.raises(AccessDenied):
            cache.get_equipment(MSK, "EQ-0011")
    assert len(calls) == 2, "каждый отказ идёт в источник и попадает в его аудит"


def test_writes_are_not_cached():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(201, json={"document_id": "WO-000001", "idempotency_key": "k" * 8, "replayed": False})

    cache = RequestScopedCache(adapter(handler))
    cache.create_draft(SPB, draft("k" * 8))
    cache.create_draft(SPB, draft("k" * 8))
    assert len(calls) == 2
