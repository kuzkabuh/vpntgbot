"""
# ----------------------------------------------------------
# Версия файла: 1.1.0
# Описание: ORM-модели SQLAlchemy для VPN backend
#  - Location: локации (страны/регионы)
#  - Server: VPN-сервера (WireGuard-ноды)
#  - User: пользователи (Telegram)
#  - SubscriptionPlan: тарифные планы (триал, 1/2/3 месяца и т.п.)
#  - Subscription: подписки пользователей
# Дата изменения: 2025-12-29
# Изменения:
#  - 1.0.0: добавлены модели Location и Server
#  - 1.1.0: добавлены модели User, SubscriptionPlan, Subscription
# ----------------------------------------------------------
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    BigInteger,
    Numeric,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Базовый класс для всех моделей."""
    pass


# ======================
# ЛОКАЦИИ И СЕРВЕРА
# ======================


class Location(Base):
    """
    Модель локации (страна/регион), которую видит пользователь.
    Пример: eu-nl -> "Нидерланды".
    """

    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    servers: Mapped[List["Server"]] = relationship("Server", back_populates="location")

    def __repr__(self) -> str:
        return f"<Location code={self.code!r} name={self.name!r}>"


class Server(Base):
    """
    Модель VPN-сервера (WireGuard-нода).
    Привязан к локации и содержит техническую информацию.
    """

    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    location: Mapped[Location] = relationship("Location", back_populates="servers")

    public_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    wg_port: Mapped[int] = mapped_column(Integer, nullable=False)
    vpn_subnet: Mapped[str] = mapped_column(String(32), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    health_status: Mapped[str] = mapped_column(String(16), default="healthy", nullable=False)

    max_peers: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    current_peers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    subscriptions: Mapped[List["Subscription"]] = relationship("Subscription", back_populates="server")

    def __repr__(self) -> str:
        return f"<Server code={self.code!r} ip={self.public_ip!r} location_id={self.location_id}>"


# ======================
# ПОЛЬЗОВАТЕЛИ И ПОДПИСКИ
# ======================


class User(Base):
    """
    Пользователь (Telegram).
    Один пользователь может иметь несколько подписок (история), но активна обычно одна.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    language_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    subscriptions: Mapped[List["Subscription"]] = relationship("Subscription", back_populates="user")

    def __repr__(self) -> str:
        return f"<User tg_id={self.telegram_id} username={self.username!r}>"


class SubscriptionPlan(Base):
    """
    Тарифные планы:
      - триал (10 дней, бесплатно)
      - 1 месяц, 2 месяца, 3 месяца и т.д.
    Цена указываем в Telegram Stars (цена в рублях задаётся отдельно в Telegram).
    """

    __tablename__ = "subscription_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    # Длительность в днях (10, 30, 60, 90 и т.д.)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)

    # Цена в Telegram Stars (0 для триала)
    price_stars: Mapped[Numeric] = mapped_column(Numeric(10, 2), nullable=False)

    is_trial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Безлимитное количество устройств на 1 подписку
    max_devices: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    subscriptions: Mapped[List["Subscription"]] = relationship("Subscription", back_populates="plan")

    def __repr__(self) -> str:
        return f"<SubscriptionPlan code={self.code!r} price_stars={self.price_stars}>"


class Subscription(Base):
    """
    Подписка пользователя:
      - на какой план
      - на каком сервере (базовый или конкретная локация)
      - триал или платная
    """

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("subscription_plans.id"), nullable=False)
    server_id: Mapped[Optional[int]] = mapped_column(ForeignKey("servers.id"), nullable=True)

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Источник оплаты/создания: trial, stars, admin_free, donation и т.п.
    source: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    user: Mapped[User] = relationship("User", back_populates="subscriptions")
    plan: Mapped[SubscriptionPlan] = relationship("SubscriptionPlan", back_populates="subscriptions")
    server: Mapped[Optional[Server]] = relationship("Server", back_populates="subscriptions")

    def __repr__(self) -> str:
        return f"<Subscription user_id={self.user_id} plan_id={self.plan_id} active={self.is_active}>"
