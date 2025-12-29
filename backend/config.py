"""
# ----------------------------------------------------------
# Версия файла: 1.0.0
# Описание: Конфигурация backend-приложения (загрузка ENV)
# Дата изменения: 2025-12-29
# Изменения:
#  - базовый класс Settings и кэшированный get_settings
# ----------------------------------------------------------
"""

from __future__ import annotations

import os
from functools import lru_cache


class Settings:
    def __init__(self) -> None:
        # Общие настройки
        self.app_env: str = os.getenv("APP_ENV", "development")
        self.app_debug: bool = os.getenv("APP_DEBUG", "false").lower() == "true"
        self.app_host: str = os.getenv("APP_HOST", "0.0.0.0")
        self.app_port: int = int(os.getenv("APP_PORT", "8000"))

        # База данных
        self.db_dsn: str = os.getenv(
            "BACKEND_DB_DSN",
            "postgresql+psycopg2://vpn_user:change_me_strong_password@db:5432/vpn_service",
        )

        # Админский API-токен для внутренних bash-скриптов
        self.mgmt_api_token: str = os.getenv("MGMT_API_TOKEN", "change_me_management_api_token")

        # Настройки для локаций по умолчанию
        self.default_location_code: str = os.getenv("WG_DEFAULT_LOCATION_CODE", "eu-nl")
        self.default_location_name: str = os.getenv("WG_DEFAULT_LOCATION_NAME", "Нидерланды")


@lru_cache
def get_settings() -> Settings:
    return Settings()
