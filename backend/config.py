"""
# ----------------------------------------------------------
# Версия файла: 1.1.0
# Описание: Конфигурация backend-приложения (загрузка ENV)
# Дата изменения: 2025-12-29
# Изменения:
#  - убраны небезопасные дефолты для секретов
#  - добавлены настройки WG-Easy
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
        self.db_dsn: str = os.getenv("BACKEND_DB_DSN", "")
        if not self.db_dsn:
            db_host = os.getenv("DB_HOST", "")
            db_port = os.getenv("DB_PORT", "")
            db_name = os.getenv("DB_NAME", "")
            db_user = os.getenv("DB_USER", "")
            db_password = os.getenv("DB_PASSWORD", "")
            missing = [
                key
                for key, value in [
                    ("DB_HOST", db_host),
                    ("DB_PORT", db_port),
                    ("DB_NAME", db_name),
                    ("DB_USER", db_user),
                    ("DB_PASSWORD", db_password),
                ]
                if not value
            ]
            if missing:
                raise RuntimeError(
                    "Не заданы обязательные переменные окружения для БД: "
                    + ", ".join(missing)
                )
            self.db_dsn = (
                f"postgresql+psycopg2://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
            )

        # Админский API-токен для внутренних bash-скриптов
        self.mgmt_api_token: str = os.getenv("MGMT_API_TOKEN", "")
        if not self.mgmt_api_token:
            raise RuntimeError("Не задан MGMT_API_TOKEN в окружении backend.")

        # Настройки для локаций по умолчанию
        self.default_location_code: str = os.getenv("WG_DEFAULT_LOCATION_CODE", "eu-nl")
        self.default_location_name: str = os.getenv("WG_DEFAULT_LOCATION_NAME", "Нидерланды")

        # WG-Easy
        self.wg_easy_url: str = os.getenv("WG_EASY_URL", "http://wg_dashboard:51821")
        self.wg_easy_password: str = os.getenv("WG_EASY_PASSWORD", "")
        if not self.wg_easy_password:
            raise RuntimeError("Не задан WG_EASY_PASSWORD в окружении backend.")


@lru_cache
def get_settings() -> Settings:
    return Settings()
