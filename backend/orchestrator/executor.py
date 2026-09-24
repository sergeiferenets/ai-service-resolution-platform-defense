"""Исполнитель действий (ADR-0002): единственная запись в учётную систему.

Запись выполняется только по сохранённому разрешению на исполнение
(EXECUTION_GRANT): его выдаёт решение человека — подтверждение рекомендации
или согласование. Модели состояний недостаточно: между проверкой состояния
и записью проходит время, за которое решение может быть отменено. Поэтому
исполнитель перед обращением к источнику проверяет само разрешение, его
принадлежность текущему решению, состояние обращения и статус согласования.

Ключ идемпотентности — идентификатор решения из разрешения, поэтому повтор
после сбоя, повторное подтверждение или гонка двух подтверждений дают один
документ (ADR-0006).

Тело запроса собирается только из сохранённого решения и снимка фактов, а
не из текущего времени или повторного чтения источника: повтор с тем же
ключом обязан быть тем же запросом, иначе источник ответит конфликтом.
"""

from __future__ import annotations

import uuid

from backend.adapters.port import ErpPort
from backend.domain.errors import ExecutionNotAuthorized
from backend.domain.models import AuthContext, DraftRequest, DraftResult
from backend.orchestrator.recording import RecordingErp
from backend.orchestrator.states import State
from backend.orchestrator.store import Store

# Состояния, в которых разрешение действует: ожидание решения и уже созданный
# черновик (повтор после сбоя). Отклонённое или эскалированное обращение
# исполнению не подлежит.
EXECUTABLE = frozenset({State.AWAITING_CONFIRMATION, State.AWAITING_APPROVAL, State.DRAFT_CREATED})


class DraftExecutor:
    def __init__(self, store: Store, erp: ErpPort):
        self._store = store
        self._erp = erp

    def authorize(self, request_id: uuid.UUID) -> dict:
        """Сохранённое разрешение на исполнение или отказ до обращения к источнику."""
        req = self._store.get_request(request_id)
        if req is None:
            raise KeyError(f"Обращение {request_id} не найдено")
        grant = self._store.execution_grant(request_id)
        if grant is None:
            raise ExecutionNotAuthorized(f"Обращение {request_id}: нет разрешения на исполнение,"
                                         " решение человека не сохранено")
        if State(req["status"]) not in EXECUTABLE:
            raise ExecutionNotAuthorized(f"Обращение {request_id} в состоянии «{req['status']}»:"
                                         " исполнение не выполняется")
        decision = self._store.decision(request_id)
        if decision is None or decision["id"] != grant["decision_id"]:
            raise ExecutionNotAuthorized(f"Разрешение выдано на другое решение: {grant['decision_id']}")
        approval = decision["approval"]
        if approval is not None and approval["status"] != "получено":
            raise ExecutionNotAuthorized(f"Согласование в состоянии «{approval['status']}»")
        return grant

    def build(self, request_id: uuid.UUID) -> tuple[DraftRequest, uuid.UUID]:
        grant = self.authorize(request_id)
        snapshot = self._store.step_output(request_id, "recommendation")
        if snapshot is None:
            raise ExecutionNotAuthorized(f"У обращения {request_id} нет снимка решения: создавать нечего")
        rec, facts = snapshot["output"], snapshot["facts"]
        # Ключ — решение, на которое выдано разрешение: повтор даёт тот же запрос.
        key = str(grant["decision_id"])
        return DraftRequest(
            idempotency_key=key, equipment_id=facts["equipment_id"], execution_mode=rec["execution_mode"],
            fulfillment_type=rec["fulfillment_type"], service_center_id=rec["service_center_id"],
            technician_id=rec["technician_id"], warranty_preliminary=rec["warranty"]["status"],
            decision_ref=key, facts_snapshot=facts), grant["decision_id"]

    def create(self, request_id: uuid.UUID, ctx: AuthContext, *, step_id: uuid.UUID) -> DraftResult:
        draft, decision_id = self.build(request_id)
        source = RecordingErp(self._erp, self._store, request_id=request_id, step_id=step_id)
        result = source.create_draft(ctx, draft)
        created = self._store.save_draft(decision_id, external_document_id=result.document_id,
                                         idempotency_key=draft.idempotency_key, payload=draft.facts_snapshot)
        self._store.audit(request_id, ctx.user_id, "draft_created" if created else "draft_repeated",
                          after={"document_id": result.document_id, "idempotency_key": draft.idempotency_key,
                                 "replayed_by_source": result.replayed})
        return result
