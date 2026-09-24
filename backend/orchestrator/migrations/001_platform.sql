-- Схема платформы: состояние процесса, доступ, аудит (docs/architecture/data-model.md).
-- Мастер-данных учётной системы здесь нет: оборудование, партнёры, центры и
-- исполнители хранятся внешними идентификаторами (text) без атрибутов.
-- Единственная копия фактов — неизменяемый снимок в черновике документа.

CREATE TABLE territory (
    territory_id text PRIMARY KEY,
    name         text NOT NULL
);

-- APP_USER. user_id — внешний идентификатор пользователя; partner_id —
-- PARTNER_BINDING: представитель партнёра ограничен своим партнёром.
CREATE TABLE app_user (
    user_id      text PRIMARY KEY,
    display_name text NOT NULL,
    role         text NOT NULL,
    partner_id   text,
    is_active    boolean NOT NULL DEFAULT true
);

-- Территории пользователя — отношение, а не строка с разделителем.
CREATE TABLE user_territory (
    user_id      text NOT NULL REFERENCES app_user,
    territory_id text NOT NULL REFERENCES territory,
    PRIMARY KEY (user_id, territory_id)
);

CREATE TABLE rule_class (
    id   text PRIMARY KEY,
    name text NOT NULL
);
INSERT INTO rule_class (id, name) VALUES
    ('safety', 'безопасность'),
    ('deterministic', 'детерминированное'),
    ('requires_evaluation', 'требует оценки');

CREATE TABLE rule (
    id                 text PRIMARY KEY,
    condition_text     text NOT NULL,
    execution_mode     text NOT NULL,
    specificity        text CHECK (specificity IN ('модель и код', 'общий симптом')),
    urgency_hint       int  NOT NULL,          -- операционная срочность, не порядок применения
    rule_class         text NOT NULL REFERENCES rule_class,
    prescribed_actions text NOT NULL,          -- передаётся дословно
    equipment_class    text,
    source_url         text,
    rules_version      text NOT NULL
);

CREATE TABLE artifact_version (
    id                       text PRIMARY KEY,
    code_commit              text NOT NULL,
    llm_model_revision       text NOT NULL,
    vision_model_revision    text NOT NULL,
    embedding_model_revision text NOT NULL,
    prompt_version           text NOT NULL,
    rules_version            text NOT NULL,
    corpus_version           text NOT NULL,
    chunker_version          text NOT NULL,
    retrieval_config_version text NOT NULL,
    created_at               timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE service_request (
    id                    uuid PRIMARY KEY,
    external_equipment_id text,
    external_partner_id   text,
    raw_text              text NOT NULL,       -- текст обращения как получен
    channel               text NOT NULL,
    impact                text CHECK (impact IN ('высокое', 'среднее', 'низкое')),
    status                text NOT NULL,
    status_reason         text,
    territory_id          text REFERENCES territory,   -- территория оборудования, после идентификации
    created_by            text NOT NULL REFERENCES app_user,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE identification (
    id               uuid PRIMARY KEY,
    request_id       uuid NOT NULL UNIQUE REFERENCES service_request,
    serial_number    text,
    confidence_level text NOT NULL CHECK (confidence_level IN ('подтверждён', 'распознан', 'со слов')),
    discrepancy_flag boolean NOT NULL DEFAULT false,
    source           text NOT NULL CHECK (source IN ('текст', 'изображение', 'ручной ввод'))
);

-- seq — порядок шагов внутри обращения; по нему последовательность
-- восстанавливается однозначно, включая параллельные шаги.
CREATE TABLE process_step (
    id               uuid PRIMARY KEY,
    request_id       uuid NOT NULL REFERENCES service_request,
    seq              int  NOT NULL,
    step_name        text NOT NULL,
    actor            text NOT NULL CHECK (actor IN ('агент', 'инструмент', 'человек')),
    status           text NOT NULL CHECK (status IN ('выполняется', 'выполнен', 'отказ')),
    state_snapshot   jsonb NOT NULL DEFAULT '{}'::jsonb,
    artifact_version text NOT NULL REFERENCES artifact_version,
    started_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at      timestamptz,
    UNIQUE (request_id, seq)
);

CREATE TABLE tool_call (
    id               uuid PRIMARY KEY,
    step_id          uuid NOT NULL REFERENCES process_step,
    tool_name        text NOT NULL,
    request_payload  jsonb NOT NULL,
    response_payload jsonb,
    duration_ms      int  NOT NULL,
    outcome          text NOT NULL CHECK (outcome IN ('успех', 'отказ', 'деградация')),
    called_at        timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX tool_call_step ON tool_call (step_id, called_at);

CREATE TABLE diagnosis (
    id             uuid PRIMARY KEY,
    request_id     uuid NOT NULL REFERENCES service_request,
    hypothesis     text NOT NULL,
    evidence_level text NOT NULL CHECK (evidence_level IN ('код ошибки', 'раздел документации', 'аналогия')),
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- chunk_id станет ссылкой на DOCUMENT_CHUNK вместе с корпусом знаний.
CREATE TABLE evidence (
    id           uuid PRIMARY KEY,
    diagnosis_id uuid NOT NULL REFERENCES diagnosis,
    source_type  text NOT NULL CHECK (source_type IN ('документ', 'правило')),
    chunk_id     uuid,
    rule_id      text REFERENCES rule,
    CHECK (source_type <> 'правило' OR rule_id IS NOT NULL),
    CHECK (source_type <> 'документ' OR chunk_id IS NOT NULL)
);

CREATE TABLE decision (
    id                      uuid PRIMARY KEY,
    request_id              uuid NOT NULL UNIQUE REFERENCES service_request,
    execution_mode          text NOT NULL CHECK (execution_mode IN ('удалённо', 'выезд', 'мастерская')),
    fulfillment_type        text CHECK (fulfillment_type IN ('внутреннее', 'внешнее')),
    classifier_value        text NOT NULL,
    warranty_preliminary    text NOT NULL CHECK (warranty_preliminary IN ('гарантийный', 'платный')),
    warranty_is_preliminary boolean NOT NULL,
    applied_rule_id         text NOT NULL REFERENCES rule,
    considered_rules        jsonb NOT NULL,
    service_center_id       text,              -- внешний идентификатор центра
    created_at              timestamptz NOT NULL DEFAULT now()
);

-- CANDIDATE хранит и выбранных, и отклонённых. Идентификатор — в одном из двух
-- полей по candidate_type; заполнено ровно одно, и это проверяет схема
-- (data-model.md, раздел 3).
CREATE TABLE candidate (
    id                         uuid PRIMARY KEY,
    decision_id                uuid NOT NULL REFERENCES decision,
    candidate_type             text NOT NULL CHECK (candidate_type IN ('исполнитель', 'сервисный центр')),
    external_technician_id     text,
    external_service_center_id text,
    rank                       int,
    rationale                  text NOT NULL,
    selection_basis            text CHECK (selection_basis IN ('договор', 'карточка ИО', 'ранжирование')),
    rejection_reason           text CHECK (rejection_reason IN ('нет авторизации по бренду', 'нет компетенции',
                                   'другая территория', 'недоступен в окно', 'нет запчасти')),
    CHECK ((candidate_type = 'исполнитель') = (external_technician_id IS NOT NULL)),
    CHECK ((candidate_type = 'сервисный центр') = (external_service_center_id IS NOT NULL)),
    CHECK (rank IS NULL OR rejection_reason IS NULL)
);
CREATE INDEX candidate_decision ON candidate (decision_id);

CREATE TABLE approval (
    id               uuid PRIMARY KEY,
    decision_id      uuid NOT NULL UNIQUE REFERENCES decision,
    level            text NOT NULL,
    approver_role    text NOT NULL,
    status           text NOT NULL CHECK (status IN ('требуется', 'получено', 'отклонено')),
    is_rule_override boolean NOT NULL DEFAULT false,
    override_reason  text,
    approved_by      text REFERENCES app_user,
    decided_at       timestamptz
);

-- WORK_ORDER_DRAFT и IDEMPOTENCY_KEY: ключ — идентификатор решения,
-- один черновик на решение.
CREATE TABLE work_order_draft (
    id                   uuid PRIMARY KEY,
    decision_id          uuid NOT NULL UNIQUE REFERENCES decision,
    external_document_id text NOT NULL,
    idempotency_key      text NOT NULL UNIQUE,
    payload_snapshot     jsonb NOT NULL,       -- факты на момент создания, неизменяемы
    status               text NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_event (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id    uuid REFERENCES service_request,
    actor_user_id text REFERENCES app_user,
    event_type    text NOT NULL,
    before_state  jsonb,
    after_state   jsonb,
    occurred_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX audit_event_request ON audit_event (request_id, occurred_at);
