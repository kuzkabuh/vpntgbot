# ----------------------------------------------------------
# Версия файла: 1.2.0
# Описание: Backend VPN-проекта (FastAPI + PostgreSQL + WG-Easy)
# Дата изменения: 2025-12-29
#
# Основное:
#  - Загрузка конфигурации из переменных окружения
#  - Подключение к PostgreSQL через SQLAlchemy
#  - Интеграция с WG-Easy через библиотеку wg-easy-api
#  - Эндпоинты: /health, /api/v1/vpn/peers/create,
#    /api/v1/users/from-telegram, /api/v1/users/{telegram_id}/subscription/active,
#    /api/v1/users/{telegram_id}/trial/activate
# ----------------------------------------------------------

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from wg_easy_api import WGEasy

from config import get_settings
from db import db_session, get_db
from models import Subscription, SubscriptionPlan, User, VpnPeer
from schemas import (
    SubscriptionPlanOut,
    SubscriptionStatusResponse,
    TelegramUserIn,
    TrialGrantResponse,
    UserFromTelegramResponse,
    UserOut,
)

# ----------------------------------------------------------
# Логирование
# ----------------------------------------------------------

logger = logging.getLogger("vpn-backend")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)

# ----------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------

settings = get_settings()

WG_DEFAULT_LOCATION_CODE = settings.default_location_code
WG_DEFAULT_LOCATION_NAME = settings.default_location_name

# ----------------------------------------------------------
# Инициализация WG-Easy API клиента
# ----------------------------------------------------------

wg_client = WGEasy(settings.wg_easy_url, settings.wg_easy_password)

# ----------------------------------------------------------
# Pydantic-схемы локально
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
    device_name: Optional[str] = Field(
        None, description="Имя устройства (если передано ботом)"
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
# Утилиты
# ----------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_or_create_user(db: Session, payload: TelegramUserIn) -> tuple[User, bool]:
    user = db.execute(
        select(User).where(User.telegram_id == payload.telegram_id)
    ).scalar_one_or_none()

    if user:
        updated = False
        if payload.username and user.username != payload.username:
            user.username = payload.username
            updated = True
        if payload.first_name and user.first_name != payload.first_name:
            user.first_name = payload.first_name
            updated = True
        if payload.last_name and user.last_name != payload.last_name:
            user.last_name = payload.last_name
            updated = True
        if payload.language_code and user.language_code != payload.language_code:
            user.language_code = payload.language_code
            updated = True
        if updated:
            db.add(user)
            db.commit()
            db.refresh(user)
        return user, False

    user = User(
        telegram_id=payload.telegram_id,
        username=payload.username,
        first_name=payload.first_name,
        last_name=payload.last_name,
        language_code=payload.language_code,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user, True


def get_active_subscription(db: Session, user_id: int) -> Optional[Subscription]:
    now = utcnow()
    return (
        db.execute(
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.is_active.is_(True),
                Subscription.ends_at >= now,
            )
            .order_by(Subscription.ends_at.desc())
        )
        .scalars()
        .first()
    )


def has_had_trial(db: Session, user_id: int) -> bool:
    return (
        db.execute(
            select(Subscription)
            .join(SubscriptionPlan, Subscription.plan_id == SubscriptionPlan.id)
            .where(
                Subscription.user_id == user_id,
                SubscriptionPlan.is_trial.is_(True),
            )
        )
        .scalars()
        .first()
        is not None
    )


def get_or_create_trial_plan(db: Session) -> SubscriptionPlan:
    plan = db.execute(
        select(SubscriptionPlan).where(SubscriptionPlan.code == "trial_10")
    ).scalar_one_or_none()
    if plan:
        return plan

    plan = SubscriptionPlan(
        code="trial_10",
        name="Бесплатный триал на 10 дней",
        duration_days=10,
        price_stars=0,
        is_trial=True,
        is_active=True,
        sort_order=0,
        max_devices=None,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def build_subscription_status(db: Session, user: User) -> SubscriptionStatusResponse:
    active = get_active_subscription(db, user.id)
    trial_used = has_had_trial(db, user.id)

    if active:
        plan_name = active.plan.name if active.plan else None
        return SubscriptionStatusResponse(
            has_active_subscription=True,
            is_trial_active=active.is_trial,
            active_plan_name=plan_name,
            subscription_ends_at=active.ends_at,
            trial_available=not trial_used,
        )

    return SubscriptionStatusResponse(
        has_active_subscription=False,
        is_trial_active=False,
        active_plan_name=None,
        subscription_ends_at=None,
        trial_available=not trial_used,
    )


# ----------------------------------------------------------
# Инициализация FastAPI
# ----------------------------------------------------------

app = FastAPI(
    title="VPN Service Backend",
    description="Backend-сервис для Telegram VPN-бота с интеграцией WG-Easy",
    version="1.2.0",
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
    - проверяем, что соединение с БД устанавливается
    """
    logger.info("vpn-backend: Старт backend-сервиса, инициализация БД...")
    try:
        with db_session() as session:
            session.execute(text("SELECT 1"))
        logger.info("vpn-backend: Подключение к БД успешно, backend готов к работе.")
    except Exception as exc:
        logger.error(
            "vpn-backend: Ошибка подключения к БД при старте: %s",
            exc,
            exc_info=True,
        )
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
        db.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("Health-check: ошибка БД: %s", exc)
        db_ok = False

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        timestamp=utcnow(),
        database_ok=db_ok,
        wg_easy_url=settings.wg_easy_url,
    )


@app.post(
    "/api/v1/users/from-telegram",
    response_model=UserFromTelegramResponse,
    summary="Регистрация/обновление пользователя из Telegram",
)
def register_user_from_telegram(
    payload: TelegramUserIn,
    db: Session = Depends(get_db),
) -> UserFromTelegramResponse:
    user, is_new = get_or_create_user(db, payload)
    status_data = build_subscription_status(db, user)

    return UserFromTelegramResponse(
        user=UserOut.model_validate(user),
        is_new=is_new,
        has_active_subscription=status_data.has_active_subscription,
        active_until=status_data.subscription_ends_at,
        has_had_trial=not status_data.trial_available,
        is_trial_active=status_data.is_trial_active,
        active_plan_name=status_data.active_plan_name,
        subscription_ends_at=status_data.subscription_ends_at,
        trial_available=status_data.trial_available,
    )


@app.get(
    "/api/v1/users/{telegram_id}/subscription/active",
    response_model=SubscriptionStatusResponse,
    summary="Получить статус активной подписки",
)
def get_subscription_status(
    telegram_id: int,
    db: Session = Depends(get_db),
) -> SubscriptionStatusResponse:
    user = db.execute(
        select(User).where(User.telegram_id == telegram_id)
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    return build_subscription_status(db, user)


@app.post(
    "/api/v1/users/{telegram_id}/trial/activate",
    response_model=TrialGrantResponse,
    summary="Активировать бесплатный триал",
)
def activate_trial(
    telegram_id: int,
    db: Session = Depends(get_db),
) -> TrialGrantResponse:
    user = db.execute(
        select(User).where(User.telegram_id == telegram_id)
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    trial_used = has_had_trial(db, user.id)
    if trial_used:
        return TrialGrantResponse(
            success=False,
            message="Бесплатный пробный период уже был использован ранее.",
            trial_ends_at=None,
            user=UserOut.model_validate(user),
            plan=None,
            already_had_trial=True,
        )

    active_subscription = get_active_subscription(db, user.id)
    if active_subscription:
        return TrialGrantResponse(
            success=False,
            message="У вас уже есть активная подписка. Триал недоступен.",
            trial_ends_at=None,
            user=UserOut.model_validate(user),
            plan=None,
            already_had_trial=False,
        )

    plan = get_or_create_trial_plan(db)
    starts_at = utcnow()
    ends_at = starts_at + timedelta(days=plan.duration_days)

    subscription = Subscription(
        user_id=user.id,
        plan_id=plan.id,
        server_id=None,
        starts_at=starts_at,
        ends_at=ends_at,
        is_active=True,
        is_trial=True,
        source="trial",
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)

    return TrialGrantResponse(
        success=True,
        message="Бесплатный пробный период успешно активирован.",
        trial_ends_at=subscription.ends_at,
        user=UserOut.model_validate(user),
        plan=SubscriptionPlanOut.model_validate(plan),
        already_had_trial=False,
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
         - если есть, возвращаем существующую конфигурацию;
      3) Если нет — создаём нового клиента в WG-Easy, сохраняем его ID и возвращаем конфиг.
    """

    user, _ = get_or_create_user(
        db,
        TelegramUserIn(
            telegram_id=payload.telegram_id,
            username=payload.telegram_username,
            first_name=None,
            last_name=None,
            language_code=None,
        ),
    )

    location_code = payload.location_code or WG_DEFAULT_LOCATION_CODE
    location_name = payload.location_name or WG_DEFAULT_LOCATION_NAME

    existing_peer = (
        db.execute(
            select(VpnPeer).where(
                VpnPeer.user_id == user.id,
                VpnPeer.location_code == location_code,
                VpnPeer.is_active.is_(True),
            )
        )
        .scalars()
        .first()
    )

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

        client_name = payload.device_name or f"tg_{user.telegram_id}_{location_code}"

        logger.info(
            "Создаём нового WG-клиента: user_id=%s, name=%s, location=%s",
            user.id,
            client_name,
            location_code,
        )

        new_client = await wg_client.create_client(client_name)
        wg_client_id: str = new_client.id

        config_text: str = await wg_client.get_client_config(wg_client_id)

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
        raise
    except Exception as exc:
        logger.exception("Ошибка при работе с WG-Easy: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ошибка при взаимодействии с WG-Easy, попробуйте позже.",
        ) from exc
