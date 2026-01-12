"""Configuration and constants for the VPN Telegram bot.

This module reads environment variables and exposes configuration constants.
It also provides helper functions such as checking whether a user is an admin.
"""

from __future__ import annotations

import os
from typing import Set

__all__ = [
    "TELEGRAM_BOT_TOKEN",
    "BACKEND_BASE_URL",
    "BACKEND_TIMEOUT",
    "BACKEND_CONNECT_TIMEOUT",
    "MAX_CONFIGS_PER_USER",
    "CALLBACK_TOKEN_TTL_SEC",
    "STARS_ENABLED",
    "STARS_CURRENCY",
    "STARS_PROVIDER_TOKEN",
    "STARS_PAYLOAD_PREFIX",
    "STARS_START_PARAMETER_PREFIX",
    "ADMIN_TELEGRAM_IDS",
    "is_admin",
]

# Telegram bot token
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Backend service address (Docker service name `backend` by default)
BACKEND_BASE_URL: str = os.getenv("BACKEND_BASE_URL", "http://backend:8000").strip()

# HTTP timeouts
BACKEND_TIMEOUT: float = float(os.getenv("BACKEND_TIMEOUT", "12.0"))
BACKEND_CONNECT_TIMEOUT: float = float(os.getenv("BACKEND_CONNECT_TIMEOUT", "3.5"))

# Limit of WireGuard configs per user (0 or negative means unlimited)
MAX_CONFIGS_PER_USER: int = int(os.getenv("MAX_CONFIGS_PER_USER", "0"))

# TTL for callback tokens (seconds)
CALLBACK_TOKEN_TTL_SEC: int = int(os.getenv("CALLBACK_TOKEN_TTL_SEC", "3600"))

# Telegram Stars payment parameters
STARS_ENABLED: bool = os.getenv("STARS_ENABLED", "1").strip() == "1"
STARS_CURRENCY: str = "XTR"
STARS_PROVIDER_TOKEN: str = ""  # Provider token for Stars is left empty
STARS_PAYLOAD_PREFIX: str = "vpn_plan:"
STARS_START_PARAMETER_PREFIX: str = "vpn_plan"

# Admins Telegram IDs (comma separated)
ADMIN_TELEGRAM_IDS_RAW: str = (os.getenv("ADMIN_TELEGRAM_IDS") or "").strip()
ADMIN_TELEGRAM_IDS: Set[int] = set()
if ADMIN_TELEGRAM_IDS_RAW:
    for part in ADMIN_TELEGRAM_IDS_RAW.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            ADMIN_TELEGRAM_IDS.add(int(p))
        except Exception:
            continue


def is_admin(user_id: int) -> bool:
    """Return True if the given Telegram user ID is in the admin list."""
    try:
        return int(user_id) in ADMIN_TELEGRAM_IDS
    except Exception:
        return False