"""
# ----------------------------------------------------------
# Версия файла: 1.1.0
# Описание: Первичная схема БД (locations, servers, users, plans, subscriptions, vpn_peers)
# Дата изменения: 2025-12-29
#
# Изменения (1.1.0):
#  - Миграция сделана идемпотентной: не падает, если таблицы/индексы уже существуют
#  - Добавлена проверка существования таблиц/индексов через SQLAlchemy Inspector
#  - Это позволяет "подхватить" уже созданные ранее таблицы (users, vpn_peers) и доразвернуть схему до ТЗ
# ----------------------------------------------------------
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision = "20251229_001"
down_revision = None
branch_labels = None
depends_on = None


# ----------------------------------------------------------
# Helpers
# ----------------------------------------------------------


def _table_exists(bind, table_name: str, schema: str = "public") -> bool:
    insp = inspect(bind)
    return insp.has_table(table_name, schema=schema)


def _index_exists(bind, table_name: str, index_name: str, schema: str = "public") -> bool:
    insp = inspect(bind)
    for idx in insp.get_indexes(table_name, schema=schema):
        if idx.get("name") == index_name:
            return True
    return False


# ----------------------------------------------------------
# Upgrade / Downgrade
# ----------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()

    # ----------------------
    # locations
    # ----------------------
    if not _table_exists(bind, "locations"):
        op.create_table(
            "locations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column("code", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        )

    if not _index_exists(bind, "locations", "ix_locations_code"):
        op.create_index("ix_locations_code", "locations", ["code"], unique=True)

    # ----------------------
    # servers
    # ----------------------
    if not _table_exists(bind, "servers"):
        op.create_table(
            "servers",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column("code", sa.String(length=64), nullable=False),
            sa.Column("location_id", sa.Integer(), nullable=False),
            sa.Column("public_ip", sa.String(length=64), nullable=False),
            sa.Column("wg_port", sa.Integer(), nullable=False),
            sa.Column("vpn_subnet", sa.String(length=32), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column(
                "health_status",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'healthy'"),
            ),
            sa.Column("max_peers", sa.Integer(), nullable=True),
            sa.Column("current_peers", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["location_id"], ["locations.id"]),
        )

    if not _index_exists(bind, "servers", "ix_servers_code"):
        op.create_index("ix_servers_code", "servers", ["code"], unique=True)

    # ----------------------
    # users
    # ----------------------
    if not _table_exists(bind, "users"):
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column("telegram_id", sa.BigInteger(), nullable=False),
            sa.Column("username", sa.String(length=64), nullable=True),
            sa.Column("first_name", sa.String(length=128), nullable=True),
            sa.Column("last_name", sa.String(length=128), nullable=True),
            sa.Column("language_code", sa.String(length=8), nullable=True),
            sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        )

    if not _index_exists(bind, "users", "ix_users_telegram_id"):
        op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)

    # ----------------------
    # subscription_plans
    # ----------------------
    if not _table_exists(bind, "subscription_plans"):
        op.create_table(
            "subscription_plans",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column("code", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("duration_days", sa.Integer(), nullable=False),
            sa.Column("price_stars", sa.Numeric(10, 2), nullable=False),
            sa.Column("is_trial", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("max_devices", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        )

    if not _index_exists(bind, "subscription_plans", "ix_subscription_plans_code"):
        op.create_index("ix_subscription_plans_code", "subscription_plans", ["code"], unique=True)

    # ----------------------
    # subscriptions
    # ----------------------
    if not _table_exists(bind, "subscriptions"):
        op.create_table(
            "subscriptions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("plan_id", sa.Integer(), nullable=False),
            sa.Column("server_id", sa.Integer(), nullable=True),
            sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("is_trial", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("source", sa.String(length=32), nullable=False, server_default=sa.text("'unknown'")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["plan_id"], ["subscription_plans.id"]),
            sa.ForeignKeyConstraint(["server_id"], ["servers.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        )

    if not _index_exists(bind, "subscriptions", "ix_subscriptions_user_id"):
        op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"], unique=False)

    # ----------------------
    # vpn_peers
    # ----------------------
    if not _table_exists(bind, "vpn_peers"):
        op.create_table(
            "vpn_peers",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("wg_client_id", sa.String(length=128), nullable=False),
            sa.Column("client_name", sa.String(length=255), nullable=False),
            sa.Column("location_code", sa.String(length=32), nullable=False),
            sa.Column("location_name", sa.String(length=255), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        )

    if not _index_exists(bind, "vpn_peers", "ix_vpn_peers_wg_client_id"):
        op.create_index("ix_vpn_peers_wg_client_id", "vpn_peers", ["wg_client_id"], unique=True)


def downgrade() -> None:
    """
    ВНИМАНИЕ:
    Для реального production downgrade этой миграции обычно не используют.
    Но оставляем корректный откат, который удаляет таблицы в обратном порядке.

    Важно: если БД была "грязной" до применения миграции (часть таблиц уже существовала),
    откат может удалить существующие таблицы. Используй downgrade только на тестовой среде.
    """
    bind = op.get_bind()

    # vpn_peers
    if _table_exists(bind, "vpn_peers"):
        if _index_exists(bind, "vpn_peers", "ix_vpn_peers_wg_client_id"):
            op.drop_index("ix_vpn_peers_wg_client_id", table_name="vpn_peers")
        op.drop_table("vpn_peers")

    # subscriptions
    if _table_exists(bind, "subscriptions"):
        if _index_exists(bind, "subscriptions", "ix_subscriptions_user_id"):
            op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
        op.drop_table("subscriptions")

    # subscription_plans
    if _table_exists(bind, "subscription_plans"):
        if _index_exists(bind, "subscription_plans", "ix_subscription_plans_code"):
            op.drop_index("ix_subscription_plans_code", table_name="subscription_plans")
        op.drop_table("subscription_plans")

    # users
    if _table_exists(bind, "users"):
        if _index_exists(bind, "users", "ix_users_telegram_id"):
            op.drop_index("ix_users_telegram_id", table_name="users")
        op.drop_table("users")

    # servers
    if _table_exists(bind, "servers"):
        if _index_exists(bind, "servers", "ix_servers_code"):
            op.drop_index("ix_servers_code", table_name="servers")
        op.drop_table("servers")

    # locations
    if _table_exists(bind, "locations"):
        if _index_exists(bind, "locations", "ix_locations_code"):
            op.drop_index("ix_locations_code", table_name="locations")
        op.drop_table("locations")
