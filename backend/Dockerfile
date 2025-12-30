# ----------------------------------------------------------
# Версия файла: 1.0.1
# Описание: Dockerfile backend-сервиса (FastAPI) для VPN-проекта
#           Добавлен curl для отладки и health-checks.
# Дата изменения: 2025-12-30
# ----------------------------------------------------------

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      build-essential \
      curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

EXPOSE 8000

CMD ["uvicorn", "app_main:app", "--host", "0.0.0.0", "--port", "8000"]
