# ----------------------------------------------------------
# Версия файла: 1.4.6
# Описание: Backend VPN-проекта (FastAPI + PostgreSQL + WG-Easy v14)
# Дата изменения: 2025-12-30
#
# Основное:
#  - Загрузка конфигурации из переменных окружения
#  - Подключение к PostgreSQL через SQLAlchemy
#  - Интеграция с WG-Easy через нативный HTTP-клиент (aiohttp) с корректным JSON
#  - Эндпоинты:
#       /health
#       /api/v1/users/from-telegram
#       /api/v1/users/{telegram_id}/subscription/active
#       /api/v1/users/{telegram_id}/trial/activate
#       /api/v1/subscription-plans/active
#       /api/v1/vpn/peers/create
#       /api/v1/vpn/peers/list
#       /api/v1/vpn/peers/revoke
#       /api/v1/admin/users
#       /api/v1/admin/subscription-plans (list/create)
#
# Изменения (1.4.6):
#  - Исправлено получение пароля WG-Easy:
#      * ранее settings.wg_easy_password мог указывать на bcrypt-хеш (WG_EASY_PASSWORD_HASH),
#        из-за чего backend логинился хешем и получал 401.
#      * теперь backend ЯВНО берёт пароль из ENV WG_EASY_PASSWORD (plain), а если его нет —
#        использует settings.wg_easy_password только если это НЕ bcrypt-хеш.
#  - Улучшена диагностика: в лог добавляются длины и источник пароля (env/settings),
#    без вывода самого пароля.
# ----------------------------------------------------------

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

import aiohttp
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

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

_DEVICE_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-\.]+")
_BCRYPT_RE = re.compile(r"^\$2[aby]\$\d{2}\$.{10,}$")  # "$2b$10$...." и т.п.


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_str(v: Any) -> str:
    try:
        return str(v)
    except Exception:
        return "<unprintable>"


def _looks_like_bcrypt_hash(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return False
    return _BCRYPT_RE.match(v) is not None


def _resolve_wg_easy_password() -> tuple[str, str]:
    """
    Возвращает (password, source), где source: "env" | "settings".
    Требование:
      - для API /api/session нужен ПЛЕЙН пароль, а не bcrypt hash.
    """
    env_pass = (os.getenv("WG_EASY_PASSWORD") or "").strip()
    if env_pass:
        return env_pass, "env"

    cfg_pass = str(getattr(settings, "wg_easy_password", "") or "").strip()
    if cfg_pass and not _looks_like_bcrypt_hash(cfg_pass):
        return cfg_pass, "settings"

    # Если cfg_pass выглядит как bcrypt-хеш — это почти наверняка WG_EASY_PASSWORD_HASH
    # и использовать его для /api/session нельзя.
    return "", "settings"


@dataclass
class WGEasyHTTP:
    """
    Нативный HTTP-клиент WG-Easy (v14+) через aiohttp.
    Работает так же, как успешный curl:
      POST /api/session (JSON: {"password": "..."}), cookie connect.sid
      POST /api/wireguard/client (JSON: {"name": "..."}), success=true
      GET  /api/wireguard/client (JSON list)
      GET  /api/wireguard/client/{id}/configuration (text wireguard config)
      DELETE /api/wireguard/client/{id} (best-effort)
    """

    base_url: str
    password: str
    timeout_sec: float = 15.0

    def base(self) -> str:
        return (self.base_url or "").rstrip("/")

    def _timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=self.timeout_sec)

    async def _request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        *,
        json_data: Optional[dict] = None,
        expected_status: int = 200,
        return_json: bool = True,
    ) -> Any:
        url = f"{self.base()}{path}"
        try:
            async with session.request(method, url, json=json_data) as resp:
                content_type = (resp.headers.get("Content-Type") or "").lower()
                body_text = await resp.text()

                if resp.status != expected_status:
                    raise aiohttp.ClientResponseError(
                        request_info=resp.request_info,
                        history=resp.history,
                        status=resp.status,
                        message=body_text[:1500],
                        headers=resp.headers,
                    )

                if return_json:
                    if "application/json" in content_type:
                        return await resp.json()
                    return body_text
                return body_text
        except aiohttp.ClientResponseError:
            raise
        except Exception as exc:
            raise RuntimeError(f"WG-Easy request failed: method={method} url={url} err={exc!r}") from exc

    async def login(self, session: aiohttp.ClientSession) -> None:
        data = await self._request(
            session,
            "POST",
            "/api/session",
            json_data={"password": self.password},
            expected_status=200,
            return_json=True,
        )
        if not isinstance(data, dict) or data.get("success") is not True:
            raise RuntimeError(f"WG-Easy login failed: {data!r}")

    async def session_info(self, session: aiohttp.ClientSession) -> dict:
        data = await self._request(
            session,
            "GET",
            "/api/session",
            expected_status=200,
            return_json=True,
        )
        if isinstance(data, dict):
            return data
        return {"raw": str(data)}

    async def list_clients(self, session: aiohttp.ClientSession) -> list[dict]:
        data = await self._request(
            session,
            "GET",
            "/api/wireguard/client",
            expected_status=200,
            return_json=True,
        )
        if not isinstance(data, list):
            raise RuntimeError(f"WG-Easy clients list unexpected: {data!r}")
        return data

    async def create_client(self, session: aiohttp.ClientSession, name: str) -> None:
        data = await self._request(
            session,
            "POST",
            "/api/wireguard/client",
            json_data={"name": name},
            expected_status=200,
            return_json=True,
        )
        if not isinstance(data, dict) or data.get("success") is not True:
            raise RuntimeError(f"WG-Easy create_client failed: {data!r}")

    async def find_client_id_by_name(self, session: aiohttp.ClientSession, name: str) -> Optional[str]:
        clients = await self.list_clients(session)
        for c in clients:
            if str(c.get("name") or "") == name:
                cid = str(c.get("id") or "").strip()
                if cid:
                    return cid
        return None

    async def get_configuration(self, session: aiohttp.ClientSession, client_id: str) -> str:
        cfg = await self._request(
            session,
            "GET",
            f"/api/wireguard/client/{client_id}/configuration",
            expected_status=200,
            return_json=False,
        )
        return str(cfg or "")

    async def delete_client(self, session: aiohttp.ClientSession, client_id: str) -> bool:
        try:
            await self._request(
                session,
                "DELETE",
                f"/api/wireguard/client/{client_id}",
                expected_status=200,
                return_json=True,
            )
            return True
        except aiohttp.ClientResponseError as exc:
            if int(getattr(exc, "status", 0) or 0) in (204, 404):
                return True
            return False
        except Exception:
            return False

    async def create_and_get_config(self, client_name: str) -> tuple[str, str]:
        if not self.base():
            raise RuntimeError("WG_EASY_URL пустой")
        if not self.password:
            raise RuntimeError("WG_EASY_PASSWORD пустой")

        async with aiohttp.ClientSession(timeout=self._timeout()) as session:
            await self.login(session)
            await self.create_client(session, client_name)

            cid = await self.find_client_id_by_name(session, client_name)
            if not cid:
                raise RuntimeError("WG-Easy: client создан, но id не найден в списке клиентов")

            cfg = await self.get_configuration(session, cid)
            if not cfg.strip():
                raise RuntimeError("WG-Easy: конфиг пустой (configuration endpoint вернул пустую строку)")
            return cid, cfg

    async def get_config(self, client_id: str) -> str:
        async with aiohttp.ClientSession(timeout=self._timeout()) as session:
            await self.login(session)
            return await self.get_configuration(session, client_id)

    async def delete(self, client_id: str) -> bool:
        async with aiohttp.ClientSession(timeout=self._timeout()) as session:
            await self.login(session)
            return await self.delete_client(session, client_id)


def _init_wg_easy_client() -> WGEasyHTTP:
    url = str(getattr(settings, "wg_easy_url", "") or "").strip()
    password, source = _resolve_wg_easy_password()

    if not url:
        raise RuntimeError("WG_EASY_URL не задан в окружении backend.")

    if not password:
        cfg_hint = str(getattr(settings, "wg_easy_password", "") or "").strip()
        if _looks_like_bcrypt_hash(cfg_hint):
            raise RuntimeError(
                "WG_EASY_PASSWORD не задан/пустой, а settings.wg_easy_password похож на bcrypt-хеш. "
                "Для API /api/session нужен ПЛЕЙН пароль. Укажи WG_EASY_PASSWORD в .env (plain), "
                "а WG_EASY_PASSWORD_HASH используй только для UI wg-easy."
            )
        raise RuntimeError("WG_EASY_PASSWORD не задан в окружении backend (нужен для логина в API WG-Easy).")

    logger.info(
        "WG-Easy init: url=%s; password_source=%s; password_len=%s",
        url,
        source,
        len(password),
    )

    return WGEasyHTTP(base_url=url, password=password, timeout_sec=15.0)


wg_client = _init_wg_easy_client()


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


class PlansPublicResponse(BaseModel):
    plans: list[SubscriptionPlanOut] = Field(..., description="Список активных тарифов")


# -----------------------------
# Утилиты
# -----------------------------

def normalize_client_name(raw_name: str, fallback: str) -> str:
    name = (raw_name or "").strip()
    if not name:
        name = fallback

    name = name.replace(" ", "_")
    name = _DEVICE_SAFE_RE.sub("", name)
    if not name:
        name = fallback

    if len(name) > 48:
        name = name[:48]
    return name


def _summarize_wg_error(exc: Exception) -> str:
    parts: list[str] = [f"type={exc.__class__.__name__}"]

    status_code = getattr(exc, "status", None)
    message = getattr(exc, "message", None)

    url = None
    req_info = getattr(exc, "request_info", None)
    if req_info is not None:
        url = getattr(req_info, "real_url", None) or getattr(req_info, "url", None)
    url = url or getattr(exc, "url", None)

    if status_code is not None:
        parts.append(f"status={_safe_str(status_code)}")
    if message:
        parts.append(f"message={_safe_str(message)}")
    if url:
        parts.append(f"url={_safe_str(url)}")

    base = "; ".join(parts)

    if str(status_code) == "500":
        base += (
            " | hint=WG-Easy 500 часто означает проблему внутри WG-Easy: исчерпан пул IP (/24 ~ 253 клиента), "
            "повреждён /etc/wireguard, или ошибка конфигурации/volume."
        )
    if str(status_code) == "401":
        base += (
            " | hint=401 обычно означает неверный ПЛЕЙН пароль WG_EASY_PASSWORD для входа в WG-Easy API. "
            "Проверь, что backend использует WG_EASY_PASSWORD, а не WG_EASY_PASSWORD_HASH."
        )
    if str(status_code) == "403":
        base += " | hint=403 обычно означает запрет доступа/CSRF/неверная сессия. Проверь /api/session и cookie."

    return base


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
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5.0)) as session:
            _ = await wg_client.session_info(session)
            return True, None
    except aiohttp.ClientResponseError as exc:
        code = int(getattr(exc, "status", 0) or 0)
        if 200 <= code < 500:
            return True, None
        return False, f"wg-easy error: {_summarize_wg_error(exc)}"
    except Exception as exc:
        return False, f"wg-easy probe failed: {exc!r}"


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
    description="Backend-сервис для Telegram VPN-бота с интеграцией WG-Easy (v14)",
    version="1.4.6",
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
        logger.error("Health-check: ошибка БД: %s", exc, exc_info=True)
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


@app.get(
    "/api/v1/subscription-plans/active",
    response_model=PlansPublicResponse,
    summary="Публичный список активных тарифов (для бота/витрины)",
)
def public_active_plans(
    db: Session = Depends(get_db),
) -> PlansPublicResponse:
    plans = (
        db.execute(
            select(SubscriptionPlan)
            .where(SubscriptionPlan.is_active.is_(True))
            .order_by(SubscriptionPlan.sort_order.asc(), SubscriptionPlan.id.asc())
        )
        .scalars()
        .all()
    )
    return PlansPublicResponse(plans=[SubscriptionPlanOut.model_validate(p) for p in plans])


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
            config_text = await wg_client.get_config(existing_peer.wg_client_id)
            config_text = str(config_text or "")
            if not config_text.strip():
                raise RuntimeError("WG-Easy вернул пустой конфиг для существующего клиента")
            return PeerCreateResponse(
                client_id=existing_peer.wg_client_id,
                client_name=existing_peer.client_name,
                location_code=existing_peer.location_code,
                location_name=existing_peer.location_name,
                config=config_text,
            )

        fallback_name = f"tg_{user.telegram_id}_{location_code}"
        raw_name = payload.device_name or fallback_name
        client_name = normalize_client_name(raw_name, fallback=fallback_name)

        # Делаем имя уникальным для корректного поиска id по имени
        unique_suffix = int(utcnow().timestamp())
        if len(client_name) > 40:
            client_name = client_name[:40]
        client_name = f"{client_name}_{unique_suffix}"

        wg_client_id, config_text = await wg_client.create_and_get_config(client_name=client_name)
        config_text = str(config_text or "")

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
        logger.error("Ошибка при работе с WG-Easy: %s", _summarize_wg_error(exc), exc_info=True)

        hint = (
            "WG-Easy вернул ошибку. Возможные причины: неверный пароль для API (нужен WG_EASY_PASSWORD, не hash), "
            "исчерпан пул IP адресов (например /24 ≈ 253 клиента), либо проблема в данных WG-Easy (volume /etc/wireguard). "
            "Проверь логи контейнера wg_dashboard."
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=hint,
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

    result: list[PeerListItem] = []
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

    try:
        ok = await wg_client.delete(peer.wg_client_id)
        if not ok:
            logger.warning(
                "WG-Easy: не удалось удалить клиента (client_id=%s). Продолжаем деактивацию в БД.",
                peer.wg_client_id,
            )
    except Exception as exc:
        logger.warning("WG-Easy: ошибка при удалении клиента: %s", _summarize_wg_error(exc), exc_info=True)

    peer.is_active = False
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
