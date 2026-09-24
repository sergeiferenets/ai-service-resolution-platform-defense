# Платформа: API, оркестратор, инструменты, адаптер. Контекст сборки — корень
# репозитория. Из data/ копируются только справочники платформы: территории,
# пользователи и их доступ, свод правил. Мастер-данные учётной системы в образ
# не входят — платформа получает их через адаптер (data-model.md, раздел 1).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv

WORKDIR /srv

COPY backend/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY data/loader /srv/data/loader
COPY data/reference/rules.yaml data/reference/04_decision_rules.csv data/reference/02_error_codes.csv \
     data/reference/08_users.csv data/reference/19_territory_access.csv \
     data/reference/10_documents.csv data/reference/01_models.csv /srv/data/reference/
COPY data/synthetic/territories.csv /srv/data/synthetic/territories.csv
# Корпус знаний: из него считается версия корпуса в наборе артефактов и он же
# загружается в векторное хранилище (python -m backend.knowledge.loader).
COPY data/corpus /srv/data/corpus
COPY backend /srv/backend

# Коммит — после установки зависимостей: его смена не сбрасывает кэш слоёв.
ARG CODE_COMMIT=unknown
ENV CODE_COMMIT=${CODE_COMMIT}

EXPOSE 8000

# Миграции и справочники платформы, затем сервер. Загрузка идемпотентна.
CMD ["sh", "-c", "python -m backend.orchestrator.seed && exec uvicorn backend.api.main:app --host 0.0.0.0 --port 8000"]
