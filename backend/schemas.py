"""
# ----------------------------------------------------------
# Версия файла: 1.2.0
# Описание: Pydantic-схемы для API backend
#  - ServerCreate, LocationOut, ServerOut
#  - TelegramUserIn, UserOut, SubscriptionPlanOut
#  - UserFromTelegramResponse
#  - TrialGrantResponse
# Дата изменения: 2025-12-29
# Изменения:
#  - 1.0.0: схемы для ServerCreate, LocationOut, ServerOut
#  - 1.1.0: добавлены схемы для пользователей и тарифов
#  - 1.2.0: добавлена схема TrialGrantResponse для выдачи пробного периода
# ----------------------------------------------------------
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ======================
# СЕРВЕРА И ЛОКАЦИИ
# ======================


class ServerCreate(BaseModel):
    """
    Входная схема для регистрации/обновления сервера через /admin/servers.
    Используется bash-скриптом add_vpn_server.sh (позже).
    """

    code: str = Field(..., description="Уникальный код сервера, например 'eu-nl-1'")
    location_code: str = Field(..., description="Код локации, например 'eu-nl'")
    location_name: str = Field(..., description="Название локации, например 'Нидерланды'")
    public_ip: str = Field(..., description="Публичный IP-адрес сервера")
    wg_port: int = Field(..., description="Порт WireGuard (UDP)")
    vpn_subnet: str = Field(..., description="Внутренний VPN-адрес и маска, например '10.8.0.1/24'")
    max_peers: Optional[int] = Field(None, description="Желаемый лимит пиров на ноде (опционально)")


class LocationOut(BaseModel):
    id: int
    code: str
    name: str
    is_default: bool
    is_public: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ServerOut(BaseModel):
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

    class Config:
        from_attributes = True


# ======================
# ПОЛЬЗОВАТЕЛИ И ТАРИФЫ
# ======================


class TelegramUserIn(BaseModel):
    """
    Данные, которые бот отправляет в backend при первом /start.
    """

    telegram_id: int = Field(..., description="Telegram ID пользователя")
    username: Optional[str] = Field(None, description="username без @")
    first_name: Optional[str] = Field(None, description="Имя")
    last_name: Optional[str] = Field(None, description="Фамилия")
    language_code: Optional[str] = Field(None, description="Код языка Telegram (ru, en и т.п.)")


class UserOut(BaseModel):
    id: int
    telegram_id: int
    username: Optional[str]
    first_name: Optional[str]
    last_name: Optional[str]
    language_code: Optional[str]
    is_blocked: bool
    is_admin: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SubscriptionPlanOut(BaseModel):
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

    class Config:
        from_attributes = True


class UserFromTelegramResponse(BaseModel):
    """
    Ответ backend на регистрацию/обновление пользователя.
    Используется ботом, чтобы понимать, новый клиент или нет,
    и есть ли у него активная подписка.
    """

    user: UserOut
    is_new: bool
    has_active_subscription: bool
    active_until: Optional[datetime]
    has_had_trial: bool


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
