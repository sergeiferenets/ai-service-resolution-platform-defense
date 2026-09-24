"""Оркестратор обращения — явный конечный автомат (ADR-0002).

Без библиотеки оркестрации агентов: состояния и переходы — states.py,
персистентность — только собственная схема (store.py). Каждый шаг —
строка PROCESS_STEP со ссылкой на версию набора артефактов, каждый вызов
адаптера, модели и инструмента — строка TOOL_CALL этого шага, каждый
переход — AUDIT_EVENT.

Основной путь (sequence-and-states.md, раздел 1):
входной контроль → предохранитель → [идентификация ‖ поиск] → правила →
гарантия → тип исполнения и кандидаты → срочность → согласование →
выходной контроль → рекомендация → подтверждение человеком → черновик.

Все три агента ADR-0002 реализованы (backend/agents/); заглушки остаются для
модульных и изолированных тестов. Policy Agent вызывается только для правил,
размеченных в своде как требующие оценки, и способ выполнения работ не
выбирает. Бюджеты и повторы ADR-0007 — в адаптере и в слое вызова модели;
размыкатель — следующей задачей.

Запись в учётную систему привязана к решению человека: подтверждение и
согласование выдают разрешение на исполнение под блокировкой строки
обращения, отказ при выданном разрешении отклоняется (executor.py).
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from backend.adapters.cache import RequestScopedCache
from backend.adapters.port import ErpPort
from backend.agents.context.agent import ContextResult, ContextTools, RiskSignal
from backend.agents.contracts import Agents, KnowledgeResult, KnowledgeTools, PolicyCase, PolicyTools
from backend.domain.errors import AccessDenied, NotFound, SourceContractError, SourceUnavailable
from backend.domain.models import AuthContext, Contract, DraftResult, Equipment, PartStock, ServiceCenter, Technician
from backend.guardrails.input.check import InputRules, check_input, load_rules, redactor
from backend.llm.client import InferenceError
from backend.llm.structured import Image, StructuredOutputRejected
from backend.orchestrator.artifacts import ArtifactVersion
from backend.orchestrator.executor import DraftExecutor
from backend.orchestrator.recording import RecordingErp, elapsed_ms
from backend.orchestrator.serialize import to_jsonable
from backend.orchestrator.states import IllegalTransition, State
from backend.orchestrator.store import Store
from backend.rules.catalog import Catalog
from backend.rules.engine import Facts, RuleEngine
from backend.rules.urgency import compute_urgency
from backend.tools.approval import DISPATCHER, SERVICE_MANAGER, assess_approval
from backend.tools.error_codes import ErrorCodeDirectory
from backend.tools.fulfillment import REJECTION_REASONS, FulfillmentDecision, decide_fulfillment
from backend.tools.warranty import prequalify
from data.loader.reference import ROUTES

AGENT, TOOL, HUMAN = "агент", "инструмент", "человек"
ROUTE_LABELS = {"remote": "удалённо", "onsite": "выезд", "workshop": "мастерская"}
FULFILLMENT_LABELS = {"internal": "внутреннее", "external": "внешнее"}
WARRANTY_LABELS = {"warranty": "гарантийный", "paid": "платный"}
CLASSIFIER = {route: text for text, route in ROUTES.items()}
CONFIRMING_ROLES = frozenset({DISPATCHER, SERVICE_MANAGER})
NO_FACTS_NOTICE = ("Данные об оборудовании недоступны. Создание заказа недоступно: договор, гарантия "
                   "и остатки не проверены.")


@dataclass(frozen=True)
class StepBudgets:
    """ADR-0007. Бюджеты учётной системы — в адаптере, моделей — в слое вызова модели.

    Бюджет поиска (5 с) принадлежит самому поиску и задан в клиенте хранилища
    векторов. Шаг Knowledge Agent — это поиск и вызов языковой модели подряд,
    поэтому у него собственный бюджет: 5 с поиска, 60 с модели и запас на
    повтор структурированного вывода.
    """
    retrieval_s: float = 5.0
    knowledge_s: float = 130.0


@dataclass(frozen=True)
class Identified:
    context: ContextResult
    equipment: Equipment | None
    contract: Contract | None
    product_price: int | None


def output_problems(*, catalog: Catalog, rule_id: str, route: str, fulfillment: FulfillmentDecision,
                    centers: list[ServiceCenter], technicians: list[Technician],
                    suspected_part_ids: tuple[str, ...], parts: list[PartStock]) -> list[str]:
    """Выходной контроль: каждая упомянутая в рекомендации сущность существует."""
    problems = []
    if rule_id not in catalog.rules:
        problems.append(f"правило {rule_id} отсутствует в своде")
    if route not in ROUTE_LABELS:
        problems.append(f"способ выполнения «{route}» вне классификатора")
    if fulfillment.service_center_id and fulfillment.service_center_id not in {c.center_id for c in centers}:
        problems.append(f"сервисный центр {fulfillment.service_center_id} не найден")
    if fulfillment.technician_id and fulfillment.technician_id not in {t.technician_id for t in technicians}:
        problems.append(f"исполнитель {fulfillment.technician_id} не найден")
    known = {p.part_id for p in parts}
    problems += [f"запчасть {p} не подходит к модели или не существует" for p in suspected_part_ids if p not in known]
    problems += [f"причина отклонения «{c.rejection_reason}» вне перечня" for c in fulfillment.candidates
                 if c.rejection_reason and c.rejection_reason not in REJECTION_REASONS]
    return problems


def equipment_brief(e: Equipment) -> dict:
    return {"equipment_id": e.equipment_id, "serial_number": e.serial_number,
            "model": f"{e.model.manufacturer} {e.model.name}", "location": e.location}


def context_output(result: ContextResult) -> dict:
    ident, eq = result.identification, result.identification.equipment
    return {
        "status": ident.status, "confidence_level": ident.confidence_level, "discrepancy": ident.discrepancy,
        "source": ident.source, "serial_number": ident.serial_number, "reason": ident.reason,
        "equipment_id": eq.equipment_id if eq else None, "model_id": eq.model.model_id if eq else None,
        "manufacturer": eq.model.manufacturer if eq else None, "territory_id": eq.territory_id if eq else None,
        "candidates": [equipment_brief(c) for c in ident.candidates],
        "extraction": to_jsonable(result.extraction), "plate": to_jsonable(result.plate),
        "code_check": to_jsonable(result.code_check), "risk_signals": to_jsonable(result.risk_signals),
    }


def identification_reason(result: ContextResult) -> str:
    ident = result.identification
    reason = f"Идентификация: {ident.reason}"
    if ident.confidence_level:
        reason += f"; уровень «{ident.confidence_level}»" + (", признак расхождения" if ident.discrepancy else "")
    if ident.candidates:
        reason += ". Кандидаты: " + "; ".join(
            f"{c.equipment_id} ({c.serial_number}, {c.model.manufacturer} {c.model.name})" for c in ident.candidates)
    return reason


class _Step:
    def __init__(self, store: Store, request_id: uuid.UUID, name: str, actor: str, artifact_version: str,
                 state: State):
        self.snapshot: dict[str, Any] = {}
        self.id = store.start_step(request_id, name, actor, artifact_version, state)


class Orchestrator:
    def __init__(self, store: Store, erp: ErpPort, catalog: Catalog, agents: Agents, artifacts: ArtifactVersion,
                 *, today: Callable[[], dt.date] = dt.date.today, budgets: StepBudgets = StepBudgets(),
                 input_rules: InputRules | None = None):
        self.store = store
        self.erp = erp
        self.catalog = catalog
        self.agents = agents
        self.input_rules = input_rules or load_rules()
        self.engine = RuleEngine(catalog)
        self.executor = DraftExecutor(store, erp)
        self.today = today
        self.budgets = budgets
        self.artifacts = artifacts
        self.artifact_version = store.ensure_artifact_version(artifacts)

    # --- служебное ----------------------------------------------------------------------------------

    def _to(self, rid: uuid.UUID, ctx: AuthContext, state: State, reason: str | None = None, *,
            require_no_execution_grant: bool = False) -> State:
        self.store.transition(rid, state, actor_user_id=ctx.user_id, reason=reason,
                              require_no_execution_grant=require_no_execution_grant)
        return state

    def _start(self, rid: uuid.UUID, name: str, actor: str, state: State) -> _Step:
        return _Step(self.store, rid, name, actor, self.artifact_version, state)

    def _finish(self, step: _Step, fn: Callable[[], Any]) -> tuple[Any, Exception | None]:
        try:
            result = fn()
        except Exception as exc:
            step.snapshot["error"] = f"{type(exc).__name__}: {exc}"
            self.store.finish_step(step.id, "отказ", step.snapshot)
            return None, exc
        self.store.finish_step(step.id, "выполнен", step.snapshot)
        return result, None

    @contextmanager
    def _step(self, rid: uuid.UUID, name: str, actor: str, state: State) -> Iterator[_Step]:
        step = self._start(rid, name, actor, state)
        try:
            yield step
        except Exception as exc:
            step.snapshot["error"] = f"{type(exc).__name__}: {exc}"
            self.store.finish_step(step.id, "отказ", step.snapshot)
            raise
        self.store.finish_step(step.id, "выполнен", step.snapshot)

    def _tool(self, step: _Step, name: str, request: Any, fn: Callable[[], Any]) -> Any:
        started = time.perf_counter()
        try:
            result = fn()
        except Exception as exc:
            self.store.record_tool_call(step.id, name, request, {"error": f"{type(exc).__name__}: {exc}"},
                                        elapsed_ms(started), "отказ")
            raise
        self.store.record_tool_call(step.id, name, request, result, elapsed_ms(started), "успех")
        return result

    @staticmethod
    def _agent(agent: object) -> dict:
        return {"agent": type(agent).__name__, "stub": bool(getattr(agent, "IS_STUB", False))}

    def _safety_risks(self, keywords: list[str]) -> list[RiskSignal]:
        """Слово предохранителя с пометкой в своде даёт признак риска по гарантии (PRD 5.10)."""
        kinds: dict[str, str] = {}
        for keyword in keywords:
            kind = self.catalog.safety_warranty_risk.get(keyword)
            if kind and kind not in kinds:
                kinds[kind] = keyword
        return [RiskSignal(kind, keyword, "предохранитель") for kind, keyword in kinds.items()]

    # --- разбор -------------------------------------------------------------------------------------

    def run(self, request_id: uuid.UUID, ctx: AuthContext) -> State:
        """Разбор до рекомендации, ожидающей подтверждения, или до конечного состояния."""
        req = self.store.get_request(request_id)
        if req is None:
            raise KeyError(f"Обращение {request_id} не найдено")
        if req["status"] != State.ACCEPTED.value:
            raise IllegalTransition(f"Разбор начинается в состоянии «Принято», текущее — «{req['status']}»")
        rid, text, today = req["id"], req["raw_text"], self.today()
        erp = RequestScopedCache(self.erp)   # кэш чтения на одно обращение (ADR-0006)

        # Входной контроль — до любого вызова модели (security.md, раздел 3.1).
        with self._step(rid, "input_control", TOOL, State.ACCEPTED) as s:
            verdict = self._tool(s, "guard.input", {"text_length": len(text or ""), "rules": self.input_rules.digest},
                                 lambda: check_input(text, self.input_rules))
            s.snapshot["output"] = {"decision": verdict.decision, "category": verdict.category,
                                    "reasons": verdict.reasons, "pii": verdict.pii}
        if verdict.decision == "reject":
            return self._to(rid, ctx, State.REJECTED, verdict.notice)

        safety = self.catalog.safety_rule
        with self._step(rid, "safety_guard", TOOL, State.ACCEPTED) as s:
            hits = self._tool(s, "guard.safety", {"text": "service_request.raw_text"},
                              lambda: self.engine.safety_text_hits(text))
            risks = self._safety_risks(hits)
            s.snapshot["output"] = {"keywords": hits, "rule_id": safety.rule_id if hits else None,
                                    "prescribed_actions": safety.prescribed_actions if hits else None,
                                    "risk_signals": to_jsonable(risks)}
        if hits:
            flag = f"; признак риска по гарантии: {', '.join(r.kind for r in risks)}" if risks else ""
            return self._to(rid, ctx, State.ESCALATED, f"Безопасность ({safety.rule_id}): «{hits[0]}» в тексте{flag}")
        self._to(rid, ctx, State.ANALYSIS)

        # Идентификация и поиск независимы и идут параллельно (ADR-0007, раздел 4).
        image = self.store.attachment(rid)
        partner_id = req["external_partner_id"] or ctx.partner_id
        mask = redactor(text, verdict.pii)
        ident_step = self._start(rid, "identification", AGENT, State.ANALYSIS)
        know_step = self._start(rid, "knowledge", AGENT, State.ANALYSIS)
        pool = ThreadPoolExecutor(max_workers=2)
        f_ident = pool.submit(self._finish, ident_step,
                              lambda: self._identify(rid, ctx, erp, ident_step, text, image, partner_id, mask, today))
        f_know = pool.submit(self._finish, know_step, lambda: self._search(rid, ctx, know_step, text, mask))
        pool.shutdown(wait=False)
        identified, ident_error = f_ident.result()
        try:
            knowledge, know_error = f_know.result(timeout=self.budgets.knowledge_s)
        except FuturesTimeout:
            knowledge, know_error = None, TimeoutError(
                f"поиск и гипотеза не уложились в {self.budgets.knowledge_s:g} с")

        if isinstance(ident_error, AccessDenied):
            return self._to(rid, ctx, State.ESCALATED, f"Доступ запрещён источником фактов: {ident_error.reason}")
        if isinstance(ident_error, (SourceUnavailable, SourceContractError)):
            return self._degraded(rid, ctx, knowledge, ident_error)
        if isinstance(ident_error, StructuredOutputRejected):
            return self._to(rid, ctx, State.ESCALATED, "Context Agent: ответ модели дважды не прошёл проверку; "
                                                       "неразобранный текст дальше не передаётся")
        if isinstance(ident_error, InferenceError):
            return self._to(rid, ctx, State.ESCALATED, f"Сервис инференса: {ident_error}; решение за человеком")
        if isinstance(ident_error, NotFound):
            return self._to(rid, ctx, State.ESCALATED, f"Идентификация: {ident_error}")
        if ident_error is not None:
            raise ident_error
        if identified.equipment is None:
            return self._to(rid, ctx, State.ESCALATED, identification_reason(identified.context))
        if know_error is not None:
            return self._to(rid, ctx, State.ESCALATED, f"Поиск: {know_error}. Ответ без источников не выдаётся")
        if not knowledge.sufficient or not knowledge.sources:
            # ADR-0003: при пустой или недостаточной выдаче гипотеза не строится.
            return self._to(rid, ctx, State.ESCALATED,
                            "Поиск: в найденных фрагментах нет ответа на обращение; "
                            "ответ без источников не выдаётся")

        try:
            return self._decide(rid, ctx, erp, req, identified, knowledge, today, mask)
        except AccessDenied as exc:
            return self._to(rid, ctx, State.ESCALATED, f"Доступ запрещён источником фактов: {exc.reason}")
        except (SourceUnavailable, SourceContractError) as exc:
            return self._degraded(rid, ctx, knowledge, exc)

    def _identify(self, rid: uuid.UUID, ctx: AuthContext, erp: RequestScopedCache, step: _Step, text: str,
                  image: Image | None, partner_id: str | None, mask: Callable[[str], str],
                  today: dt.date) -> Identified:
        source = RecordingErp(erp, self.store, request_id=rid, step_id=step.id)
        tools = ContextTools(erp=source, redact=mask, record=lambda tool, request, response, ms, outcome:
                             self.store.record_tool_call(step.id, tool, request, response, ms, outcome))
        result = self.agents.context.identify(ctx, text, image=image, partner_id=partner_id, tools=tools)
        output = context_output(result)
        step.snapshot.update(self._agent(self.agents.context), output=output)
        ident, eq = result.identification, result.identification.equipment
        if eq is None:
            if ident.confidence_level:
                self.store.save_identification(rid, serial_number=ident.serial_number,
                                               confidence_level=ident.confidence_level,
                                               discrepancy_flag=ident.discrepancy, source=ident.source or "текст")
            return Identified(result, None, None, None)
        partner = source.get_partner(ctx, eq.partner_id)
        contracts = sorted(source.list_contracts(ctx, partner.partner_id, today), key=lambda c: c.contract_id)
        history = source.service_history(ctx, eq.equipment_id)
        price = source.product_price(ctx, eq.model.model_id)
        contract = contracts[0] if contracts else None
        self.store.save_identification(
            rid, serial_number=ident.serial_number or eq.serial_number, confidence_level=ident.confidence_level,
            discrepancy_flag=ident.discrepancy, source=ident.source or "текст", equipment_id=eq.equipment_id,
            partner_id=partner.partner_id, territory_id=eq.territory_id)
        output.update(partner_id=partner.partner_id, contract_id=contract.contract_id if contract else None,
                      service_history_entries=len(history))
        return Identified(result, eq, contract, price)

    def _search(self, rid: uuid.UUID, ctx: AuthContext, step: _Step, text: str,
                mask: Callable[[str], str]) -> KnowledgeResult:
        tools = KnowledgeTools(
            record=lambda tool, request, response, ms, outcome:
                self.store.record_tool_call(step.id, tool, request, response, ms, outcome),
            # Аудит прав поиска: какие уровни доступа были разрешены и что выдано.
            audit=lambda event, payload: self.store.audit(rid, ctx.user_id, event, after=payload),
            redact=mask)
        result = self.agents.knowledge.search(ctx, text, tools=tools)
        step.snapshot.update(self._agent(self.agents.knowledge), output=to_jsonable(result))
        return result

    def _degraded(self, rid: uuid.UUID, ctx: AuthContext, knowledge: KnowledgeResult | None,
                  error: Exception) -> State:
        """ADR-0007: при отказе источника фактов — только документация, запись заблокирована."""
        self._to(rid, ctx, State.DEGRADED, f"Источник фактов недоступен: {error}")
        with self._step(rid, "recommendation_without_facts", TOOL, State.DEGRADED) as s:
            s.snapshot["output"] = {"hypothesis": knowledge.hypothesis if knowledge else None,
                                    "sources": list(knowledge.sources) if knowledge else [],
                                    "notice": NO_FACTS_NOTICE, "draft_blocked": True}
        self._to(rid, ctx, State.RECOMMENDATION_NO_FACTS, NO_FACTS_NOTICE)
        return self._to(rid, ctx, State.ESCALATED, "Требуется решение человека: рекомендация без фактов")

    def _clarification_needed(self, rid: uuid.UUID, ctx: AuthContext, basis: str) -> State:
        """Точка приостановки ADR-0008 предусмотрена моделью — состояние
        ОжиданиеУточнения и его переходы есть в states.py, снимки шагов хранят
        всё для возобновления. Сама приостановка — следующей задачей. До неё
        обращение передаётся человеку: неоднозначность не угадывается."""
        return self._to(rid, ctx, State.ESCALATED,
                        f"Требуется диагностическое уточнение ({basis}); приостановка в срезе не реализована")

    def _decide(self, rid: uuid.UUID, ctx: AuthContext, erp: RequestScopedCache, req: dict, idf: Identified,
                knowledge: KnowledgeResult, today: dt.date, mask: Callable[[str], str] = lambda s: s) -> State:
        result, eq, contract = idf.context, idf.equipment, idf.contract
        extraction, ident = result.extraction, result.identification
        # Меток симптомов нет: контракт Knowledge Agent ограничен гипотезой и
        # ссылками на источники, поэтому правила по общему симптому не
        # срабатывают (conformance.md, раздел 7).
        facts = Facts(eq.model.manufacturer, extraction.error_code, frozenset(), req["raw_text"],
                      model_id=eq.model.model_id)

        with self._step(rid, "rules", TOOL, State.ANALYSIS) as s:
            outcome = self._tool(s, "rules.evaluate",
                                 {"manufacturer": facts.manufacturer, "model_id": facts.model_id,
                                  "error_code": facts.error_code,
                                  "symptoms": facts.symptoms, "rules_version": self.catalog.version},
                                 lambda: self.engine.evaluate(facts))
            rule = outcome.rule
            s.snapshot["output"] = {
                "kind": outcome.kind, "level": outcome.level, "rule_id": rule.rule_id if rule else None,
                "category": rule.category if rule else None, "route": rule.route if rule else None,
                "reason": outcome.reason, "considered": to_jsonable(outcome.considered),
                "prescribed_actions": outcome.prescribed_actions}
        if outcome.kind in ("safety_escalation", "human"):
            return self._to(rid, ctx, State.ESCALATED, outcome.reason)
        if outcome.kind == "clarification":
            return self._clarification_needed(rid, ctx, outcome.reason)
        route = rule.route
        if outcome.kind == "needs_evaluation":
            # Policy Agent отвечает только на вопрос применимости правила.
            # Способ выполнения работ остаётся из свода: агент его не выбирает.
            with self._step(rid, "policy", AGENT, State.ANALYSIS) as s:
                tools = PolicyTools(record=lambda tool, request, response, ms, outcome_:
                                    self.store.record_tool_call(s.id, tool, request, response, ms, outcome_),
                                    redact=mask)
                # Вход собирается явно: механизм правил работает с Facts, где
                # лежит текст обращения, — агенту он не передаётся.
                case = PolicyCase(rule_id=rule.rule_id, condition=rule.condition_text,
                                  manufacturer=facts.manufacturer, error_code=facts.error_code,
                                  model_designation=f"{eq.model.manufacturer} {eq.model.name}",
                                  symptom_text=extraction.symptom_text)
                verdict = self.agents.policy.evaluate(ctx, case, tools=tools)
                s.snapshot["input"] = to_jsonable(case)
                s.snapshot.update(self._agent(self.agents.policy), output=to_jsonable(verdict))
            if verdict.result == "not_applicable":
                return self._to(rid, ctx, State.ESCALATED,
                                f"{rule.rule_id} к обращению не применимо: {verdict.reason}")
            if verdict.result != "applicable":
                return self._clarification_needed(rid, ctx, f"{rule.rule_id} требует оценки: {verdict.reason}")
        if route not in ROUTE_LABELS:
            return self._to(rid, ctx, State.ESCALATED, f"{rule.rule_id}: решение — {CLASSIFIER.get(route, route)}")

        risk_kinds = frozenset(r.kind for r in result.risk_signals)
        with self._step(rid, "warranty", TOOL, State.ANALYSIS) as s:
            warranty = self._tool(
                s, "warranty.prequalify",
                {"equipment_id": eq.equipment_id, "equipment_type": eq.model.equipment_type, "sale_date": eq.sale_date,
                 "today": today, "risk_signals": risk_kinds},
                lambda: prequalify(eq, today, self.catalog.warranty_months_by_type, risk_kinds))
            s.snapshot["output"] = to_jsonable(warranty)
        is_warranty = warranty.status == "warranty"

        with self._step(rid, "fulfillment", TOOL, State.ANALYSIS) as s:
            source = RecordingErp(erp, self.store, request_id=rid, step_id=s.id)
            centers = source.list_service_centers(ctx, eq.territory_id)
            technicians = source.list_technicians(ctx, eq.territory_id)
            parts = source.list_parts(ctx, eq.model.model_id)
            # Подозреваемая запчасть — из справочника кодов ошибок, а не из ответа
            # модели: так она проверяема и не может быть выдумана.
            typical_part = result.code_check.typical_part if result.code_check else None
            suspected = ErrorCodeDirectory.parts_for(typical_part, parts)
            args = dict(route=route, warranty=is_warranty, brand=eq.model.manufacturer, territory_id=eq.territory_id,
                        required_skill=rule.required_skill, window_start=today,
                        part_available=any(p.stock > 0 for p in suspected) if suspected else None,
                        contract_technician_id=contract.assigned_technician_id if contract else None,
                        card_technician_id=eq.assigned_technician_id)
            fulfillment = self._tool(
                s, "fulfillment.decide",
                {**args, "centers": [c.center_id for c in centers], "technicians": [t.technician_id for t in technicians]},
                lambda: decide_fulfillment(centers=centers, technicians=technicians, **args))
            s.snapshot["output"] = to_jsonable(fulfillment)
        if fulfillment.fulfillment_type is None:
            return self._to(rid, ctx, State.ESCALATED, fulfillment.basis)

        with self._step(rid, "urgency", TOOL, State.ANALYSIS) as s:
            reaction = contract.reaction_hours if contract else None
            urgency = self._tool(s, "urgency.compute",
                                 {"urgency_hint": rule.urgency_hint, "impact": req["impact"],
                                  "contract_reaction_hours": reaction},
                                 lambda: compute_urgency(rule.urgency_hint, req["impact"], reaction))
            s.snapshot["output"] = to_jsonable(urgency)

        # Смета в срезе — цена подозреваемой запчасти; трудозатраты появятся с агентами.
        part = min(suspected, key=lambda p: p.price_rub) if suspected else None
        with self._step(rid, "approval", TOOL, State.ANALYSIS) as s:
            approval_args = dict(warranty=is_warranty, estimated_cost=part.price_rub if part else None,
                                 part_price=part.price_rub if part else None,
                                 part_lead_time_days=part.lead_time_days if part and part.stock == 0 else None,
                                 product_price=idf.product_price)
            approval = self._tool(s, "approval.assess", approval_args, lambda: assess_approval(**approval_args))
            s.snapshot["output"] = {**to_jsonable(approval),
                                    "estimate_basis": f"цена запчасти {part.part_id}" if part else "смета не определена"}

        with self._step(rid, "output_control", TOOL, State.ANALYSIS) as s:
            source = RecordingErp(erp, self.store, request_id=rid, step_id=s.id)
            known_parts = source.list_parts(ctx, eq.model.model_id)   # «существуют ли артикулы» — из кэша обращения
            suspected_ids = tuple(p.part_id for p in suspected)
            problems = self._tool(
                s, "guard.output", {"rule_id": rule.rule_id, "route": route, "parts": suspected_ids},
                lambda: output_problems(catalog=self.catalog, rule_id=rule.rule_id, route=route,
                                        fulfillment=fulfillment, centers=centers, technicians=technicians,
                                        suspected_part_ids=suspected_ids, parts=known_parts))
            s.snapshot["output"] = {"problems": problems}
        if problems:
            return self._to(rid, ctx, State.ESCALATED, "Выходной контроль: " + "; ".join(problems))

        self.store.save_diagnosis(rid, knowledge.hypothesis, knowledge.evidence_level, [rule.rule_id],
                                  [s.chunk_id for s in knowledge.sources])
        decision_id = self.store.save_decision(
            rid, execution_mode=ROUTE_LABELS[route], fulfillment_type=FULFILLMENT_LABELS[fulfillment.fulfillment_type],
            classifier_value=CLASSIFIER[route], warranty_preliminary=WARRANTY_LABELS[warranty.status],
            warranty_is_preliminary=warranty.preliminary, applied_rule_id=rule.rule_id,
            considered_rules=outcome.considered, service_center_id=fulfillment.service_center_id,
            candidates=fulfillment.candidates, approval_level=approval.level)
        self._to(rid, ctx, State.RECOMMENDATION_READY)

        with self._step(rid, "recommendation", TOOL, State.RECOMMENDATION_READY) as s:
            s.snapshot["output"] = {
                "decision_id": decision_id, "rule_id": rule.rule_id, "rule_level": outcome.level,
                "rule_category": rule.category, "execution_mode": route, "classifier_value": CLASSIFIER[route],
                "prescribed_actions": rule.prescribed_actions,   # дословно (ADR-0004, раздел 5)
                "identification": {"confidence_level": ident.confidence_level, "discrepancy": ident.discrepancy,
                                   "source": ident.source, "reason": ident.reason,
                                   # PRD 5.3: при уровне «со слов» рекомендация предварительная.
                                   "preliminary": ident.confidence_level == "со слов"},
                "error_code": {"value": extraction.error_code, "check": result.code_check},
                "symptom_text": extraction.symptom_text,
                "risk_signals": result.risk_signals,
                "hypothesis": knowledge.hypothesis, "evidence_level": knowledge.evidence_level,
                # Ссылки проверяемые: документ, раздел и уровень доступа фрагмента.
                "sources": [{"reference": s.reference, "document_id": s.document_id, "title": s.title,
                             "section_title": s.section_title, "confidentiality_level": s.confidentiality_level}
                            for s in knowledge.sources],
                "warranty": {"status": warranty.status, "preliminary": warranty.preliminary, "basis": warranty.basis,
                             "valid_until": warranty.valid_until, "risk_flags": warranty.risk_flags},
                "fulfillment_type": fulfillment.fulfillment_type, "service_center_id": fulfillment.service_center_id,
                "technician_id": fulfillment.technician_id, "fulfillment_basis": fulfillment.basis,
                "urgency": urgency, "approval": approval,
                "agents": [self._agent(a) for a in (self.agents.context, self.agents.knowledge, self.agents.policy)]}
            # Снимок фактов на момент решения — единственная копия данных учётной системы;
            # из него собирается черновик, поэтому повтор создания даёт тот же запрос.
            s.snapshot["facts"] = {
                "decision_id": decision_id, "serial_number": eq.serial_number, "equipment_id": eq.equipment_id,
                "model_id": eq.model.model_id, "manufacturer": eq.model.manufacturer, "partner_id": eq.partner_id,
                "territory_id": eq.territory_id, "sale_date": eq.sale_date, "error_code": extraction.error_code,
                "confidence_level": ident.confidence_level,
                "contract_id": contract.contract_id if contract else None,
                "warranty_valid_until": warranty.valid_until, "product_price_rub": idf.product_price,
                "parts": [{"part_id": p.part_id, "stock": p.stock, "price_rub": p.price_rub,
                           "lead_time_days": p.lead_time_days} for p in suspected],
                "rule_id": rule.rule_id, "rules_version": self.artifacts.rules_version,
                "artifact_version": self.artifact_version}
        return self._to(rid, ctx, State.AWAITING_CONFIRMATION)

    # --- решения человека и исполнение -----------------------------------------------------------------

    def _authorized(self, request_id: uuid.UUID, ctx: AuthContext, roles: frozenset[str], action: str) -> dict:
        req = self.store.get_request(request_id)
        if req is None:
            raise KeyError(f"Обращение {request_id} не найдено")
        if ctx.role not in roles or ctx.partner_id is not None or req["territory_id"] not in ctx.territories:
            self.store.audit(req["id"], ctx.user_id, "access_denied",
                             after={"action": action, "role": ctx.role, "request_territory": req["territory_id"],
                                    "user_territories": sorted(ctx.territories)})
            raise AccessDenied(f"{action}: у роли «{ctx.role}» нет прав на территорию обращения",
                               "service_request", str(req["id"]))
        return req

    def confirm(self, request_id: uuid.UUID, ctx: AuthContext, accept: bool) -> State:
        req = self._authorized(request_id, ctx, CONFIRMING_ROLES, "подтверждение рекомендации")
        rid, status = req["id"], State(req["status"])
        if status is State.DRAFT_CREATED and accept:
            return status   # повторное подтверждение: документ уже создан, запись не повторяется
        if status is not State.AWAITING_CONFIRMATION:
            raise IllegalTransition(f"Подтверждение возможно в состоянии «{State.AWAITING_CONFIRMATION.value}»,"
                                    f" текущее — «{status.value}»")
        with self._step(rid, "confirmation", HUMAN, status) as s:
            s.snapshot["output"] = {"user_id": ctx.user_id, "role": ctx.role, "accept": accept}
        if not accept:
            # Отказ невозможен, если исполнение уже разрешено: обе операции
            # борются за строку обращения, происходит ровно одна.
            return self._to(rid, ctx, State.REJECTED, f"Рекомендация отклонена пользователем {ctx.user_id}",
                            require_no_execution_grant=True)
        decision = self.store.decision(rid)
        approval = decision["approval"]
        if approval and approval["level"] == SERVICE_MANAGER:
            return self._to(rid, ctx, State.AWAITING_APPROVAL, "Сумма выше порога: нужно согласование сервис-менеджера")
        if approval and not self.store.decide_approval(decision["id"], "получено", ctx.user_id):
            raise IllegalTransition(f"Согласование решения {decision['id']} уже завершено: исполнение не выполняется")
        self.store.grant_execution(rid, actor_user_id=ctx.user_id, basis="подтверждение",
                                   allowed=frozenset({State.AWAITING_CONFIRMATION}))
        self.execute_draft(rid, ctx)
        return self._to(rid, ctx, State.DRAFT_CREATED)

    def approve(self, request_id: uuid.UUID, ctx: AuthContext, accept: bool) -> State:
        req = self._authorized(request_id, ctx, frozenset({SERVICE_MANAGER}), "согласование")
        rid, status = req["id"], State(req["status"])
        if status is not State.AWAITING_APPROVAL:
            raise IllegalTransition(f"Согласование возможно в состоянии «{State.AWAITING_APPROVAL.value}»,"
                                    f" текущее — «{status.value}»")
        with self._step(rid, "approval_decision", HUMAN, status) as s:
            s.snapshot["output"] = {"user_id": ctx.user_id, "role": ctx.role, "accept": accept}
        decision = self.store.decision(rid)
        # Решение по согласованию записывается один раз: если оно уже принято,
        # обновления не происходит и исполнение не продолжается.
        if not self.store.decide_approval(decision["id"], "получено" if accept else "отклонено", ctx.user_id):
            raise IllegalTransition(f"Решение по согласованию {decision['id']} уже принято")
        if not accept:
            return self._to(rid, ctx, State.REJECTED, f"В согласовании отказано: {ctx.user_id}",
                            require_no_execution_grant=True)
        self.store.grant_execution(rid, actor_user_id=ctx.user_id, basis="согласование",
                                   allowed=frozenset({State.AWAITING_APPROVAL}))
        self.execute_draft(rid, ctx)
        return self._to(rid, ctx, State.DRAFT_CREATED)

    def execute_draft(self, request_id: uuid.UUID, ctx: AuthContext) -> DraftResult:
        """Исполнитель: единственная запись в учётную систему, ключ — идентификатор
        решения. Без сохранённого разрешения на исполнение запись не выполняется
        даже при прямом вызове: проверку делает сам исполнитель."""
        state = State(self.store.get_request(request_id)["status"])
        with self._step(request_id, "create_draft", TOOL, state) as s:
            result = self.executor.create(request_id, ctx, step_id=s.id)
            s.snapshot["output"] = to_jsonable(result)
        return result
