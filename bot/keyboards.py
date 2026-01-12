"""User interface keyboards for the VPN Telegram bot."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, List, Optional

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# Import settings and callback helpers using absolute imports because this module
# resides at the top level of the project rather than in a package.
from settings import MAX_CONFIGS_PER_USER, is_admin
from callback_tokens import register_client_id_for_callback

__all__ = [
    "main_menu_keyboard",
    "admin_payments_keyboard",
    "devices_inline_keyboard",
    "configs_inline_keyboard",
    "plans_pay_inline_keyboard",
]


def _is_unlimited() -> bool:
    return MAX_CONFIGS_PER_USER <= 0


def main_menu_keyboard(user_id: Optional[int] = None) -> ReplyKeyboardMarkup:
    """
    Build the main reply keyboard. Adds an admin menu for admin users.
    """
    keyboard: List[List[KeyboardButton]] = [
        [
            KeyboardButton(text="📊 Статус подписки"),
            KeyboardButton(text="🎁 Активировать триал"),
        ],
        [
            KeyboardButton(text="🔐 Конфиги WireGuard"),
            KeyboardButton(text="📱 Устройства"),
        ],
        [
            KeyboardButton(text="📖 Инструкция подключения"),
            KeyboardButton(text="⭐ Купить подписку"),
        ],
        [KeyboardButton(text="ℹ️ О проекте")],
    ]

    if user_id is not None and is_admin(user_id):
        keyboard.append([KeyboardButton(text="🛡 Админ: Платежи/подписки")])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def admin_payments_keyboard() -> ReplyKeyboardMarkup:
    """
    Build the admin reply keyboard for payments and subscription management.
    """
    keyboard: List[List[KeyboardButton]] = [
        [
            KeyboardButton(text="🧾 Планы (backend)"),
            KeyboardButton(text="🔎 Проверить подписку (TG ID)"),
        ],
        [
            KeyboardButton(text="✅ Подтвердить Stars оплату (payload)"),
            KeyboardButton(text="🕘 Последний платёж"),
        ],
        [KeyboardButton(text="⬅️ Назад в меню")],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def devices_inline_keyboard(peers: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """
    Build an inline keyboard for device revocation and refresh.
    Only active peers are listed for revocation.
    """
    rows: List[List[InlineKeyboardButton]] = []
    for p in peers:
        if not p.get("is_active", True):
            continue
        client_id = str(p.get("client_id", "")).strip()
        client_name = str(p.get("client_name", "")).strip() or "device"
        location_code = str(p.get("location_code", "")).strip()
        if not client_id:
            continue
        btn_text = f"🗑 Отключить: {client_name}"
        if location_code:
            btn_text += f" ({location_code})"
        rows.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"revoke:{client_id}")
        ])
    # Add refresh row
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить список", callback_data="devices:refresh")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def configs_inline_keyboard(peers: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """
    Build an inline keyboard for WireGuard config actions (add, refresh, download, QR, revoke).
    """
    rows: List[List[InlineKeyboardButton]] = []
    # Add first row: Add new config
    rows.append([
        InlineKeyboardButton(text="➕ Добавить устройство", callback_data="cfg:add")
    ])
    # Second row: Refresh
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить список", callback_data="cfg:refresh")
    ])

    for p in peers:
        client_id = str(p.get("client_id", "")).strip()
        client_name = str(p.get("client_name", "")).strip() or "device"
        location_code = str(p.get("location_code", "")).strip()
        is_active_peer = bool(p.get("is_active", True))
        if not client_id:
            continue
        token = await register_client_id_for_callback(client_id)
        title = client_name
        if location_code:
            title += f" ({location_code})"
        if not is_active_peer:
            title += " ⛔"
        # Download and QR buttons
        rows.append([
            InlineKeyboardButton(text=f"⬇️ .conf: {title}", callback_data=f"cfg:dl:{token}"),
            InlineKeyboardButton(text=f"📷 QR: {title}", callback_data=f"cfg:qr:{token}"),
        ])
        # Revoke button
        rows.append([
            InlineKeyboardButton(text=f"🗑 Удалить: {title}", callback_data=f"cfg:rv:{token}")
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plans_pay_inline_keyboard(plans: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """
    Build an inline keyboard for subscription plans with Stars payment.
    """
    rows: List[List[InlineKeyboardButton]] = []
    for p in plans:
        code = str(p.get("code") or "").strip()
        name = str(p.get("name") or "Тариф").strip()
        price_stars = p.get("price_stars")
        if not code:
            continue
        try:
            stars_amount = int(Decimal(str(price_stars)))
        except Exception:
            stars_amount = 0
        btn_text = f"⭐ {name} — {stars_amount} Stars"
        rows.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"pay:{code}")
        ])
    # Refresh row
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data="pay:refresh")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)