"""API платформы: приём обращения, состояние, шаги, подтверждение, согласование.

Пользователь определяется заголовком X-User-Id — временная замена единого
входа. Права берутся не из заголовка, а из собственного реестра платформы
(APP_USER, USER_TERRITORY); неизвестный пользователь получает 401.

Фотография идентификационной таблички передаётся в теле запроса (base64),
JPEG или PNG до 5 МБ. Партнёр обращения — из контекста авторизации
представителя партнёра; сотрудник, регистрирующий обращение за партнёра,
указывает его явно.

Разбор выполняется синхронно в запросе; длительность вызовов ограничена
бюджетами ADR-0007 в адаптере и в слое вызова модели.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from backend.adapters.sap_odata import SapODataAdapter
from backend.agents.factory import model_agents
from backend.api.visibility import can_read
from backend.config import load_settings
from backend.domain.access import is_staff, visible_levels
from backend.domain.errors import AccessDenied, ExecutionNotAuthorized, IdempotencyConflict
from backend.domain.models import AuthContext
from backend.guardrails.input.check import load_rules
from backend.knowledge.loader import corpus_version
from backend.llm.structured import Image
from backend.orchestrator.artifacts import build_artifacts, composite_rules_version
from backend.orchestrator.db import make_pool
from backend.orchestrator.machine import Orchestrator
from backend.orchestrator.serialize import to_jsonable
from backend.orchestrator.states import IllegalTransition
from backend.orchestrator.store import Store
from backend.rules.catalog import load_catalog
from backend.tools.error_codes import ErrorCodeDirectory

MAX_IMAGE_BYTES = 5 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    pool = make_pool(settings)
    catalog = load_catalog()
    input_rules = load_rules()
    codes = ErrorCodeDirectory.load(catalog.code_prefixes_ignored)
    agent_set = model_agents(settings, codes, input_rules, catalog)
    artifacts = build_artifacts(
        code_commit=settings.code_commit,
        rules_version=composite_rules_version(catalog.version, codes.digest, input_rules.digest),
        llm_model_revision=settings.llm_model_revision, vision_model_revision=settings.vision_model_revision,
        embedding_model_revision=settings.embed_model_revision, corpus_version=corpus_version(),
        chunker_version=agent_set.chunker_version, retrieval_config_version=agent_set.retrieval_config_version,
        prompt_versions=agent_set.prompt_versions)
    store = Store(pool)
    app.state.store = store
    app.state.orchestrator = Orchestrator(store, SapODataAdapter(settings.erp_base_url, settings.mock_erp_token),
                                          catalog, agent_set.agents, artifacts, input_rules=input_rules)
    yield
    pool.close()


app = FastAPI(title="Платформа сервисных обращений", lifespan=lifespan)


class NewRequest(BaseModel):
    text: str = Field(description="Текст обращения как получен")
    channel: str = "интерфейс"
    impact: Literal["высокое", "среднее", "низкое"] | None = None
    partner_id: str | None = Field(default=None, description="Партнёр, если обращение регистрирует сотрудник")
    image_base64: str | None = Field(default=None, description="Фотография таблички, base64")
    image_content_type: Literal["image/jpeg", "image/png"] | None = None


class HumanDecision(BaseModel):
    accept: bool = True


def current_user(request: Request, x_user_id: str | None = Header(default=None)) -> AuthContext:
    store: Store = request.app.state.store
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Нет пользователя: заголовок X-User-Id обязателен")
    ctx = store.auth_context(x_user_id)
    if ctx is None:
        store.audit(None, None, "access_denied", after={"claimed_user_id": x_user_id, "reason": "пользователь неизвестен"})
        raise HTTPException(status_code=401, detail="Пользователь неизвестен платформе")
    return ctx


def decode_image(body: NewRequest) -> Image | None:
    if body.image_base64 is None:
        return None
    if body.image_content_type is None:
        raise HTTPException(status_code=422, detail="Для изображения нужен image_content_type: image/jpeg или image/png")
    try:
        data = base64.b64decode(body.image_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="Изображение передано не в base64")
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Изображение пустое или больше 5 МБ")
    return Image(body.image_content_type, data)


def visible_request(request: Request, request_id: uuid.UUID, ctx: AuthContext) -> dict:
    """Право на чтение проверяется по текущим правам: отзыв территории
    закрывает обращение и тому, кто его создал."""
    store: Store = request.app.state.store
    req = store.get_request(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Обращение не найдено")
    if not can_read(req, ctx):
        store.audit(req["id"], ctx.user_id, "access_denied",
                    after={"action": "просмотр обращения", "request_territory": req["territory_id"],
                           "user_territories": sorted(ctx.territories)})
        raise HTTPException(status_code=403, detail={"error": "access_denied", "reason": "обращение вне зоны ответственности"})
    return req


def _output(store: Store, request_id: uuid.UUID, step: str) -> dict:
    return ((store.step_output(request_id, step) or {}).get("output")) or {}


def view(store: Store, request_id: uuid.UUID, ctx: AuthContext) -> dict:
    """Ответ собирается в границах прав читателя: уровни доступа уходят в
    запрос к хранилищу, закрытый материал сюда не попадает."""
    req = store.get_request(request_id)
    decision = store.decision(request_id)
    recommendation = store.recommendation_snapshot(request_id, visible_levels(ctx))
    context = (store.context_snapshot(request_id, technical=is_staff(ctx)) or {}).get("output") or {}
    return to_jsonable({
        "request_id": req["id"], "status": req["status"], "status_reason": req["status_reason"],
        "territory_id": req["territory_id"], "equipment_id": req["external_equipment_id"],
        "partner_id": req["external_partner_id"], "identification": store.identification(request_id),
        "context": context or None,
        # Признак риска по гарантии — с обоих путей: предохранитель и Context Agent (PRD 5.10).
        "risk_signals": _output(store, request_id, "safety_guard").get("risk_signals", [])
                        + context.get("risk_signals", []),
        "recommendation": recommendation["output"] if recommendation else None,
        "decision": decision, "draft": store.draft(decision["id"]) if decision else None,
    })


def human_decision(request: Request, request_id: uuid.UUID, ctx: AuthContext, action) -> dict:
    try:
        action(request_id, ctx)
    except KeyError:
        raise HTTPException(status_code=404, detail="Обращение не найдено")
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail={"error": "access_denied", "reason": exc.reason})
    except (ExecutionNotAuthorized, IllegalTransition, IdempotencyConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return view(request.app.state.store, request_id, ctx)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/requests", status_code=201)
def create_request(body: NewRequest, request: Request, ctx: AuthContext = Depends(current_user)) -> dict:
    store: Store = request.app.state.store
    image = decode_image(body)
    if ctx.partner_id and body.partner_id and body.partner_id != ctx.partner_id:
        store.audit(None, ctx.user_id, "access_denied",
                    after={"action": "обращение за другого партнёра", "claimed_partner": body.partner_id})
        raise HTTPException(status_code=403, detail={"error": "access_denied", "reason": "обращение за другого партнёра"})
    rid = store.create_request(raw_text=body.text, channel=body.channel, impact=body.impact, created_by=ctx.user_id,
                               partner_id=ctx.partner_id or body.partner_id)
    if image is not None:
        store.save_attachment(rid, image)
    request.app.state.orchestrator.run(rid, ctx)
    return view(store, rid, ctx)


@app.get("/v1/requests/{request_id}")
def get_request(request_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(current_user)) -> dict:
    visible_request(request, request_id, ctx)
    return view(request.app.state.store, request_id, ctx)


@app.get("/v1/requests/{request_id}/steps")
def get_steps(request_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(current_user)) -> dict:
    """Шаги — техническая трассировка: снимки шагов и вызовы инструментов
    содержат запросы к модели вместе с найденными фрагментами, в том числе
    внутренними. Доступны только служебным ролям (PRD, раздел 3.3)."""
    store: Store = request.app.state.store
    visible_request(request, request_id, ctx)
    if not is_staff(ctx):
        store.audit(request_id, ctx.user_id, "access_denied",
                    after={"action": "просмотр шагов обращения", "role": ctx.role, "partner_id": ctx.partner_id})
        raise HTTPException(status_code=403,
                            detail={"error": "access_denied", "reason": "трассировка доступна сотрудникам"})
    return to_jsonable({"request_id": request_id, "steps": store.steps(request_id)})


@app.post("/v1/requests/{request_id}/confirm")
def confirm(request_id: uuid.UUID, body: HumanDecision, request: Request,
            ctx: AuthContext = Depends(current_user)) -> dict:
    orchestrator: Orchestrator = request.app.state.orchestrator
    return human_decision(request, request_id, ctx, lambda rid, c: orchestrator.confirm(rid, c, body.accept))


@app.post("/v1/requests/{request_id}/approve")
def approve(request_id: uuid.UUID, body: HumanDecision, request: Request,
            ctx: AuthContext = Depends(current_user)) -> dict:
    orchestrator: Orchestrator = request.app.state.orchestrator
    return human_decision(request, request_id, ctx, lambda rid, c: orchestrator.approve(rid, c, body.accept))
