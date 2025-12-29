# ----------------------------------------------------------
# Версия файла: 0.3.0
# Описание: Dockerfile backend-сервиса VPN (FastAPI + WG-Easy API)
# Дата изменения: 2025-12-29
# Изменения:
#  - Базовый образ обновлён до python:3.12-slim для поддержки wg-easy-api
#  - Установлены зависимости из requirements.txt
#  - Запуск uvicorn в качестве ASGI-сервера
# ----------------------------------------------------------

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Базовые утилиты для сборки некоторых пакетов (psycopg2-binary и др.)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Копируем исходный код backend в контейнер
COPY . /app

# Стартуем FastAPI-приложение через uvicorn
CMD ["uvicorn", "app_main:app", "--host", "0.0.0.0", "--port", "8000"]
