"""Handlers for WireGuard configuration management.

This module registers handlers responsible for listing, adding,
downloading, showing QR codes and revoking WireGuard peer configs.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, BufferedInputFile

from backend_client import call_backend, BackendError
from callback_tokens import resolve_client_id_from_callback
from keyboards import main_menu_keyboard, configs_inline_keyboard
from settings import MAX_CONFIGS_PER_USER
from utils import safe_filename, build_qr_png_bytes

logger = logging.getLogger("vpn-bot.configs")


def register_handlers(dp: Dispatcher, bot: Bot) -> None:
    """Register handlers for WireGuard configuration management."""

    @dp.message(F.text == "🔐 Конфиги WireGuard")
    async def handle_configs(message: Message) -> None:
        """List existing WireGuard configs and provide config actions."""
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
            logger.exception("Unexpected error in configs list")
            await message.answer(
                "Ошибка при получении списка конфигов. Попробуйте позже.",
                reply_markup=main_menu_keyboard(user.id),
            )
            return
        peers = data.get("peers") or []
        if not isinstance(peers, list):
            peers = []
        used = len(peers)
        if MAX_CONFIGS_PER_USER <= 0:
            limit_line = "Лимит устройств: <b>без ограничений</b>"
        else:
            limit_line = f"Лимит устройств: <b>{used}/{MAX_CONFIGS_PER_USER}</b>"
        lines = [
            "<b>Конфиги WireGuard</b>",
            "",
            limit_line,
            "",
            "Действия:",
            "• ⬇️ <b>.conf</b> — скачать конфигурацию файлом",
            "• 📷 <b>QR</b> — отсканировать в WireGuard",
            "• 🗑 <b>Удалить</b> — отключить устройство",
            "• ➕ <b>Добавить</b> — создать новое устройство",
            "",
            "Если вы не знаете как подключить — откройте «📖 Инструкция подключения».",
        ]
        await message.answer("\n".join(lines), reply_markup=main_menu_keyboard(user.id))
        # Build keyboard separately to avoid awaiting inside kwargs
        configs_kb = await configs_inline_keyboard(peers)
        await message.answer("Управление конфигами:", reply_markup=configs_kb)

    @dp.callback_query(F.data == "cfg:refresh")
    async def cb_configs_refresh(callback: CallbackQuery) -> None:
        """Refresh the list of configs and update the inline keyboard."""
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
            logger.exception("Unexpected error in cfg refresh")
            await callback.answer("Ошибка обновления списка.", show_alert=True)
            return
        peers = data.get("peers") or []
        if not isinstance(peers, list):
            peers = []
        try:
            configs_kb = await configs_inline_keyboard(peers)
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=configs_kb)
        except Exception:
            pass
        await callback.answer("Список обновлён.")

    @dp.callback_query(F.data == "cfg:add")
    async def cb_configs_add(callback: CallbackQuery) -> None:
        """Create a new WireGuard peer and send config and QR to the user."""
        user = callback.from_user
        if user is None:
            await callback.answer("Не удалось определить пользователя.", show_alert=True)
            return
        # Pre-check current count
        try:
            data = await call_backend(
                method="GET",
                path="/api/v1/vpn/peers/list",
                params={"telegram_id": user.id},
            )
            peers = data.get("peers") or []
            if not isinstance(peers, list):
                peers = []
        except BackendError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        except Exception:
            logger.exception("Unexpected error in cfg add precheck")
            await callback.answer("Ошибка. Попробуйте позже.", show_alert=True)
            return
        if MAX_CONFIGS_PER_USER > 0 and len(peers) >= MAX_CONFIGS_PER_USER:
            await callback.answer(
                f"Лимит устройств: {MAX_CONFIGS_PER_USER}. Удалите старое устройство.",
                show_alert=True,
            )
            return
        await callback.answer("Создаём новое устройство...")
        safe_first = (user.first_name or "device").strip()
        device_name = f"{safe_first}_{user.id}_{len(peers) + 1}"
        try:
            created = await call_backend(
                method="POST",
                path="/api/v1/vpn/peers/create",
                json={
                    "telegram_id": user.id,
                    "telegram_username": user.username,
                    "device_name": device_name,
                },
            )
        except BackendError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        except Exception:
            logger.exception("Unexpected error in cfg add/create")
            await callback.answer("Ошибка создания устройства.", show_alert=True)
            return
        config_text = created.get("config")
        client_name = created.get("client_name") or device_name
        location_code = created.get("location_code") or ""
        location_name = created.get("location_name") or ""
        if not config_text or not str(config_text).strip():
            await callback.answer("Создано, но конфиг не получен (ошибка backend).", show_alert=True)
            return
        filename = safe_filename(f"wg_{user.id}_{client_name}.conf", default=f"wg_{user.id}.conf")
        conf_bytes = str(config_text).encode("utf-8", errors="replace")
        conf_file = BufferedInputFile(conf_bytes, filename=filename)
        qr_png = build_qr_png_bytes(str(config_text))
        qr_file = BufferedInputFile(qr_png, filename="wireguard_qr.png")
        meta_lines = [
            "<b>Новое устройство создано.</b>",
            f"Устройство: <b>{html.escape(str(client_name))}</b>",
        ]
        if location_code or location_name:
            meta_lines.append(
                f"Локация: <code>{html.escape(str(location_code))}</code> {html.escape(str(location_name))}".strip()
            )
        meta_lines.append("")
        meta_lines.append("Далее:")
        meta_lines.append("• импортируйте <b>.conf</b> или")
        meta_lines.append("• откройте WireGuard → <b>+</b> → <b>Сканировать QR</b>.")
        try:
            if callback.message:
                await callback.message.answer(
                    "\n".join(meta_lines), reply_markup=main_menu_keyboard(user.id)
                )
            await asyncio.gather(
                bot.send_document(
                    chat_id=user.id,
                    document=conf_file,
                    caption="Файл конфигурации WireGuard (.conf).",
                ),
                bot.send_photo(
                    chat_id=user.id,
                    photo=qr_file,
                    caption="QR-код для добавления туннеля в WireGuard.",
                ),
            )
        except Exception:
            logger.exception("Failed to send config/qr")
            if callback.message:
                conf_escaped = html.escape(str(config_text))
                await callback.message.answer(
                    "<b>Конфиг WireGuard:</b>\n\n"
                    f"<pre>{conf_escaped}</pre>\n",
                    reply_markup=main_menu_keyboard(user.id),
                )
        # Refresh list after creation
        try:
            data2 = await call_backend(
                method="GET",
                path="/api/v1/vpn/peers/list",
                params={"telegram_id": user.id},
            )
            peers2 = data2.get("peers") or []
            if not isinstance(peers2, list):
                peers2 = []
            if callback.message:
                configs_kb = await configs_inline_keyboard(peers2)
                await callback.message.edit_reply_markup(reply_markup=configs_kb)
        except Exception:
            pass

    async def _get_peer_config_from_backend(user_id: int, client_id: str) -> dict:
        data = await call_backend(
            method="GET",
            path="/api/v1/vpn/peers/config",
            params={"telegram_id": user_id, "client_id": client_id},
        )
        return data

    @dp.callback_query(F.data.startswith("cfg:dl:"))
    async def cb_configs_download(callback: CallbackQuery) -> None:
        """Download a WireGuard .conf file for a specific peer."""
        user = callback.from_user
        if user is None:
            await callback.answer("Не удалось определить пользователя.", show_alert=True)
            return
        token = (callback.data or "").split("cfg:dl:", 1)[-1].strip()
        if not token:
            await callback.answer("Некорректный запрос.", show_alert=True)
            return
        client_id = await resolve_client_id_from_callback(token)
        if not client_id:
            await callback.answer("Ссылка устарела. Нажмите «Обновить список».", show_alert=True)
            return
        await callback.answer("Готовим .conf...")
        try:
            data = await _get_peer_config_from_backend(user.id, client_id)
        except BackendError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        except Exception:
            logger.exception("Unexpected error in cfg download")
            await callback.answer("Ошибка получения конфига.", show_alert=True)
            return
        config_text = data.get("config")
        client_name = data.get("client_name") or "device"
        if not config_text or not str(config_text).strip():
            await callback.answer("Конфиг не получен (ошибка backend).", show_alert=True)
            return
        filename = safe_filename(
            f"wg_{user.id}_{client_name}.conf", default=f"wg_{user.id}.conf"
        )
        conf_bytes = str(config_text).encode("utf-8", errors="replace")
        conf_file = BufferedInputFile(conf_bytes, filename=filename)
        try:
            await bot.send_document(
                chat_id=user.id,
                document=conf_file,
                caption="Файл конфигурации WireGuard (.conf).",
            )
        except Exception:
            logger.exception("Failed to send .conf as document")
            if callback.message:
                conf_escaped = html.escape(str(config_text))
                await callback.message.answer(
                    f"<b>Ваш конфиг WireGuard:</b>\n\n<pre>{conf_escaped}</pre>\n"
                )
        await callback.answer("Файл отправлен.")

    @dp.callback_query(F.data.startswith("cfg:qr:"))
    async def cb_configs_qr(callback: CallbackQuery) -> None:
        """Send a QR code representing the WireGuard configuration for a peer."""
        user = callback.from_user
        if user is None:
            await callback.answer("Не удалось определить пользователя.", show_alert=True)
            return
        token = (callback.data or "").split("cfg:qr:", 1)[-1].strip()
        if not token:
            await callback.answer("Некорректный запрос.", show_alert=True)
            return
        client_id = await resolve_client_id_from_callback(token)
        if not client_id:
            await callback.answer("Ссылка устарела. Нажмите «Обновить список».", show_alert=True)
            return
        await callback.answer("Готовим QR...")
        try:
            data = await _get_peer_config_from_backend(user.id, client_id)
        except BackendError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        except Exception:
            logger.exception("Unexpected error in cfg qr")
            await callback.answer("Ошибка получения конфига.", show_alert=True)
            return
        config_text = data.get("config")
        client_name = data.get("client_name") or "device"
        if not config_text or not str(config_text).strip():
            await callback.answer("Конфиг не получен (ошибка backend).", show_alert=True)
            return
        try:
            qr_png = build_qr_png_bytes(str(config_text))
            qr_file = BufferedInputFile(qr_png, filename="wireguard_qr.png")
            caption = (
                "<b>QR-код для WireGuard</b>\n"
                f"Устройство: <b>{html.escape(str(client_name))}</b>\n\n"
                "WireGuard → <b>+</b> → <b>Сканировать QR</b>"
            )
            await bot.send_photo(chat_id=user.id, photo=qr_file, caption=caption)
        except Exception:
            logger.exception("Failed to send QR")
            await callback.answer("Не удалось отправить QR. Попробуйте .conf.", show_alert=True)
            return
        await callback.answer("QR отправлен.")

    @dp.callback_query(F.data.startswith("cfg:rv:"))
    async def cb_configs_revoke(callback: CallbackQuery) -> None:
        """Revoke (delete) a peer configuration."""
        user = callback.from_user
        if user is None:
            await callback.answer("Не удалось определить пользователя.", show_alert=True)
            return
        token = (callback.data or "").split("cfg:rv:", 1)[-1].strip()
        if not token:
            await callback.answer("Некорректный запрос.", show_alert=True)
            return
        client_id = await resolve_client_id_from_callback(token)
        if not client_id:
            await callback.answer("Ссылка устарела. Нажмите «Обновить список».", show_alert=True)
            return
        await callback.answer("Удаляем устройство...")
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
            logger.exception("Unexpected error in cfg revoke")
            await callback.answer("Ошибка удаления устройства.", show_alert=True)
            return
        # Refresh list after deletion
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
                kb = await configs_inline_keyboard(peers)
                await callback.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
        await callback.answer("Устройство удалено.")
