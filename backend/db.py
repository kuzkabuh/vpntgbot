"""
# ----------------------------------------------------------
# Версия файла: 1.1.0
# Описание: Подключение к БД (SQLAlchemy), фабрика сессий
# Дата изменения: 2025-12-29
#
# Изменения (1.1.0):
#  - Добавлены настройки пула соединений для production (pool_size, max_overflow, pool_recycle)
#  - Добавлен pool_timeout и параметр echo (через APP_DEBUG)
#  - get_db теперь безопаснее: rollback при исключении во время обработки запроса
#  - db_session (contextmanager) оставлен для внутренних задач/скриптов
#  - Добавлена единая точка настройки Engine (под Alembic и runtime)
# ----------------------------------------------------------
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from config import get_settings

settings = get_settings()

# Параметры пула можно переопределять через ENV при необходимости.
# По умолчанию — безопасные значения для небольшого production.
POOL_SIZE = int((settings_env := getattr(settings, "pool_size", None)) or 5)  # fallback 5
MAX_OVERFLOW = int((settings_env := getattr(settings, "max_overflow", None)) or 10)  # fallback 10
POOL_RECYCLE = int((settings_env := getattr(settings, "pool_recycle", None)) or 1800)  # seconds
POOL_TIMEOUT = int((settings_env := getattr(settings, "pool_timeout", None)) or 30)  # seconds

engine = create_engine(
    settings.db_dsn,
    pool_pre_ping=True,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    pool_recycle=POOL_RECYCLE,
    pool_timeout=POOL_TIMEOUT,
    echo=bool(getattr(settings, "app_debug", False)),
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)


def get_db() -> Iterator[Session]:
    """
    FastAPI dependency: выдаёт сессию на запрос.

    Важно:
      - Если внутри запроса произошла ошибка, откатываем транзакцию,
        чтобы не оставить соединение в "грязном" состоянии.
      - Коммит делается явным образом в коде endpoint/service (как у вас сейчас).
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        # защита от "pending transaction" при исключениях в обработчиках
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def db_session() -> Iterator[Session]:
    """
    Контекстная сессия для внутренних задач (startup checks, скрипты, cron jobs).
    Внутри контекста по завершении делаем commit, при ошибке rollback.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
