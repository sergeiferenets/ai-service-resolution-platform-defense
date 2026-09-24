"""OData transport: контракт, retries, CSRF и обязательный контекст."""
import datetime as dt
import inspect
import json

import httpx
import pytest

from backend.adapters.port import PORT_METHODS
from backend.adapters.sap_odata import SapODataAdapter
from backend.domain.errors import AccessDenied, IdempotencyConflict, NotFound, SourceContractError, SourceUnavailable
from backend.domain.models import AuthContext, DraftRequest

CTX = AuthContext('U-002', 'Диспетчер', frozenset({'TER-SPB'}))
BASE = 'http://erp/sap/opu/odata4/sap/zai_service/0001/'


def adapter(handler):
    return SapODataAdapter(BASE, 'test-token', client=httpx.Client(transport=httpx.MockTransport(handler)), retry_delay_s=0)


def test_port_has_required_auth_context():
    for method in PORT_METHODS:
        params = list(inspect.signature(getattr(SapODataAdapter, method)).parameters.values())
        assert params[1].name == 'ctx' and params[1].default is inspect.Parameter.empty


@pytest.mark.parametrize('status,exception,count', [(401,AccessDenied,1),(403,AccessDenied,1),
    (404,NotFound,1),(409,IdempotencyConflict,1),(400,SourceContractError,1),(503,SourceUnavailable,2)])
def test_error_mapping_and_retry_limits(status, exception, count):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={'error': {'code':'ZAI/TEST','message':'Отказ источника'}})
    with pytest.raises(exception): adapter(respond).get_partner(CTX, 'CUST-008')
    assert len(calls) == count


@pytest.mark.parametrize('key,count', [(None,1),('decision-123',2)])
def test_write_timeout_retried_only_with_key(key,count):
    calls=[]
    def respond(request):
        calls.append(request)
        raise httpx.ReadTimeout('timeout', request=request)
    with pytest.raises(SourceUnavailable):
        adapter(respond)._request(CTX,'POST','WorkOrderDrafts',body={},idempotency_key=key)
    assert len(calls)==count


def test_serial_is_escaped_and_auth_scope_transmitted():
    def respond(request):
        assert request.url.params['$filter'] == "SerialNumber eq 'SN''1'"
        assert request.url.params['$expand'] == 'Model'
        assert request.headers['X-Auth-User'] == CTX.user_id
        assert request.headers['X-Auth-Territories'] == 'TER-SPB'
        assert request.url.path.endswith('/Equipment')
        return httpx.Response(200,json={'value':[]})
    with pytest.raises(NotFound): adapter(respond).find_equipment_by_serial(CTX,"SN'1")


@pytest.mark.parametrize('body', [{'value':'bad'},{'value':[{}],'@odata.nextLink':'http://external/'},[]])
def test_malformed_collection_fails_closed(body):
    with pytest.raises(SourceContractError):
        adapter(lambda r:httpx.Response(200,json=body)).find_equipment_by_serial(CTX,'SN1')


def test_write_fetches_csrf_and_keeps_same_body_during_retry():
    calls=[]
    def respond(request):
        calls.append(request)
        if request.method=='GET':
            assert request.headers['X-CSRF-Token']=='Fetch'
            return httpx.Response(200,json={'value':[]},headers={'X-CSRF-Token':'token-for-user'})
        assert request.headers['X-CSRF-Token']=='token-for-user'
        assert request.headers['Idempotency-Key']=='decision-123'
        body=json.loads(request.content)
        assert body['FactsSnapshot']=={'original_key':42}
        if len(calls)==2: return httpx.Response(503)
        return httpx.Response(200,json={'DocumentId':'WO-1','IdempotencyKey':'decision-123','Replayed':True})
    draft=DraftRequest('decision-123','EQ-0011','onsite','internal',None,None,'paid','decision-123',{'original_key':42})
    result=adapter(respond).create_draft(CTX,draft)
    assert result.replayed and result.document_id=='WO-1'
    assert calls[1].content==calls[2].content and len(calls)==3


def test_live_application_uses_only_integration_adapter():
    from pathlib import Path
    source=(Path(__file__).resolve().parents[2]/'backend/api/main.py').read_text(encoding='utf-8')
    assert 'SapODataAdapter(settings.erp_base_url' in source
    assert 'MockErpAdapter' not in source
