# ----------------------------------------------------------
# Версия файла: 1.3.1
# Описание: Backend VPN-проекта (FastAPI + PostgreSQL + WG-Easy)
# Дата изменения: 2025-12-29
#
# Основное:
#  - Загрузка конфигурации из переменных окружения
#  - Подключение к PostgreSQL через SQLAlchemy
#  - Интеграция с WG-Easy через библиотеку wg-easy-api
#  - Эндпоинты:
#       /health
#       /api/v1/users/from-telegram
#       /api/v1/users/{telegram_id}/subscription/active
#       /api/v1/users/{telegram_id}/trial/activate
#       /api/v1/vpn/peers/create
#       /api/v1/vpn/peers/list
#       /api/v1/vpn/peers/revoke
#       /api/v1/admin/users
#       /api/v1/admin/subscription-plans (list/create)
#
# Изменения (1.3.1):
#  - Убрана обязательная зависимость от httpx (падал контейнер).
#    WG probe теперь через urllib (stdlib) + опционально wg_easy_api.
#  - Улучшено поведение revoke: выставляем revoked_at (если поле существует).
#  - CORS: берем из settings.cors_origins если есть, иначе '*'.
# ----------------------------------------------------------

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
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

logger = logging.getLogger("vpn-backend")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)

settings = get_settings()

WG_DEFAULT_LOCATION_CODE = settings.default_location_code
WG_DEFAULT_LOCATION_NAME = settings.default_location_name

# WG-Easy клиент (через библиотеку)
wg_client = WGEasy(settings.wg_easy_url, settings.wg_easy_password)


# -----------------------------
# Pydantic схемы (локально)
# -----------------------------

class HealthResponse(BaseModel):
    status: str = Field(..., description="Статус сервиса (ok|degraded)")
    timestamp: datetime = Field(..., description="Время ответа (UTC)")
    database_ok: bool = Field(..., description="Доступность БД")
    wg_ok: bool = Field(..., description="Доступность WG-Easy")
    wg_easy_url: str = Field(..., description="URL WG-Easy")
    details: Optional[str] = Field(None, description="Доп. сведения/ошибка (если есть)")


class PeerCreateRequest(BaseModel):
    telegram_id: int = Field(..., description="Telegram ID пользователя")
    telegram_username: Optional[str] = Field(None, description="Username пользователя в Telegram")
    device_name: Optional[str] = Field(None, description="Имя устройства (если передано ботом)")
    location_code: Optional[str] = Field(None, description="Код локации сервера (например, eu-nl)")
    location_name: Optional[str] = Field(None, description="Человекочитаемое название локации")


class PeerCreateResponse(BaseModel):
    client_id: str = Field(..., description="ID клиента в WG-Easy")
    client_name: str = Field(..., description="Имя клиента в WG-Easy")
    location_code: str = Field(..., description="Код локации сервера")
    location_name: str = Field(..., description="Название локации сервера")
    config: str = Field(..., description="WireGuard конфигурация (текст .conf)")


class PeerListItem(BaseModel):
    client_id: str
    client_name: str
    location_code: str
    location_name: str
    is_active: bool
    created_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class PeerListResponse(BaseModel):
    telegram_id: int
    peers: list[PeerListItem]


class PeerRevokeRequest(BaseModel):
    telegram_id: int = Field(..., description="Telegram ID пользователя")
    client_id: str = Field(..., description="WG-Easy client_id, который нужно отозвать")
    location_code: Optional[str] = Field(None, description="Опционально: локация, если нужно уточнить")


class SubscriptionPlanCreate(BaseModel):
    code: str = Field(..., description="Уникальный код тарифа")
    name: str = Field(..., description="Название тарифа")
    duration_days: int = Field(..., ge=1, description="Длительность в днях")
    price_stars: float = Field(..., ge=0, description="Стоимость тарифа в Telegram Stars")
    is_trial: bool = Field(False, description="Пробный тариф")
    is_active: bool = Field(True, description="Активен/неактивен")
    sort_order: int = Field(0, description="Порядок сортировки")
    max_devices: Optional[int] = Field(None, ge=1, description="Лимит устройств (peers) на тариф")


# -----------------------------
# Утилиты
# -----------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_or_create_user(db: Session, payload: TelegramUserIn) -> tuple[User, bool]:
    user = db.execute(select(User).where(User.telegram_id == payload.telegram_id)).scalar_one_or_none()

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
    plan = db.execute(select(SubscriptionPlan).where(SubscriptionPlan.code == "trial_10")).scalar_one_or_none()
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


def require_active_subscription(db: Session, user: User) -> Subscription:
    sub = get_active_subscription(db, user.id)
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Нет активной подписки. Активируйте триал или оплатите тариф.",
        )
    if sub.plan and sub.plan.is_active is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Тарифный план отключён. Обратитесь в поддержку.",
        )
    return sub


def enforce_device_limit(db: Session, user: User, location_code: str, sub: Subscription) -> None:
    max_devices = None
    if sub.plan and sub.plan.max_devices:
        max_devices = sub.plan.max_devices

    if not max_devices:
        return

    active_peers_count = (
        db.execute(
            select(func.count(VpnPeer.id)).where(
                VpnPeer.user_id == user.id,
                VpnPeer.is_active.is_(True),
            )
        )
        .scalar_one()
        or 0
    )

    existing_peer_in_location = (
        db.execute(
            select(VpnPeer.id).where(
                VpnPeer.user_id == user.id,
                VpnPeer.location_code == location_code,
                VpnPeer.is_active.is_(True),
            )
        )
        .scalars()
        .first()
        is not None
    )

    if existing_peer_in_location:
        return

    if active_peers_count >= max_devices:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Достигнут лимит устройств по тарифу: {max_devices}. Отключите лишнее устройство.",
        )


async def wg_probe() -> Tuple[bool, Optional[str]]:
    """
    Проверка доступности WG-Easy:
      1) Пытаемся дернуть API через библиотеку (если метод get_clients существует)
      2) Fallback: TCP/HTTP доступность через urllib (без авторизации)
    """
    # 1) попытка через библиотеку
    try:
        get_clients = getattr(wg_client, "get_clients", None)
        if callable(get_clients):
            await get_clients()
            return True, None
    except Exception as exc:
        # не считаем окончательно — попробуем TCP
        lib_err = f"wg-easy api error: {exc}"
    else:
        lib_err = None

    # 2) fallback: просто проверяем что вебка отвечает
    try:
        url = settings.wg_easy_url.rstrip("/")
        req = Request(url, method="GET")
        with urlopen(req, timeout=3) as resp:
            code = getattr(resp, "status", 200)
        # 200..499 — сервис жив (401/403 допустимы без авторизации)
        if 200 <= int(code) < 500:
            return True, None
        return False, f"wg-easy unexpected status: {code}"
    except HTTPError as exc:
        # HTTPError — это тоже ответ сервиса (например 401/403)
        if 200 <= exc.code < 500:
            return True, None
        return False, f"wg-easy http error: {exc.code}"
    except URLError as exc:
        return False, lib_err or f"wg-easy unreachable: {exc}"
    except Exception as exc:
        return False, lib_err or f"wg-easy probe failed: {exc}"


# -----------------------------
# Админский токен
# -----------------------------

mgmt_api_header = APIKeyHeader(name="X-Mgmt-Token", auto_error=True)


def require_mgmt_token(api_key: str = Depends(mgmt_api_header)) -> str:
    if not settings.mgmt_api_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="MGMT_API_TOKEN не настроен на сервере",
        )
    if api_key != settings.mgmt_api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный токен управления")
    return api_key


# -----------------------------
# FastAPI init
# -----------------------------

app = FastAPI(
    title="VPN Service Backend",
    description="Backend-сервис для Telegram VPN-бота с интеграцией WG-Easy",
    version="1.3.1",
)

cors_origins = getattr(settings, "cors_origins", None) or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    logger.info("vpn-backend: Старт backend-сервиса, инициализация БД...")
    try:
        with db_session() as session:
            session.execute(text("SELECT 1"))
        logger.info("vpn-backend: Подключение к БД успешно, backend готов к работе.")
    except Exception as exc:
        logger.error("vpn-backend: Ошибка подключения к БД при старте: %s", exc, exc_info=True)
        raise


# -----------------------------
# Endpoints
# -----------------------------

@app.get("/health", response_model=HealthResponse)
async def health(db: Session = Depends(get_db)) -> HealthResponse:
    db_ok = True
    details: Optional[str] = None

    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("Health-check: ошибка БД: %s", exc)
        db_ok = False
        details = f"db error: {exc}"

    wg_ok, wg_details = await wg_probe()
    if not wg_ok and not details:
        details = wg_details

    overall_ok = db_ok and wg_ok
    return HealthResponse(
        status="ok" if overall_ok else "degraded",
        timestamp=utcnow(),
        database_ok=db_ok,
        wg_ok=wg_ok,
        wg_easy_url=settings.wg_easy_url,
        details=details,
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
    user = db.execute(select(User).where(User.telegram_id == telegram_id)).scalar_one_or_none()
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
    user = db.execute(select(User).where(User.telegram_id == telegram_id)).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    if has_had_trial(db, user.id):
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

    active_sub = require_active_subscription(db, user)

    location_code = payload.location_code or WG_DEFAULT_LOCATION_CODE
    location_name = payload.location_name or WG_DEFAULT_LOCATION_NAME

    enforce_device_limit(db, user, location_code, active_sub)

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
            config_text: str = await wg_client.get_client_config(existing_peer.wg_client_id)
            return PeerCreateResponse(
                client_id=existing_peer.wg_client_id,
                client_name=existing_peer.client_name,
                location_code=existing_peer.location_code,
                location_name=existing_peer.location_name,
                config=config_text,
            )

        client_name = payload.device_name or f"tg_{user.telegram_id}_{location_code}"
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


@app.get(
    "/api/v1/vpn/peers/list",
    response_model=PeerListResponse,
    summary="Список VPN peers пользователя",
)
def list_vpn_peers(
    telegram_id: int,
    db: Session = Depends(get_db),
) -> PeerListResponse:
    user = db.execute(select(User).where(User.telegram_id == telegram_id)).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    peers = (
        db.execute(select(VpnPeer).where(VpnPeer.user_id == user.id).order_by(VpnPeer.created_at.desc()))
        .scalars()
        .all()
    )

    result = []
    for p in peers:
        result.append(
            PeerListItem(
                client_id=p.wg_client_id,
                client_name=p.client_name,
                location_code=p.location_code,
                location_name=p.location_name,
                is_active=p.is_active,
                created_at=getattr(p, "created_at", None),
                revoked_at=getattr(p, "revoked_at", None),
            )
        )

    return PeerListResponse(telegram_id=telegram_id, peers=result)


@app.post(
    "/api/v1/vpn/peers/revoke",
    summary="Отозвать (деактивировать) peer пользователя",
)
async def revoke_vpn_peer(
    payload: PeerRevokeRequest,
    db: Session = Depends(get_db),
) -> dict:
    user = db.execute(select(User).where(User.telegram_id == payload.telegram_id)).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    q = select(VpnPeer).where(
        VpnPeer.user_id == user.id,
        VpnPeer.wg_client_id == payload.client_id,
    )
    if payload.location_code:
        q = q.where(VpnPeer.location_code == payload.location_code)

    peer = db.execute(q).scalar_one_or_none()
    if not peer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Peer не найден")

    if not peer.is_active:
        return {"ok": True, "message": "Peer уже деактивирован"}

    # Пытаемся удалить/отключить клиента в WG-Easy (если метод доступен)
    try:
        delete_client = getattr(wg_client, "delete_client", None)
        disable_client = getattr(wg_client, "disable_client", None)

        if callable(delete_client):
            await delete_client(peer.wg_client_id)
        elif callable(disable_client):
            await disable_client(peer.wg_client_id)
    except Exception as exc:
        logger.warning("Не удалось удалить/отключить клиента в WG-Easy: %s", exc)

    peer.is_active = False
    # если в модели есть revoked_at — фиксируем
    if hasattr(peer, "revoked_at"):
        setattr(peer, "revoked_at", utcnow())

    db.add(peer)
    db.commit()

    return {"ok": True, "message": "Peer деактивирован"}


# -----------------------------
# Admin API
# -----------------------------

@app.get(
    "/api/v1/admin/users",
    response_model=list[UserOut],
    summary="Список пользователей (admin)",
    tags=["admin"],
)
def admin_list_users(
    _token: str = Depends(require_mgmt_token),
    db: Session = Depends(get_db),
) -> list[UserOut]:
    users = db.execute(select(User).order_by(User.created_at.desc())).scalars().all()
    return [UserOut.model_validate(u) for u in users]


@app.get(
    "/api/v1/admin/subscription-plans",
    response_model=list[SubscriptionPlanOut],
    summary="Список тарифов (admin)",
    tags=["admin"],
)
def admin_list_plans(
    _token: str = Depends(require_mgmt_token),
    db: Session = Depends(get_db),
) -> list[SubscriptionPlanOut]:
    plans = db.execute(select(SubscriptionPlan).order_by(SubscriptionPlan.sort_order.asc())).scalars().all()
    return [SubscriptionPlanOut.model_validate(p) for p in plans]


@app.post(
    "/api/v1/admin/subscription-plans",
    response_model=SubscriptionPlanOut,
    summary="Создать тариф (admin)",
    tags=["admin"],
)
def admin_create_plan(
    payload: SubscriptionPlanCreate,
    _token: str = Depends(require_mgmt_token),
    db: Session = Depends(get_db),
) -> SubscriptionPlanOut:
    existing = db.execute(select(SubscriptionPlan).where(SubscriptionPlan.code == payload.code)).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Тариф с таким code уже существует")

    plan = SubscriptionPlan(
        code=payload.code,
        name=payload.name,
        duration_days=payload.duration_days,
        price_stars=payload.price_stars,
        is_trial=payload.is_trial,
        is_active=payload.is_active,
        sort_order=payload.sort_order,
        max_devices=payload.max_devices,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return SubscriptionPlanOut.model_validate(plan)
