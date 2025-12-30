"""
# ----------------------------------------------------------
# Версия файла: 1.4.1
# Описание: Pydantic-схемы для API backend
#  - ServerCreate, LocationOut, ServerOut
#  - TelegramUserIn, UserOut, SubscriptionPlanOut
#  - UserFromTelegramResponse
#  - TrialGrantResponse
#  - SubscriptionStatusResponse
#  - Peers: PeerList/PeerRevoke
#  - Admin: SubscriptionPlanCreate/Update
# Дата изменения: 2025-12-30
#
# Изменения (1.4.1):
#  - Исправлен обрыв файла (закрыты классы и добавлены недостающие окончания)
#  - Сохранена совместимость с Pydantic v2 (ConfigDict/from_attributes)
#  - Уточнены типы и ограничения Field для критичных полей
# ----------------------------------------------------------
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

# ======================
# СЕРВЕРА И ЛОКАЦИИ
# ======================


class ServerCreate(BaseModel):
    """
    Входная схема для регистрации/обновления сервера через /admin/servers.
    Используется bash-скриптом add_vpn_server.sh (позже).
    """

    code: str = Field(..., min_length=2, max_length=64, description="Уникальный код сервера, например 'eu-nl-1'")
    location_code: str = Field(..., min_length=2, max_length=32, description="Код локации, например 'eu-nl'")
    location_name: str = Field(..., min_length=2, max_length=128, description="Название локации, например 'Нидерланды'")
    public_ip: str = Field(..., min_length=3, max_length=64, description="Публичный IP-адрес сервера")
    wg_port: int = Field(..., ge=1, le=65535, description="Порт WireGuard (UDP)")
    vpn_subnet: str = Field(..., min_length=3, max_length=32, description="VPN-сеть, например '10.8.0.1/24'")
    max_peers: Optional[int] = Field(None, ge=1, description="Лимит пиров на ноде (опционально)")


class LocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    is_default: bool
    is_public: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class ServerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    public_ip: str
    wg_port: int
    vpn_subnet: str
    is_active: bool
    health_status: str
    max_peers: Optional[int]
    current_peers: int
    location: LocationOut
    created_at: datetime
    updated_at: datetime


# ======================
# ПОЛЬЗОВАТЕЛИ И ТАРИФЫ
# ======================


class TelegramUserIn(BaseModel):
    """
    Данные, которые бот отправляет в backend при первом /start.
    """

    telegram_id: int = Field(..., ge=1, description="Telegram ID пользователя")
    username: Optional[str] = Field(None, max_length=64, description="username без @")
    first_name: Optional[str] = Field(None, max_length=128, description="Имя")
    last_name: Optional[str] = Field(None, max_length=128, description="Фамилия")
    language_code: Optional[str] = Field(None, max_length=8, description="Код языка Telegram (ru, en и т.п.)")


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    language_code: Optional[str] = None
    is_blocked: bool
    is_admin: bool
    created_at: datetime
    updated_at: datetime


class SubscriptionPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    duration_days: int
    price_stars: float
    is_trial: bool
    is_active: bool
    sort_order: int
    max_devices: Optional[int]
    created_at: datetime
    updated_at: datetime


class UserFromTelegramResponse(BaseModel):
    """
    Ответ backend на регистрацию/обновление пользователя.
    Используется ботом, чтобы понимать, новый клиент или нет,
    и есть ли у него активная подписка.
    """

    user: UserOut
    is_new: bool
    has_active_subscription: bool
    active_until: Optional[datetime] = None
    has_had_trial: bool
    is_trial_active: bool
    active_plan_name: Optional[str] = None
    subscription_ends_at: Optional[datetime] = None
    trial_available: bool = False


class SubscriptionStatusResponse(BaseModel):
    """
    Ответ на запрос статуса подписки пользователя.
    """

    has_active_subscription: bool
    is_trial_active: bool
    active_plan_name: Optional[str] = None
    subscription_ends_at: Optional[datetime] = None
    trial_available: bool = False


class TrialGrantResponse(BaseModel):
    """
    Ответ при попытке выдать пользователю бесплатный пробный период.
    """

    success: bool = Field(..., description="Флаг успешной выдачи триала")
    message: str = Field(..., description="Человекочитаемое сообщение о результате операции")
    trial_ends_at: Optional[datetime] = Field(
        None,
        description="Дата и время окончания триала (UTC), если успешно выдан",
    )
    user: UserOut
    plan: Optional[SubscriptionPlanOut] = None
    already_had_trial: bool = False


# ======================
# PEERS (для меню бота)
# ======================


class PeerListItem(BaseModel):
    client_id: str = Field(..., description="WG-Easy client_id")
    client_name: str = Field(..., description="Имя устройства/клиента")
    location_code: str = Field(..., description="Код локации")
    location_name: str = Field(..., description="Название локации")
    is_active: bool = Field(..., description="Активен ли peer")
    created_at: Optional[datetime] = Field(None, description="Дата создания (UTC)")
    revoked_at: Optional[datetime] = Field(None, description="Дата деактивации (UTC)")


class PeerListResponse(BaseModel):
    telegram_id: int = Field(..., description="Telegram ID пользователя")
    peers: list[PeerListItem]


class PeerRevokeRequest(BaseModel):
    telegram_id: int = Field(..., ge=1, description="Telegram ID пользователя")
    client_id: str = Field(..., min_length=1, max_length=128, description="WG-Easy client_id, который нужно отозвать")
    location_code: Optional[str] = Field(None, max_length=32, description="Опционально: локация для уточнения")


# ======================
# ADMIN (минимум для бота/панели)
# ======================


class SubscriptionPlanCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=32, description="Уникальный код тарифа (например, month_1)")
    name: str = Field(..., min_length=2, max_length=128, description="Название тарифа")
    duration_days: int = Field(..., ge=1, le=3650, description="Длительность в днях")
    price_stars: float = Field(..., ge=0, description="Стоимость в Telegram Stars")
    is_trial: bool = Field(False, description="Пробный тариф")
    is_active: bool = Field(True, description="Активен/неактивен")
    sort_order: int = Field(0, description="Порядок сортировки")
    max_devices: Optional[int] = Field(None, ge=1, description="Лимит устройств (peers); null = безлимит")


class SubscriptionPlanUpdate(BaseModel):
    """
    PATCH/UPDATE схема для тарифа (если добавите эндпоинт обновления).
    Важно: max_devices=None должно означать 'безлимит', поэтому поле Optional[int].
    """

    name: Optional[str] = Field(None, min_length=2, max_length=128, description="Название тарифа")
    duration_days: Optional[int] = Field(None, ge=1, le=3650, description="Длительность в днях")
    price_stars: Optional[float] = Field(None, ge=0, description="Стоимость в Telegram Stars")
    is_trial: Optional[bool] = Field(None, description="Пробный тариф")
    is_active: Optional[bool] = Field(None, description="Активен/неактивен")
    sort_order: Optional[int] = Field(None, description="Порядок сортировки")
    max_devices: Optional[int] = Field(None, ge=1, description="Лимит устройств (peers); null = безлимит")
