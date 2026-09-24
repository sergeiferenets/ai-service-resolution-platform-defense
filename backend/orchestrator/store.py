"""Хранилище состояния платформы (PostgreSQL, data-model.md).

Единственный механизм персистентности процесса (ADR-0002): шаг, снимок
состояния, вызовы инструментов, версия набора артефактов. Отдельного
формата контрольных точек нет. Каждый переход состояния пишется в
AUDIT_EVENT той же транзакцией, что и сам переход.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from typing import Any, Iterable

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from backend.domain.errors import ExecutionNotAuthorized
from backend.domain.models import AuthContext
from backend.llm.structured import Image
from backend.orchestrator.artifacts import ArtifactVersion
from backend.orchestrator.serialize import to_jsonable
from backend.orchestrator.states import IllegalTransition, State, check_transition


def _json(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(to_jsonable(value))


class Store:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def _one(self, query: str, params: tuple = ()) -> dict | None:
        with self.pool.connection() as conn:
            return conn.cursor(row_factory=dict_row).execute(query, params).fetchone()

    def _all(self, query: str, params: tuple = ()) -> list[dict]:
        with self.pool.connection() as conn:
            return conn.cursor(row_factory=dict_row).execute(query, params).fetchall()

    def _exec(self, query: str, params: tuple = ()) -> None:
        with self.pool.connection() as conn:
            conn.execute(query, params)

    # --- доступ -----------------------------------------------------------------------------------

    def auth_context(self, user_id: str) -> AuthContext | None:
        user = self._one("SELECT user_id, role, partner_id FROM app_user WHERE user_id = %s AND is_active",
                         (user_id,))
        if user is None:
            return None
        rows = self._all("SELECT territory_id FROM user_territory WHERE user_id = %s", (user_id,))
        return AuthContext(user["user_id"], user["role"], frozenset(r["territory_id"] for r in rows),
                           user["partner_id"])

    def ensure_artifact_version(self, av: ArtifactVersion) -> str:
        fields = to_jsonable(av)
        placeholders = ", ".join(["%s"] * (len(fields) + 1))
        self._exec(f"INSERT INTO artifact_version (id, {', '.join(fields)}) VALUES ({placeholders})"
                   " ON CONFLICT (id) DO NOTHING", (av.id, *fields.values()))
        return av.id

    # --- обращение, вложения, переходы --------------------------------------------------------------

    def create_request(self, *, raw_text: str, channel: str, impact: str | None, created_by: str,
                       partner_id: str | None = None) -> uuid.UUID:
        """partner_id — партнёр, от имени которого обращение: представитель
        партнёра или указанный сотрудником. Нужен для определения оборудования
        без серийного номера (PRD 5.3); идентификация его подтверждает."""
        rid = uuid.uuid4()
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO service_request (id, raw_text, channel, impact, status, created_by,"
                         " external_partner_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                         (rid, raw_text, channel, impact, State.ACCEPTED.value, created_by, partner_id))
            conn.execute("INSERT INTO audit_event (request_id, actor_user_id, event_type, after_state)"
                         " VALUES (%s, %s, 'request_created', %s)", (rid, created_by, _json({"state": State.ACCEPTED})))
        return rid

    def save_attachment(self, request_id: uuid.UUID, image: Image, kind: str = "фото таблички") -> None:
        self._exec("INSERT INTO request_attachment (id, request_id, kind, content_type, size_bytes, sha256, data)"
                   " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                   (uuid.uuid4(), request_id, kind, image.content_type, len(image.data),
                    hashlib.sha256(image.data).hexdigest(), image.data))

    def attachment(self, request_id: uuid.UUID, kind: str = "фото таблички") -> Image | None:
        row = self._one("SELECT content_type, data FROM request_attachment WHERE request_id = %s AND kind = %s"
                        " ORDER BY created_at LIMIT 1", (request_id, kind))
        return Image(row["content_type"], bytes(row["data"])) if row else None

    def get_request(self, request_id: uuid.UUID) -> dict | None:
        return self._one("SELECT * FROM service_request WHERE id = %s", (request_id,))

    def transition(self, request_id: uuid.UUID, target: State, *, actor_user_id: str,
                   reason: str | None = None, require_no_execution_grant: bool = False) -> None:
        """require_no_execution_grant — для отказа человека: если разрешение на
        исполнение уже выдано, отказ отклоняется. Обе операции берут строку
        обращения на блокировку, поэтому происходит ровно одна из них."""
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute("SELECT status FROM service_request WHERE id = %s FOR UPDATE",
                               (request_id,)).fetchone()
            if row is None:
                raise KeyError(f"Обращение {request_id} не найдено")
            current = State(row[0])
            check_transition(current, target)
            if require_no_execution_grant and conn.execute(
                    "SELECT 1 FROM execution_grant WHERE request_id = %s", (request_id,)).fetchone():
                raise IllegalTransition("Исполнение уже разрешено решением человека: отказ невозможен")
            conn.execute("UPDATE service_request SET status = %s, status_reason = %s, updated_at = now()"
                         " WHERE id = %s", (target.value, reason, request_id))
            conn.execute("INSERT INTO audit_event (request_id, actor_user_id, event_type, before_state, after_state)"
                         " VALUES (%s, %s, 'state_changed', %s, %s)",
                         (request_id, actor_user_id, _json({"state": current}),
                          _json({"state": target, "reason": reason})))

    def save_identification(self, request_id: uuid.UUID, *, serial_number: str | None, confidence_level: str,
                            discrepancy_flag: bool, source: str, equipment_id: str | None = None,
                            partner_id: str | None = None, territory_id: str | None = None) -> None:
        """Без equipment_id — номер указан, но в справочнике не найден: запись
        идентификации есть, ссылки на оборудование у обращения нет."""
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO identification (id, request_id, serial_number, confidence_level,"
                         " discrepancy_flag, source) VALUES (%s, %s, %s, %s, %s, %s)",
                         (uuid.uuid4(), request_id, serial_number, confidence_level, discrepancy_flag, source))
            if equipment_id is not None:
                conn.execute("UPDATE service_request SET external_equipment_id = %s, external_partner_id = %s,"
                             " territory_id = %s, updated_at = now() WHERE id = %s",
                             (equipment_id, partner_id, territory_id, request_id))

    def identification(self, request_id: uuid.UUID) -> dict | None:
        return self._one("SELECT serial_number, confidence_level, discrepancy_flag, source FROM identification"
                         " WHERE request_id = %s", (request_id,))

    # --- шаги, вызовы, аудит ----------------------------------------------------------------------

    def start_step(self, request_id: uuid.UUID, step_name: str, actor: str, artifact_version: str,
                   state: State) -> uuid.UUID:
        step_id = uuid.uuid4()
        self._exec("INSERT INTO process_step (id, request_id, seq, step_name, actor, status, state_snapshot,"
                   " artifact_version) VALUES (%s, %s,"
                   " (SELECT COALESCE(MAX(seq), 0) + 1 FROM process_step WHERE request_id = %s),"
                   " %s, %s, 'выполняется', %s, %s)",
                   (step_id, request_id, request_id, step_name, actor, _json({"state": state}), artifact_version))
        return step_id

    def finish_step(self, step_id: uuid.UUID, status: str, snapshot: dict) -> None:
        self._exec("UPDATE process_step SET status = %s, state_snapshot = state_snapshot || %s,"
                   " finished_at = clock_timestamp() WHERE id = %s", (status, _json(snapshot), step_id))

    def record_tool_call(self, step_id: uuid.UUID, tool_name: str, request_payload: Any, response_payload: Any,
                         duration_ms: int, outcome: str) -> None:
        self._exec("INSERT INTO tool_call (id, step_id, tool_name, request_payload, response_payload, duration_ms,"
                   " outcome) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                   (uuid.uuid4(), step_id, tool_name, _json(request_payload), _json(response_payload), duration_ms,
                    outcome))

    def audit(self, request_id: uuid.UUID | None, actor_user_id: str | None, event_type: str, *,
              before: Any = None, after: Any = None) -> None:
        self._exec("INSERT INTO audit_event (request_id, actor_user_id, event_type, before_state, after_state)"
                   " VALUES (%s, %s, %s, %s, %s)", (request_id, actor_user_id, event_type, _json(before), _json(after)))

    def audit_events(self, request_id: uuid.UUID) -> list[dict]:
        return self._all("SELECT actor_user_id, event_type, before_state, after_state, occurred_at FROM audit_event"
                         " WHERE request_id = %s ORDER BY occurred_at, id", (request_id,))

    def step_output(self, request_id: uuid.UUID, step_name: str) -> dict | None:
        row = self._one("SELECT state_snapshot FROM process_step WHERE request_id = %s AND step_name = %s"
                        " AND status = 'выполнен' ORDER BY seq DESC LIMIT 1", (request_id, step_name))
        return row["state_snapshot"] if row else None

    # Снимки шагов читаются с учётом прав читателя: ограничение выполняется в
    # запросе, закрытый материал не покидает хранилище (PRD, раздел 3.3).
    RESTRICTED_NOTICE = ("Обоснование опирается на материалы ограниченного доступа "
                         "и доступно сотрудникам сервисной организации.")
    # Технические поля разбора: трассировка работы агента, а кандидаты вдобавок
    # относятся к чужому оборудованию, найденному под правами автора обращения.
    TECHNICAL_CONTEXT = ("extraction", "plate", "code_check", "candidates")

    def recommendation_snapshot(self, request_id: uuid.UUID, levels: Iterable[str]) -> dict | None:
        """Снимок рекомендации. Если хоть один источник гипотезы недоступен
        читателю, гипотеза, уровень обоснования и ссылки не возвращаются:
        скрывается вывод целиком, а не отдельные слова в нём."""
        row = self._one(
            "WITH snapshot AS ("
            "  SELECT state_snapshot AS snap FROM process_step"
            "   WHERE request_id = %s AND step_name = 'recommendation' AND status = 'выполнен'"
            "   ORDER BY seq DESC LIMIT 1)"
            " SELECT CASE WHEN NOT EXISTS ("
            "          SELECT 1 FROM jsonb_array_elements(COALESCE(snap -> 'output' -> 'sources', '[]'::jsonb)) source"
            "           WHERE source ->> 'confidentiality_level' <> ALL(%s))"
            "        THEN snap"
            "        ELSE jsonb_set(jsonb_set("
            "               ((snap #- '{output,hypothesis}') #- '{output,evidence_level}') #- '{output,sources}',"
            "               '{output,sources_restricted}', 'true'::jsonb),"
            "               '{output,restriction_notice}', to_jsonb(%s::text))"
            "        END AS snap FROM snapshot", (request_id, list(levels), self.RESTRICTED_NOTICE))
        return row["snap"] if row else None

    def context_snapshot(self, request_id: uuid.UUID, *, technical: bool) -> dict | None:
        """Снимок идентификации. Без права на трассировку технические поля
        остаются в хранилище."""
        drop = "".join(f" #- '{{output,{field}}}'" for field in self.TECHNICAL_CONTEXT)
        row = self._one(
            f"SELECT CASE WHEN %s THEN state_snapshot ELSE state_snapshot{drop} END AS snap"
            " FROM process_step WHERE request_id = %s AND step_name = 'identification' AND status = 'выполнен'"
            " ORDER BY seq DESC LIMIT 1", (technical, request_id))
        return row["snap"] if row else None

    def steps(self, request_id: uuid.UUID) -> list[dict]:
        steps = self._all(
            "SELECT s.id, s.seq, s.step_name, s.actor, s.status, s.state_snapshot, s.started_at, s.finished_at,"
            " to_jsonb(a) AS artifact_version FROM process_step s JOIN artifact_version a ON a.id = s.artifact_version"
            " WHERE s.request_id = %s ORDER BY s.seq", (request_id,))
        calls = self._all(
            "SELECT c.step_id, c.tool_name, c.request_payload, c.response_payload, c.duration_ms, c.outcome"
            " FROM tool_call c JOIN process_step s ON s.id = c.step_id WHERE s.request_id = %s"
            " ORDER BY c.called_at, c.id", (request_id,))
        by_step: dict[uuid.UUID, list[dict]] = defaultdict(list)
        for c in calls:
            by_step[c.pop("step_id")].append(c)
        for s in steps:
            s["tool_calls"] = by_step.get(s["id"], [])
        return steps

    # --- диагноз, решение, согласование, черновик ---------------------------------------------------

    def save_diagnosis(self, request_id: uuid.UUID, hypothesis: str, evidence_level: str,
                       rule_ids: Iterable[str], chunk_ids: Iterable[str] = ()) -> uuid.UUID:
        """Доказательства диагноза: применённое правило и фрагменты корпуса.
        Без записей в EVIDENCE рекомендация не выдаётся (AC-2)."""
        diagnosis_id = uuid.uuid4()
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO diagnosis (id, request_id, hypothesis, evidence_level) VALUES (%s, %s, %s, %s)",
                         (diagnosis_id, request_id, hypothesis, evidence_level))
            for rule_id in rule_ids:
                conn.execute("INSERT INTO evidence (id, diagnosis_id, source_type, rule_id)"
                             " VALUES (%s, %s, 'правило', %s)", (uuid.uuid4(), diagnosis_id, rule_id))
            for chunk_id in chunk_ids:
                conn.execute("INSERT INTO evidence (id, diagnosis_id, source_type, chunk_id)"
                             " VALUES (%s, %s, 'документ', %s)", (uuid.uuid4(), diagnosis_id, chunk_id))
        return diagnosis_id

    def save_decision(self, request_id: uuid.UUID, *, execution_mode: str, fulfillment_type: str | None,
                      classifier_value: str, warranty_preliminary: str, warranty_is_preliminary: bool,
                      applied_rule_id: str, considered_rules: Any, service_center_id: str | None,
                      candidates: Iterable[Any], approval_level: str | None) -> uuid.UUID:
        decision_id = uuid.uuid4()
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO decision (id, request_id, execution_mode, fulfillment_type, classifier_value,"
                " warranty_preliminary, warranty_is_preliminary, applied_rule_id, considered_rules, service_center_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (decision_id, request_id, execution_mode, fulfillment_type, classifier_value, warranty_preliminary,
                 warranty_is_preliminary, applied_rule_id, _json(considered_rules), service_center_id))
            for c in candidates:
                is_technician = c.candidate_type == "исполнитель"
                conn.execute(
                    "INSERT INTO candidate (id, decision_id, candidate_type, external_technician_id,"
                    " external_service_center_id, rank, rationale, selection_basis, rejection_reason)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (uuid.uuid4(), decision_id, c.candidate_type, c.candidate_id if is_technician else None,
                     None if is_technician else c.candidate_id, c.rank, c.rationale, c.selection_basis,
                     c.rejection_reason))
            if approval_level is not None:
                conn.execute("INSERT INTO approval (id, decision_id, level, approver_role, status)"
                             " VALUES (%s, %s, %s, %s, 'требуется')",
                             (uuid.uuid4(), decision_id, approval_level, approval_level))
        return decision_id

    def decision(self, request_id: uuid.UUID) -> dict | None:
        d = self._one("SELECT * FROM decision WHERE request_id = %s", (request_id,))
        if d is None:
            return None
        d["candidates"] = self._all(
            "SELECT candidate_type, external_technician_id, external_service_center_id, rank, rationale,"
            " selection_basis, rejection_reason FROM candidate WHERE decision_id = %s"
            " ORDER BY rank NULLS LAST, candidate_type, COALESCE(external_technician_id, external_service_center_id)",
            (d["id"],))
        d["approval"] = self._one(
            "SELECT level, approver_role, status, is_rule_override, override_reason, approved_by, decided_at"
            " FROM approval WHERE decision_id = %s", (d["id"],))
        return d

    def decide_approval(self, decision_id: uuid.UUID, status: str, user_id: str) -> bool:
        """True — решение записано. False — решение уже было принято раньше:
        повторное согласование ничего не меняет, и исполнение продолжать нельзя."""
        with self.pool.connection() as conn:
            cur = conn.execute("UPDATE approval SET status = %s, approved_by = %s, decided_at = now()"
                               " WHERE decision_id = %s AND status = 'требуется'", (status, user_id, decision_id))
            return cur.rowcount == 1

    # --- разрешение на исполнение -------------------------------------------------------------------

    def execution_grant(self, request_id: uuid.UUID) -> dict | None:
        return self._one("SELECT id, request_id, decision_id, granted_by, basis, approval_required, granted_at"
                         " FROM execution_grant WHERE request_id = %s", (request_id,))

    def grant_execution(self, request_id: uuid.UUID, *, actor_user_id: str, basis: str,
                        allowed: frozenset[State]) -> dict:
        """Разрешение на запись в учётную систему: решение человека, сохранённое
        под блокировкой строки обращения.

        Проверяется всё, на что опирается исполнитель: состояние обращения,
        наличие решения, подтверждение человеком и согласование, если оно
        требуется. Повтор возвращает выданное ранее разрешение — тот же ключ
        идемпотентности, а не второй документ.
        """
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute("SELECT status FROM service_request WHERE id = %s FOR UPDATE",
                               (request_id,)).fetchone()
            if row is None:
                raise KeyError(f"Обращение {request_id} не найдено")
            status = State(row[0])
            if status not in allowed:
                raise IllegalTransition(f"Исполнение разрешается в состояниях"
                                        f" {', '.join(sorted(s.value for s in allowed))}, текущее — «{status.value}»")
            decision = conn.execute("SELECT id FROM decision WHERE request_id = %s", (request_id,)).fetchone()
            if decision is None:
                raise ExecutionNotAuthorized(f"У обращения {request_id} нет решения")
            confirmed = conn.execute(
                "SELECT 1 FROM process_step WHERE request_id = %s AND step_name = 'confirmation'"
                " AND status = 'выполнен' AND (state_snapshot -> 'output' ->> 'accept')::boolean LIMIT 1",
                (request_id,)).fetchone()
            if confirmed is None:
                raise ExecutionNotAuthorized("Нет подтверждения человеком: исполнять нечего")
            approval = conn.execute("SELECT status FROM approval WHERE decision_id = %s", (decision[0],)).fetchone()
            if approval is not None and approval[0] != "получено":
                raise ExecutionNotAuthorized(f"Согласование в состоянии «{approval[0]}»")
            granted = conn.execute(
                "INSERT INTO execution_grant (id, request_id, decision_id, granted_by, basis, approval_required)"
                " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (request_id) DO NOTHING",
                (uuid.uuid4(), request_id, decision[0], actor_user_id, basis, approval is not None))
            if granted.rowcount == 1:
                conn.execute("INSERT INTO audit_event (request_id, actor_user_id, event_type, after_state)"
                             " VALUES (%s, %s, 'execution_granted', %s)",
                             (request_id, actor_user_id, _json({"decision_id": decision[0], "basis": basis})))
            return conn.cursor(row_factory=dict_row).execute(
                "SELECT id, request_id, decision_id, granted_by, basis, approval_required, granted_at"
                " FROM execution_grant WHERE request_id = %s", (request_id,)).fetchone()

    def save_draft(self, decision_id: uuid.UUID, *, external_document_id: str, idempotency_key: str,
                   payload: Any) -> bool:
        """Один черновик на решение: повтор ничего не добавляет. True — запись новая."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                "INSERT INTO work_order_draft (id, decision_id, external_document_id, idempotency_key,"
                " payload_snapshot, status) VALUES (%s, %s, %s, %s, %s, 'создан') ON CONFLICT (decision_id) DO NOTHING",
                (uuid.uuid4(), decision_id, external_document_id, idempotency_key, _json(payload)))
            return cur.rowcount == 1

    def draft(self, decision_id: uuid.UUID) -> dict | None:
        return self._one("SELECT external_document_id, idempotency_key, payload_snapshot, status, created_at"
                         " FROM work_order_draft WHERE decision_id = %s", (decision_id,))
