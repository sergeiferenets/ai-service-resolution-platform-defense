-- Схема мока учётной системы. Понятия нейтральные: оборудование, партнёр,
-- договор, сервисный центр, исполнитель, запчасть, черновик документа.
-- Соответствие полям конкретной учётной системы существует только внутри
-- её адаптера (ADR-0006).

CREATE TABLE territory (
    territory_id text PRIMARY KEY,
    name         text NOT NULL
);

CREATE TABLE partner (
    partner_id     text PRIMARY KEY,
    name           text NOT NULL,
    kind           text NOT NULL,
    territory_id   text NOT NULL REFERENCES territory,
    address        text,
    contact_person text,
    phone          text
);

CREATE TABLE skill (
    skill_id text PRIMARY KEY,
    name     text NOT NULL,
    level    integer NOT NULL
);

CREATE TABLE model (
    model_id       text PRIMARY KEY,
    manufacturer   text NOT NULL,
    name           text NOT NULL,
    equipment_type text NOT NULL,
    price_rub      integer NOT NULL
);

CREATE TABLE service_organization (
    service_org_id text PRIMARY KEY,
    name           text NOT NULL,
    territory_id   text NOT NULL REFERENCES territory,
    service_kind   text NOT NULL
);

CREATE TABLE technician (
    technician_id  text PRIMARY KEY,
    full_name      text NOT NULL,
    service_org_id text NOT NULL REFERENCES service_organization,
    territory_id   text NOT NULL REFERENCES territory,
    schedule       text,
    busy_until     date
);

CREATE TABLE technician_skill (
    technician_id text NOT NULL REFERENCES technician,
    skill_id      text NOT NULL REFERENCES skill,
    PRIMARY KEY (technician_id, skill_id)
);

-- Исполнитель, закреплённый в карточке оборудования и в договоре, — первые
-- два звена цепочки приоритетов подбора исполнителя (PRD, раздел 5.4).
CREATE TABLE equipment (
    equipment_id           text PRIMARY KEY,
    serial_number          text NOT NULL UNIQUE,
    model_id               text NOT NULL REFERENCES model,
    partner_id             text NOT NULL REFERENCES partner,
    sale_date              date NOT NULL,
    location               text,
    status                 text NOT NULL,
    assigned_technician_id text REFERENCES technician
);

CREATE TABLE contract (
    contract_id            text PRIMARY KEY,
    partner_id             text NOT NULL REFERENCES partner,
    kind                   text,
    valid_from             date,
    valid_to               date,
    reaction_hours         integer,
    resolution_sla         text,
    coverage               text,
    assigned_technician_id text REFERENCES technician
);

CREATE TABLE service_center (
    center_id      text PRIMARY KEY,
    name           text NOT NULL,
    is_internal    boolean NOT NULL,
    service_org_id text NOT NULL REFERENCES service_organization,
    supplier_id    text,
    territory_id   text NOT NULL REFERENCES territory,
    address        text
);

CREATE TABLE service_center_brand (
    center_id text NOT NULL REFERENCES service_center,
    brand     text NOT NULL,
    PRIMARY KEY (center_id, brand)
);

CREATE TABLE service_center_skill (
    center_id text NOT NULL REFERENCES service_center,
    skill_id  text NOT NULL REFERENCES skill,
    PRIMARY KEY (center_id, skill_id)
);

CREATE TABLE part (
    part_id                text PRIMARY KEY,
    name                   text NOT NULL,
    stock                  integer NOT NULL,
    warehouse_territory_id text NOT NULL REFERENCES territory,
    price_rub              integer NOT NULL,
    lead_time_days         integer NOT NULL
);

CREATE TABLE part_model (
    part_id  text NOT NULL REFERENCES part,
    model_id text NOT NULL REFERENCES model,
    PRIMARY KEY (part_id, model_id)
);

CREATE TABLE service_history (
    service_order_id text PRIMARY KEY,
    equipment_id     text NOT NULL REFERENCES equipment,
    date             date,
    symptom          text,
    error_code       text,
    diagnosis        text,
    work_done        text,
    part_id          text,
    technician_id    text,
    billing          text,
    labor_hours      numeric
);

-- Реестр прав учётной системы: права проверяются в источнике (ADR-0006).
CREATE TABLE app_user (
    user_id    text PRIMARY KEY,
    full_name  text NOT NULL,
    role       text NOT NULL,
    partner_id text REFERENCES partner
);

CREATE TABLE user_territory (
    user_id      text NOT NULL REFERENCES app_user,
    territory_id text NOT NULL REFERENCES territory,
    PRIMARY KEY (user_id, territory_id)
);

CREATE SEQUENCE work_order_draft_seq;

CREATE TABLE work_order_draft (
    document_id          text PRIMARY KEY,
    idempotency_key      text NOT NULL UNIQUE,
    request_hash         text NOT NULL,
    equipment_id         text NOT NULL REFERENCES equipment,
    partner_id           text NOT NULL REFERENCES partner,
    execution_mode       text NOT NULL,
    fulfillment_type     text NOT NULL,
    service_center_id    text REFERENCES service_center,
    technician_id        text REFERENCES technician,
    warranty_preliminary text NOT NULL,
    decision_ref         text NOT NULL,
    facts_snapshot       jsonb NOT NULL,
    created_by           text NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now()
);

-- Аудит учётной системы: отказы в доступе и создание документов.
CREATE TABLE audit_event (
    id                    bigserial PRIMARY KEY,
    occurred_at           timestamptz NOT NULL DEFAULT now(),
    event_type            text NOT NULL,
    user_id               text,
    endpoint              text NOT NULL,
    object_type           text,
    object_id             text,
    object_territory_id   text,
    claimed_territories   text[] NOT NULL DEFAULT '{}',
    effective_territories text[] NOT NULL DEFAULT '{}',
    detail                text
);

CREATE INDEX audit_event_user_idx ON audit_event (user_id, occurred_at);
