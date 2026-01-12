"""
# ----------------------------------------------------------
# Версия файла: 1.4.9
# Описание: ORM-модели SQLAlchemy для VPN backend
#  - Location: локации (страны/регионы)
#  - Server: VPN-сервера (WireGuard-ноды)
#  - User: пользователи (Telegram)
#  - SubscriptionPlan: тарифные планы
#  - Subscription: подписки пользователей
#  - VpnPeer: WireGuard-пиры (интеграция с WG-Easy)
#  - Payment: платежи (Telegram Stars)  <-- ДОБАВЛЕНО
# Дата изменения: 2026-01-12
#
# Изменения (1.4.9):
#  - Добавлена модель Payment для учёта оплат Telegram Stars
#  - Добавлены индексы и уникальность по telegram_payment_charge_id (идемпотентность)
#  - Убраны неиспользуемые импорты (List/Optional оставлены как используются)
#  - Бизнес-логика не менялась, только расширение схемы под платежи
# ----------------------------------------------------------
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
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

    servers: Mapped[List["Server"]] = relationship(
        "Server",
        back_populates="location",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

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

    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    location: Mapped[Location] = relationship("Location", back_populates="servers", lazy="selectin")

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

    subscriptions: Mapped[List["Subscription"]] = relationship(
        "Subscription",
        back_populates="server",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_servers_location_active", "location_id", "is_active"),
    )

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

    subscriptions: Mapped[List["Subscription"]] = relationship(
        "Subscription",
        back_populates="user",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    vpn_peers: Mapped[List["VpnPeer"]] = relationship(
        "VpnPeer",
        back_populates="user",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    # Платежи пользователя (история оплат)
    payments: Mapped[List["Payment"]] = relationship(
        "Payment",
        back_populates="user",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_users_is_admin", "is_admin"),
        Index("ix_users_is_blocked", "is_blocked"),
    )

    def __repr__(self) -> str:
        return f"<User tg_id={self.telegram_id} username={self.username!r}>"


class SubscriptionPlan(Base):
    """
    Тарифные планы:
      - триал (10 дней, бесплатно)
      - 1 месяц, 2 месяца, 3 месяца и т.д.
    Цена указываем в Telegram Stars.
    """

    __tablename__ = "subscription_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)

    price_stars: Mapped[Numeric] = mapped_column(Numeric(10, 2), nullable=False)

    is_trial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    max_devices: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    subscriptions: Mapped[List["Subscription"]] = relationship(
        "Subscription",
        back_populates="plan",
        lazy="selectin",
    )

    payments: Mapped[List["Payment"]] = relationship(
        "Payment",
        back_populates="plan",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_subscription_plans_active_sort", "is_active", "sort_order"),
    )

    def __repr__(self) -> str:
        return f"<SubscriptionPlan code={self.code!r} price_stars={self.price_stars}>"


class Subscription(Base):
    """
    Подписка пользователя:
      - план
      - сервер (опционально)
      - триал или платная
    """

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("subscription_plans.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    server_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("servers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    source: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    user: Mapped[User] = relationship("User", back_populates="subscriptions", lazy="selectin")
    plan: Mapped[SubscriptionPlan] = relationship("SubscriptionPlan", back_populates="subscriptions", lazy="selectin")
    server: Mapped[Optional[Server]] = relationship("Server", back_populates="subscriptions", lazy="selectin")

    payments: Mapped[List["Payment"]] = relationship(
        "Payment",
        back_populates="subscription",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_subscriptions_user_active", "user_id", "is_active"),
        Index("ix_subscriptions_active_ends", "is_active", "ends_at"),
    )

    def __repr__(self) -> str:
        return f"<Subscription user_id={self.user_id} plan_id={self.plan_id} active={self.is_active}>"


# ======================
# ПЛАТЕЖИ (TELEGRAM STARS)
# ======================


class Payment(Base):
    """
    Факт платежа (Telegram Stars).
    Идемпотентность: telegram_payment_charge_id уникален.
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    provider: Mapped[str] = mapped_column(String(32), default="telegram_stars", nullable=False)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    plan_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("subscription_plans.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    subscription_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Numeric] = mapped_column(Numeric(10, 2), nullable=False)

    invoice_payload: Mapped[str] = mapped_column(String(255), nullable=False)

    telegram_payment_charge_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    provider_payment_charge_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="paid", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship("User", back_populates="payments", lazy="selectin")
    plan: Mapped[Optional[SubscriptionPlan]] = relationship("SubscriptionPlan", back_populates="payments", lazy="selectin")
    subscription: Mapped[Optional[Subscription]] = relationship("Subscription", back_populates="payments", lazy="selectin")

    __table_args__ = (
        Index("ix_payments_created_at", "created_at"),
        Index("ix_payments_status_created", "status", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<Payment id={self.id} tg_id={self.telegram_id} amount={self.amount} {self.currency}>"


# ======================
# WIREGUARD PEERS
# ======================


class VpnPeer(Base):
    """
    WireGuard-пир, созданный через WG-Easy.
    Привязан к пользователю и локации.
    """

    __tablename__ = "vpn_peers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    wg_client_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    client_name: Mapped[str] = mapped_column(String(255), nullable=False)

    location_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    location_name: Mapped[str] = mapped_column(String(255), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship("User", back_populates="vpn_peers", lazy="selectin")

    __table_args__ = (
        Index("ix_vpn_peers_user_active", "user_id", "is_active"),
        Index("ix_vpn_peers_user_location_active", "user_id", "location_code", "is_active"),
        UniqueConstraint("user_id", "wg_client_id", name="uq_vpn_peers_user_client"),
    )

    def __repr__(self) -> str:
        return f"<VpnPeer user_id={self.user_id} wg_client_id={self.wg_client_id!r}>"
