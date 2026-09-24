# Мок учётной системы. Контекст сборки — корень репозитория: образу нужны
# справочники data/reference, data/synthetic и их загрузчик data/loader.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv

WORKDIR /srv

COPY mock-erp/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY data/loader /srv/data/loader
COPY data/reference /srv/data/reference
COPY data/synthetic /srv/data/synthetic
# Оборудование для демонстрации с настоящими фотографиями табличек (data/demo/README.md).
COPY data/demo /srv/data/demo
COPY mock-erp/app /srv/app

EXPOSE 8100

# Заполнение при пустой базе, затем сервер. Повторный старт данные не трогает.
CMD ["sh", "-c", "python -m app.seed --if-empty && exec uvicorn app.main:app --host 0.0.0.0 --port 8100"]
