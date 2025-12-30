"""
# ----------------------------------------------------------
# Версия файла: 1.1.0
# Описание: Alembic окружение для миграций backend
# Дата изменения: 2025-12-29
#
# Изменения (1.1.0):
#  - Надёжное определение BASE_DIR и подключение проекта к sys.path
#  - URL БД берется из Settings.db_dsn (единый источник конфигурации)
#  - Включены compare_type и compare_server_default для точных миграций
#  - Добавлены include_object и process_revision_directives:
#      * не генерировать "пустые" миграции
#      * мигрировать только таблицы проекта
# ----------------------------------------------------------
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool

# ----------------------------------------------------------
# Подключаем корень backend-проекта в sys.path,
# чтобы корректно импортировать config/models при запуске из контейнера.
# ----------------------------------------------------------

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.abspath(os.path.join(THIS_DIR, ".."))

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config import get_settings  # noqa: E402
from models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()

# Единый источник DSN для миграций и runtime
config.set_main_option("sqlalchemy.url", settings.db_dsn)

target_metadata = Base.metadata

# Таблицы проекта (ограничение на случай сторонних схем/расширений)
PROJECT_TABLES = {
    "locations",
    "servers",
    "users",
    "subscription_plans",
    "subscriptions",
    "vpn_peers",
}


def include_object(
    object_: Any,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: Any,
) -> bool:
    """
    Ограничиваем миграции таблицами проекта.
    """
    if type_ == "table":
        # alembic version table всегда разрешаем
        if name == "alembic_version":
            return True
        return bool(name) and name in PROJECT_TABLES
    return True


def process_revision_directives(context_, revision, directives):
    """
    Не создаём пустые миграции при autogenerate.
    """
    if getattr(config.cmd_opts, "autogenerate", False):
        script = directives[0]
        if script.upgrade_ops is None or script.upgrade_ops.is_empty():
            directives[:] = []


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
        process_revision_directives=process_revision_directives,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            compare_server_default=True,
            process_revision_directives=process_revision_directives,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
