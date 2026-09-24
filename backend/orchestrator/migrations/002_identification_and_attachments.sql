-- Уровень достоверности «выведен» (PRD 5.3, версия 0.4): оборудование
-- определено по партнёру и обозначению модели, серийный номер не указан.
ALTER TABLE identification DROP CONSTRAINT identification_confidence_level_check;
ALTER TABLE identification ADD CONSTRAINT identification_confidence_level_check
    CHECK (confidence_level IN ('подтверждён', 'распознан', 'выведен', 'со слов'));

-- REQUEST_ATTACHMENT (data-model.md): фотография идентификационной таблички.
-- Хеш — для журналов и сверки: в TOOL_CALL попадает он, а не содержимое.
CREATE TABLE request_attachment (
    id           uuid PRIMARY KEY,
    request_id   uuid NOT NULL REFERENCES service_request,
    kind         text NOT NULL CHECK (kind IN ('фото таблички')),
    content_type text NOT NULL CHECK (content_type IN ('image/jpeg', 'image/png')),
    size_bytes   int  NOT NULL,
    sha256       text NOT NULL,
    data         bytea NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX request_attachment_request ON request_attachment (request_id);
