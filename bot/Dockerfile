# ----------------------------------------------------------
# Версия файла: 1.0.0
# Описание: Dockerfile для Telegram-бота (aiogram)
# Дата изменения: 2025-12-29
# Изменения:
#  - начальная версия Docker-образа для бота
# ----------------------------------------------------------
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

CMD ["python", "bot_main.py"]
