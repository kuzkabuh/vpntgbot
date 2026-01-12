"""Handlers for managing user devices (WireGuard peers)."""

from __future__ import annotations

import html
import logging
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery

from backend_client import call_backend, BackendError
from keyboards import main_menu_keyboard, devices_inline_keyboard

logger = logging.getLogger("vpn-bot.devices")


def register_handlers(dp: Dispatcher, bot: Bot) -> None:
    """Register handlers for listing and revoking devices."""

    @dp.message(F.text == "📱 Устройства")
    async def handle_devices(message: Message) -> None:
        """Show a list of the user's devices and provide revoke actions."""
        user = message.from_user
        if user is None:
            await message.answer(
                "Не удалось определить пользователя Telegram.",
                reply_markup=main_menu_keyboard(None),
            )
            return
        try:
            data = await call_backend(
                method="GET",
                path="/api/v1/vpn/peers/list",
                params={"telegram_id": user.id},
            )
        except BackendError as exc:
            await message.answer(html.escape(str(exc)), reply_markup=main_menu_keyboard(user.id))
            return
        except Exception:
            logger.exception("Unexpected error in devices list")
            await message.answer(
                "Ошибка при получении списка устройств. Попробуйте позже.",
                reply_markup=main_menu_keyboard(user.id),
            )
            return
        peers = data.get("peers") or []
        if not isinstance(peers, list):
            peers = []
        if not peers:
            await message.answer(
                "У вас пока нет устройств. Откройте «🔐 Конфиги WireGuard» и добавьте устройство.",
                reply_markup=main_menu_keyboard(user.id),
            )
            return
        lines: list[str] = ["<b>Ваши устройства:</b>", ""]
        for i, p in enumerate(peers, start=1):
            client_name = html.escape(str(p.get("client_name") or "device"))
            client_id = html.escape(str(p.get("client_id") or ""))
            location_code = html.escape(str(p.get("location_code") or ""))
            is_active_peer = bool(p.get("is_active", True))
            status_ico = "✅" if is_active_peer else "⛔"
            lines.append(
                f"{i}. {status_ico} <b>{client_name}</b> — <code>{client_id}</code> ({location_code})"
            )
        await message.answer("\n".join(lines), reply_markup=main_menu_keyboard(user.id))
        await message.answer("Отключение устройств:", reply_markup=devices_inline_keyboard(peers))

    @dp.callback_query(F.data == "devices:refresh")
    async def cb_refresh_devices(callback: CallbackQuery) -> None:
        """Refresh the device list and update the inline keyboard."""
        user = callback.from_user
        if user is None:
            await callback.answer("Не удалось определить пользователя.", show_alert=True)
            return
        try:
            data = await call_backend(
                method="GET",
                path="/api/v1/vpn/peers/list",
                params={"telegram_id": user.id},
            )
        except BackendError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        except Exception:
            logger.exception("Unexpected error in refresh devices")
            await callback.answer("Ошибка обновления списка.", show_alert=True)
            return
        peers = data.get("peers") or []
        if not isinstance(peers, list):
            peers = []
        try:
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=devices_inline_keyboard(peers))
        except Exception:
            pass
        await callback.answer("Список обновлён.")

    @dp.callback_query(F.data.startswith("revoke:"))
    async def cb_revoke_device(callback: CallbackQuery) -> None:
        """Revoke (disable) a device for the user."""
        user = callback.from_user
        if user is None:
            await callback.answer("Не удалось определить пользователя.", show_alert=True)
            return
        raw = callback.data or ""
        client_id = raw.split("revoke:", 1)[-1].strip()
        if not client_id:
            await callback.answer("Некорректный идентификатор устройства.", show_alert=True)
            return
        await callback.answer("Отключаем устройство...")
        try:
            _ = await call_backend(
                method="POST",
                path="/api/v1/vpn/peers/revoke",
                json={"telegram_id": user.id, "client_id": client_id},
            )
        except BackendError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        except Exception:
            logger.exception("Unexpected error in revoke device")
            await callback.answer("Ошибка отключения устройства.", show_alert=True)
            return
        # Refresh list
        try:
            data = await call_backend(
                method="GET",
                path="/api/v1/vpn/peers/list",
                params={"telegram_id": user.id},
            )
            peers = data.get("peers") or []
            if not isinstance(peers, list):
                peers = []
            if callback.message:
                await callback.message.edit_reply_markup(
                    reply_markup=devices_inline_keyboard(peers)
                )
        except Exception:
            pass
        await callback.answer("Устройство отключено.")
