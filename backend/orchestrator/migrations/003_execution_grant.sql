-- Разрешение на исполнение: сохранённое решение человека, без которого
-- запись в учётную систему невозможна (PRD 5.1, ADR-0006).
--
-- Одно разрешение на обращение и одно на решение: повторное подтверждение,
-- повтор после сбоя и гонка двух подтверждений ведут к тому же ключу
-- идемпотентности, а не ко второму документу. Отказ человека проверяет
-- отсутствие этой записи под блокировкой строки обращения, поэтому отказ и
-- разрешение не могут произойти оба.
CREATE TABLE execution_grant (
    id                uuid PRIMARY KEY,
    request_id        uuid NOT NULL UNIQUE REFERENCES service_request,
    decision_id       uuid NOT NULL UNIQUE REFERENCES decision,
    granted_by        text NOT NULL REFERENCES app_user,
    basis             text NOT NULL CHECK (basis IN ('подтверждение', 'согласование')),
    approval_required boolean NOT NULL,
    granted_at        timestamptz NOT NULL DEFAULT now()
);
