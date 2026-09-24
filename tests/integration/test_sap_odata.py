"""Настоящий HTTP OData façade над PostgreSQL: не ответы MockTransport.

Окружение ERP_BASE_URL и MOCK_ERP_* задаётся изолированным тестовым стеком.
"""
import datetime as dt
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree

import httpx
import pytest

from backend.adapters.sap_odata import SapODataAdapter
from backend.domain.errors import AccessDenied, IdempotencyConflict, NotFound
from backend.domain.models import AuthContext, DraftRequest

BASE = os.environ.get('ERP_BASE_URL', 'http://mock-erp:8100/sap/opu/odata4/sap/zai_service/0001/')
CTX = AuthContext('U-002', 'Диспетчер', frozenset({'TER-SPB'}))
FOREIGN = AuthContext('U-001', 'Диспетчер', frozenset({'TER-MSK'}))


@pytest.fixture(scope='module')
def http():
    token=Path(os.environ['MOCK_ERP_TOKEN_FILE']).read_text().strip()
    with httpx.Client(base_url=BASE, headers={'Authorization':'Bearer '+token,'X-Auth-User':CTX.user_id,
                                             'X-Auth-Territories':'TER-SPB'},timeout=15) as client:
        yield client


@pytest.fixture(scope='module')
def erp():
    return SapODataAdapter(BASE, Path(os.environ['MOCK_ERP_TOKEN_FILE']).read_text().strip())


def draft():
    key=str(uuid.uuid4())
    return DraftRequest(key,'EQ-0011','onsite','external','SC-006',None,'warranty',key,{'test':'odata'})


def test_metadata_and_entity_sets(http):
    response=http.get('')
    assert response.status_code==200
    assert {r['name'] for r in response.json()['value']} == {'Equipment','BusinessPartners','ServiceContracts',
        'ServiceHistory','ServiceCenters','Technicians','Parts','Models','WorkOrderDrafts'}
    response=http.get('$metadata')
    assert response.status_code==200
    ElementTree.fromstring(response.content)
    assert 'ZAI_SERVICE' in response.text and response.headers['OData-Version']=='4.0'


def test_equipment_filter_key_select_expand(http,erp):
    result=erp.find_equipment_by_serial(CTX,'HPL-M4103-77842')
    assert result.equipment_id=='EQ-0011' and result.model.model_id=='MOD-003'
    assert erp.get_equipment(CTX,result.equipment_id)==result
    response=http.get("Equipment('EQ-0011')",params={'$select':'EquipmentId,ModelId','$expand':'Model,BusinessPartner'})
    assert response.status_code==200,response.text
    value=response.json()
    assert value['Model']['ModelId']=='MOD-003' and value['BusinessPartner']['BusinessPartnerId']=='CUST-008'
    assert 'SerialNumber' not in value
    assert erp.find_partner_equipment(CTX,'CUST-008','HP LaserJet M4103')[0].equipment_id=='EQ-0011'


def test_partner_contracts_history_and_selection_data(erp):
    assert erp.get_partner(CTX,'CUST-008').territory_id=='TER-SPB'
    contracts=erp.list_contracts(FOREIGN,'CUST-001',dt.date(2026,9,14))
    assert contracts and all(c.partner_id=='CUST-001' for c in contracts)
    assert all(c.valid_from <= dt.date(2026,9,14) <= c.valid_to for c in contracts)
    assert erp.service_history(FOREIGN,'EQ-0001')[0].service_order_id == 'SO-001023'
    assert erp.list_service_centers(CTX,'TER-SPB')
    assert erp.list_technicians(CTX,'TER-SPB')
    assert erp.list_parts(CTX,'MOD-003')
    assert erp.product_price(CTX,'MOD-003')>0


def test_draft_replay_conflict_and_parallel_requests(erp,http):
    value=draft()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:erp.create_draft(CTX,value),range(2)))
    assert results[0].document_id==results[1].document_id
    assert sorted(r.replayed for r in results)==[False,True]
    assert erp.create_draft(CTX,value).replayed
    from dataclasses import replace
    with pytest.raises(IdempotencyConflict):
        erp.create_draft(CTX,replace(value,facts_snapshot={'different':True}))
    response=http.get("WorkOrderDrafts('"+results[0].document_id+"')")
    assert response.status_code==200 and response.json()['FactsSnapshot']==value.facts_snapshot


def test_territory_denial_and_not_found(erp,http):
    with pytest.raises(AccessDenied): erp.get_equipment(FOREIGN,'EQ-0011')
    with pytest.raises(AccessDenied): erp.get_partner(FOREIGN,'CUST-008')
    with pytest.raises(AccessDenied): erp.list_service_centers(CTX,'TER-MSK')
    with pytest.raises(NotFound): erp.get_equipment(CTX,'EQ-NOT-FOUND')
    response=http.get("Equipment('EQ-0011')",headers={'X-Auth-User':'U-001','X-Auth-Territories':'TER-SPB,TER-MSK'})
    assert response.status_code==403 and response.json()['error']['code']=='access_denied'


def test_csrf_required_bound_to_caller_and_body_key(http):
    from dataclasses import asdict
    from backend.adapters.sap_odata import field_name
    value=draft();body={field_name(k):v for k,v in asdict(value).items()}
    headers={'Idempotency-Key':value.idempotency_key}
    assert http.post('WorkOrderDrafts',json=body,headers=headers).status_code==403
    token=http.get('',headers={'X-CSRF-Token':'Fetch'}).headers['X-CSRF-Token']
    headers['X-CSRF-Token']=token
    assert http.post('WorkOrderDrafts',json=body,headers={**headers,'X-Auth-User':'U-008'}).status_code==403
    assert http.post('WorkOrderDrafts',json=body,headers={**headers,'Idempotency-Key':'other-key'}).status_code==400
    assert http.post('WorkOrderDrafts',json=body,headers=headers).status_code==201


@pytest.mark.parametrize('params', [{'$filter':"SerialNumber eq 'x' or 1 eq 1"},
    {'$filter':"SerialNumber eq 'x'",'$top':'1'}, {'$select':'Password','$filter':"SerialNumber eq 'x'"},
    {'$filter':"SerialNumber eq 'x'",'$expand':'Unknown'}])
def test_unsupported_query_is_rejected_not_ignored(http,params):
    response=http.get('Equipment',params=params)
    assert response.status_code==400 and response.json()['error']['message']


def test_filter_literal_cannot_change_query(http):
    response=http.get('Equipment',params={'$filter':"SerialNumber eq 'x'' or ''1'' eq ''1'"})
    assert response.status_code==200 and response.json()['value']==[]


def test_all_collection_keys_are_readable(http,erp):
    samples={'ServiceContracts':('BusinessPartnerId','CUST-008','ContractId'),
             'ServiceHistory':('EquipmentId','EQ-0011','ServiceOrderId'),
             'ServiceCenters':('TerritoryId','TER-SPB','CenterId'),
             'Technicians':('TerritoryId','TER-SPB','TechnicianId'), 'Parts':('ModelId','MOD-003','PartId')}
    for entity,(field,value,key) in samples.items():
        response=http.get(entity,params={'$filter':f"{field} eq '{value}'"})
        assert response.status_code==200,response.text
        for row in response.json()['value'][:1]:
            result=http.get(f"{entity}('{row[key]}')")
            assert result.status_code==200,result.text
            assert result.json()[key]==row[key]
