"""
# ----------------------------------------------------------
# Версия файла: 1.2.1
# Описание: Конфигурация backend-приложения (загрузка ENV)
# Дата изменения: 2025-12-30
#
# Изменения (1.2.1):
#  - Добавлена переменная WG_EASY_USERNAME (логин для WG-Easy API, по умолчанию 'admin')
#  - Валидация: username обязателен (не пустой)
# ----------------------------------------------------------
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional


def _getenv(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _getenv_bool(name: str, default: bool = False) -> bool:
    v = _getenv(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "y", "on")


def _getenv_int(name: str, default: Optional[int] = None) -> Optional[int]:
    v = _getenv(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError as exc:
        raise RuntimeError(f"Некорректное значение {name}: ожидается int, получено {v!r}") from exc


def _split_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    parts = [p.strip() for p in value.split(",")]
    return [p for p in parts if p]


def _require(name: str) -> str:
    v = _getenv(name)
    if not v:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return v


@dataclass(frozen=True)
class Settings:
    # -----------------------
    # Общие настройки
    # -----------------------
    app_env: str
    app_debug: bool
    app_host: str
    app_port: int

    # -----------------------
    # База данных
    # -----------------------
    db_dsn: str

    # -----------------------
    # Админский токен (внутренний API)
    # -----------------------
    mgmt_api_token: str

    # -----------------------
    # Локации по умолчанию
    # -----------------------
    default_location_code: str
    default_location_name: str

    # -----------------------
    # WG-Easy
    # -----------------------
    wg_easy_url: str
    wg_easy_username: str
    wg_easy_password: str  # password/plain OR password_hash (в зависимости от вашей обвязки)

    # -----------------------
    # Security / будущие настройки (задел под ТЗ)
    # -----------------------
    cors_origins: List[str]
    admin_telegram_ids: List[int]
    rate_limit_per_minute: int

    @staticmethod
    def load() -> "Settings":
        # Общие
        app_env = _getenv("APP_ENV", "development") or "development"
        app_debug = _getenv_bool("APP_DEBUG", False)
        app_host = _getenv("APP_HOST", "0.0.0.0") or "0.0.0.0"
        app_port = _getenv_int("APP_PORT", 8000) or 8000

        # DB: приоритет BACKEND_DB_DSN, иначе собираем по кускам
        db_dsn = _getenv("BACKEND_DB_DSN")
        if not db_dsn:
            db_host = _require("DB_HOST")
            db_port = _require("DB_PORT")
            db_name = _require("DB_NAME")
            db_user = _require("DB_USER")
            db_password = _require("DB_PASSWORD")
            db_dsn = f"postgresql+psycopg2://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"

        # MGMT токен — обязателен
        mgmt_api_token = _require("MGMT_API_TOKEN")

        # Локация по умолчанию
        default_location_code = _getenv("WG_DEFAULT_LOCATION_CODE", "eu-nl") or "eu-nl"
        default_location_name = _getenv("WG_DEFAULT_LOCATION_NAME", "Нидерланды") or "Нидерланды"

        # WG-Easy URL: допустим дефолт внутри docker-сети
        wg_easy_url = _getenv("WG_EASY_URL", "http://wg_dashboard:51821") or "http://wg_dashboard:51821"

        # WG-Easy username (ВАЖНО: у тебя 'artem', а не 'admin')
        wg_easy_username = _getenv("WG_EASY_USERNAME", "artem") or "admin"
        if not wg_easy_username.strip():
            raise RuntimeError("WG_EASY_USERNAME не должен быть пустым.")

        # Предпочтительно: WG_EASY_PASSWORD_HASH (если вы используете HASH)
        # Fallback: WG_EASY_PASSWORD (legacy)
        wg_easy_password_hash = _getenv("WG_EASY_PASSWORD_HASH")
        wg_easy_password_plain = _getenv("WG_EASY_PASSWORD")

        wg_easy_password = wg_easy_password_hash or wg_easy_password_plain or ""
        if not wg_easy_password:
            raise RuntimeError(
                "Не задан WG_EASY_PASSWORD_HASH или WG_EASY_PASSWORD в окружении backend."
            )

        # Security placeholders (по ТЗ будут расширяться)
        cors_origins = _split_csv(_getenv("CORS_ORIGINS", "*")) or ["*"]
        admin_ids_raw = _split_csv(_getenv("ADMIN_TELEGRAM_IDS", ""))
        admin_telegram_ids: List[int] = []
        for item in admin_ids_raw:
            try:
                admin_telegram_ids.append(int(item))
            except ValueError:
                raise RuntimeError(
                    f"Некорректное значение ADMIN_TELEGRAM_IDS: {item!r} (ожидаются числа через запятую)"
                )

        rate_limit_per_minute = _getenv_int("RATE_LIMIT_PER_MINUTE", 60) or 60
        if rate_limit_per_minute <= 0:
            raise RuntimeError("RATE_LIMIT_PER_MINUTE должен быть > 0")

        return Settings(
            app_env=app_env,
            app_debug=app_debug,
            app_host=app_host,
            app_port=app_port,
            db_dsn=db_dsn,
            mgmt_api_token=mgmt_api_token,
            default_location_code=default_location_code,
            default_location_name=default_location_name,
            wg_easy_url=wg_easy_url,
            wg_easy_username=wg_easy_username,
            wg_easy_password=wg_easy_password,
            cors_origins=cors_origins,
            admin_telegram_ids=admin_telegram_ids,
            rate_limit_per_minute=rate_limit_per_minute,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.load()
