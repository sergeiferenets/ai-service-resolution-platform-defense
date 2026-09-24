# Демонстрационный интерфейс. Бизнес-логики в образе нет: только вызовы API.
FROM python:3.11-slim

WORKDIR /app
COPY ui/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY ui/app.py ./app.py

EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", \
     "--server.headless=true", "--browser.gatherUsageStats=false"]
