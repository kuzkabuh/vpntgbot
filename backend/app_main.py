# ----------------------------------------------------------
# Версия файла: 1.1.0
# Описание: Backend VPN-проекта (FastAPI + PostgreSQL + WG-Easy)
# Дата изменения: 2025-12-29
#
# Основное:
#  - Загрузка конфигурации ТОЛЬКО из переменных окружения (.env)
#  - Подключение к PostgreSQL через SQLAlchemy
#  - Интеграция с WG-Easy через библиотеку wg-easy-api
#  - Эндпоинты: /health, /api/v1/vpn/peers/create
# ----------------------------------------------------------

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    create_engine,
    func,
    text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

# ВАЖНО: импорт в соответствии с подсказкой интерпретатора:
# ImportError: Did you mean: 'WGEasy'?
from wg_easy_api import WGEasy

# ----------------------------------------------------------
# Логирование
# ----------------------------------------------------------

logger = logging.getLogger("vpn-backend")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)

# ----------------------------------------------------------
# Конфигурация через переменные окружения (.env)
# ----------------------------------------------------------

# Блок БД: либо используем BACKEND_DB_DSN, либо собираем DSN из DB_*.
BACKEND_DB_DSN: Optional[str] = os.getenv("BACKEND_DB_DSN")

DB_HOST: Optional[str] = os.getenv("DB_HOST")
DB_PORT: Optional[str] = os.getenv("DB_PORT")
DB_NAME: Optional[str] = os.getenv("DB_NAME")
DB_USER: Optional[str] = os.getenv("DB_USER")
DB_PASSWORD: Optional[str] = os.getenv("DB_PASSWORD")

if BACKEND_DB_DSN:
    DATABASE_URL = BACKEND_DB_DSN
else:
    # Собираем DSN из отдельных переменных; никаких паролей в коде по умолчанию
    missing_vars = []
    for key, value in [
        ("DB_HOST", DB_HOST),
        ("DB_PORT", DB_PORT),
        ("DB_NAME", DB_NAME),
        ("DB_USER", DB_USER),
        ("DB_PASSWORD", DB_PASSWORD),
    ]:
        if not value:
            missing_vars.append(key)

    if missing_vars:
        # Намеренно падаем, чтобы не было "тихих" ошибок конфигурации
        raise RuntimeError(
            f"Не заданы обязательные переменные окружения для БД: {', '.join(missing_vars)}"
        )

    DATABASE_URL = (
        f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )

logger.info("Используется DSN БД: %s", DATABASE_URL.replace(DB_PASSWORD or "", "****"))

# WG-Easy (WG Dashboard)
WG_EASY_URL: str = os.getenv("WG_EASY_URL", "http://wg_dashboard:51821")
# Пароль не задаём по умолчанию в коде — только из окружения
WG_EASY_PASSWORD: Optional[str] = os.getenv("WG_EASY_PASSWORD") or os.getenv(
    "WG_DASHBOARD_PASSWORD"
)

if not WG_EASY_PASSWORD:
    raise RuntimeError(
        "Не задан пароль WG_EASY_PASSWORD или WG_DASHBOARD_PASSWORD в окружении. "
        "Установи его в .env перед запуском backend."
    )

# Локация по умолчанию (Нидерланды)
WG_DEFAULT_LOCATION_CODE: str = os.getenv("WG_DEFAULT_LOCATION_CODE", "eu-nl")
WG_DEFAULT_LOCATION_NAME: str = os.getenv(
    "WG_DEFAULT_LOCATION_NAME", "Нидерланды (по умолчанию)"
)

# ----------------------------------------------------------
# Инициализация WG-Easy API клиента
# ----------------------------------------------------------

# Клиент асинхронный, но создавать его можно один раз
wg_client = WGEasy(WG_EASY_URL, WG_EASY_PASSWORD)

# ----------------------------------------------------------
# SQLAlchemy: Engine, Session, Base
# ----------------------------------------------------------

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db() -> Session:
    """
    Зависимость FastAPI для работы с сессией БД.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ----------------------------------------------------------
# Модели БД
# ----------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(String(64), unique=True, index=True, nullable=False)
    telegram_username = Column(String(255), nullable=True)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    vpn_peers = relationship("VpnPeer", back_populates="user")


class VpnPeer(Base):
    __tablename__ = "vpn_peers"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # ID клиента в WG-Easy (строка, так как wg-easy использует ObjectId-подобные идентификаторы)
    wg_client_id = Column(String(128), unique=True, index=True, nullable=False)
    client_name = Column(String(255), nullable=False)

    location_code = Column(String(32), nullable=False)
    location_name = Column(String(255), nullable=False)

    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user = relationship("User", back_populates="vpn_peers")


# ----------------------------------------------------------
# Pydantic-схемы
# ----------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = Field(..., description="Статус сервиса")
    timestamp: datetime = Field(..., description="Время ответа")
    database_ok: bool = Field(..., description="Доступность БД")
    wg_easy_url: str = Field(..., description="URL WG-Easy")


class PeerCreateRequest(BaseModel):
    """
    Запрос на создание VPN-подключения (peer) для пользователя.
    """

    telegram_id: int = Field(..., description="Telegram ID пользователя")
    telegram_username: Optional[str] = Field(
        None, description="Username пользователя в Telegram"
    )
    location_code: Optional[str] = Field(
        None, description="Код локации сервера (например, eu-nl)"
    )
    location_name: Optional[str] = Field(
        None, description="Человекочитаемое название локации"
    )


class PeerCreateResponse(BaseModel):
    """
    Ответ с конфигурацией WireGuard для клиента.
    """

    client_id: str = Field(..., description="ID клиента в WG-Easy")
    client_name: str = Field(..., description="Имя клиента в WG-Easy")
    location_code: str = Field(..., description="Код локации сервера")
    location_name: str = Field(..., description="Название локации сервера")
    config: str = Field(..., description="WireGuard конфигурация (текст .conf")


# ----------------------------------------------------------
# Утилиты для работы с БД
# ----------------------------------------------------------


def get_or_create_user(
    db: Session, telegram_id: int, telegram_username: Optional[str]
) -> User:
    """
    Находим пользователя по Telegram ID или создаём нового.
    """
    user = (
        db.query(User)
        .filter(User.telegram_id == str(telegram_id))
        .one_or_none()
    )
    if user:
        # Обновляем username, если поменялся
        if telegram_username and user.telegram_username != telegram_username:
            user.telegram_username = telegram_username
            db.add(user)
            db.commit()
            db.refresh(user)
        return user

    user = User(
        telegram_id=str(telegram_id),
        telegram_username=telegram_username,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ----------------------------------------------------------
# Инициализация FastAPI
# ----------------------------------------------------------

app = FastAPI(
    title="VPN Service Backend",
    description="Backend-сервис для Telegram VPN-бота с интеграцией WG-Easy",
    version="1.1.0",
)

# CORS при необходимости (можно ограничить доменами админки)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # на бою лучше ограничить
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------
# События приложения
# ----------------------------------------------------------


@app.on_event("startup")
def on_startup() -> None:
    """
    Событие старта приложения:
    - логируем используемый DSN
    - проверяем, что соединение с БД устанавливается
    """
    logger.info("vpn-backend: Старт backend-сервиса, инициализация БД...")
    try:
        # Проверяем, что к БД реально можно подключиться и выполнить простой запрос
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        logger.info("vpn-backend: Подключение к БД успешно, backend готов к работе.")
    except Exception as e:
        logger.error(
            "vpn-backend: Ошибка подключения к БД при старте: %s",
            e,
            exc_info=True,
        )
        # Если на старте не можем подключиться к БД — считаем это критической ошибкой
        raise


# ----------------------------------------------------------
# Эндпоинты
# ----------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
def health(db: Session = Depends(get_db)) -> HealthResponse:
    """
    Простой health-check:
      - проверка подключения к БД,
      - возврат базовой информации.
    """
    db_ok = True
    try:
        db.execute("SELECT 1")
    except Exception as exc:
        logger.error("Health-check: ошибка БД: %s", exc)
        db_ok = False

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        timestamp=datetime.utcnow(),
        database_ok=db_ok,
        wg_easy_url=WG_EASY_URL,
    )


@app.post(
    "/api/v1/vpn/peers/create",
    response_model=PeerCreateResponse,
    summary="Создать клиентскую конфигурацию WireGuard через WG-Easy",
)
async def create_vpn_peer(
    payload: PeerCreateRequest,
    db: Session = Depends(get_db),
) -> PeerCreateResponse:
    """
    Создаёт нового клиента (peer) в WG-Easy и сохраняет связь в БД.

    Логика:
      1) Находим/создаём пользователя по telegram_id.
      2) Проверяем, есть ли уже активный peer для этого пользователя в выбранной локации.
         - если есть, просто возвращаем существующую конфигурацию;
      3) Если нет — создаём нового клиента в WG-Easy, сохраняем его ID и возвращаем конфиг.
    """

    # 1. Пользователь
    user = get_or_create_user(
        db=db,
        telegram_id=payload.telegram_id,
        telegram_username=payload.telegram_username,
    )

    location_code = payload.location_code or WG_DEFAULT_LOCATION_CODE
    location_name = payload.location_name or WG_DEFAULT_LOCATION_NAME

    # 2. Проверяем, есть ли уже peer в этой локации
    existing_peer: Optional[VpnPeer] = (
        db.query(VpnPeer)
        .filter(
            VpnPeer.user_id == user.id,
            VpnPeer.location_code == location_code,
            VpnPeer.is_active.is_(True),
        )
        .one_or_none()
    )

    # WG-Easy работает асинхронно
    try:
        if existing_peer:
            logger.info(
                "Найден существующий peer user_id=%s, wg_client_id=%s",
                user.id,
                existing_peer.wg_client_id,
            )
            config_text: str = await wg_client.get_client_config(
                existing_peer.wg_client_id
            )

            return PeerCreateResponse(
                client_id=existing_peer.wg_client_id,
                client_name=existing_peer.client_name,
                location_code=existing_peer.location_code,
                location_name=existing_peer.location_name,
                config=config_text,
            )

        # 3. Создаём нового клиента в WG-Easy
        client_name = f"tg_{user.telegram_id}_{location_code}"

        logger.info(
            "Создаём нового WG-клиента: user_id=%s, name=%s, location=%s",
            user.id,
            client_name,
            location_code,
        )

        new_client = await wg_client.create_client(client_name)

        # Объект new_client имеет атрибут id (см. README wg-easy-api)
        wg_client_id: str = new_client.id

        # Получаем конфиг клиента
        config_text: str = await wg_client.get_client_config(wg_client_id)

        # Сохраняем в БД
        peer = VpnPeer(
            user_id=user.id,
            wg_client_id=wg_client_id,
            client_name=client_name,
            location_code=location_code,
            location_name=location_name,
            is_active=True,
        )
        db.add(peer)
        db.commit()
        db.refresh(peer)

        return PeerCreateResponse(
            client_id=peer.wg_client_id,
            client_name=peer.client_name,
            location_code=peer.location_code,
            location_name=peer.location_name,
            config=config_text,
        )

    except HTTPException:
        # пробрасываем HTTPException как есть
        raise
    except Exception as exc:
        logger.exception("Ошибка при работе с WG-Easy: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ошибка при взаимодействии с WG-Easy, попробуйте позже.",
        ) from exc
