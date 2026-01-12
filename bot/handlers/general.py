"""General user-facing handlers for the VPN Telegram bot.

This module registers handlers for common user commands such as
``/start``, ``/help``, and user menu actions like viewing subscription
status, activating a trial, viewing the instruction, and learning
about the project. Handlers related to payments, configurations,
devices, and admin functionality are registered in separate modules.
"""

from __future__ import annotations

import html
import logging
import time
from decimal import Decimal
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from backend_client import call_backend, BackendError
from instructions import build_instruction_text
from keyboards import main_menu_keyboard, plans_pay_inline_keyboard
from settings import (
    STARS_ENABLED,
    STARS_CURRENCY,
    STARS_PROVIDER_TOKEN,
    STARS_PAYLOAD_PREFIX,
    STARS_START_PARAMETER_PREFIX,
    MAX_CONFIGS_PER_USER,
)
from last_payment import set_last_payment

logger = logging.getLogger("vpn-bot.general")


def register_handlers(dp: Dispatcher, bot: Bot) -> None:
    """Register general (non-admin) handlers on the given dispatcher.

    Args:
        dp: The aiogram Dispatcher instance.
        bot: The aiogram Bot instance. Captured in closures for use in
            handlers that need to send invoices or answer payment queries.
    """

    @dp.message(CommandStart())
    async def handle_start(message: Message) -> None:
        """Greet the user, register them in the backend and show the main menu."""
        user = message.from_user
        if user is None:
            await message.answer(
                "Не удалось определить пользователя Telegram.",
                reply_markup=main_menu_keyboard(None),
            )
            return

        try:
            backend_resp = await call_backend(
                method="POST",
                path="/api/v1/users/from-telegram",
                json={
                    "telegram_id": user.id,
                    "username": user.username,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "language_code": user.language_code,
                },
            )
        except BackendError as exc:
            await message.answer(
                "Произошла ошибка при обращении к серверу.\n"
                f"{html.escape(str(exc))}",
                reply_markup=main_menu_keyboard(user.id),
            )
            return
        except Exception:
            logger.exception("Unexpected error in /start")
            await message.answer(
                "Произошла непредвиденная ошибка. Попробуйте ещё раз позже.",
                reply_markup=main_menu_keyboard(user.id),
            )
            return

        has_sub = bool(backend_resp.get("has_active_subscription", False))
        is_trial_active = bool(backend_resp.get("is_trial_active", False))
        ends_at = backend_resp.get("subscription_ends_at")
        trial_available = bool(backend_resp.get("trial_available", False))
        plan_name = backend_resp.get("active_plan_name")

        greeting: list[str] = [
            f"Привет, <b>{html.escape(user.full_name)}</b>.",
            "",
            "Я помогу подключить VPN (WireGuard).",
            "",
            "Основные разделы:",
            "• <b>🔐 Конфиги WireGuard</b> — скачать .conf или получить QR;",
            "• <b>📊 Статус подписки</b> — проверить тариф;",
            "• <b>🎁 Активировать триал</b> — 10 дней (если доступно);",
            "• <b>📖 Инструкция подключения</b> — пошагово.",
            "",
        ]

        if has_sub:
            plan_label = plan_name or "активный тариф"
            greeting.append(f"Текущий тариф: <b>{html.escape(str(plan_label))}</b>.")
            greeting.append(
                "Тип: <b>бесплатный пробный период</b>" if is_trial_active else "Тип: <b>платная подписка</b>."
            )
            if ends_at:
                greeting.append(f"Действует до: <code>{html.escape(str(ends_at))}</code> (UTC).")
            else:
                greeting.append("Срок действия: <b>без ограничения</b>.")
        else:
            greeting.append("У вас пока нет активной подписки.")
            if trial_available:
                greeting.append("Можно активировать <b>бесплатный пробный период на 10 дней</b>.")

        greeting.append("")
        greeting.append("Выберите действие в меню ниже.")

        await message.answer("\n".join(greeting), reply_markup=main_menu_keyboard(user.id))

    @dp.message(Command("help"))
    async def handle_help(message: Message) -> None:
        """Provide a short help message explaining the bot's commands."""
        user_id = message.from_user.id if message.from_user else None
        text = (
            "<b>Справка по боту</b>\n\n"
            "• «📊 Статус подписки» — тариф/срок;\n"
            "• «🎁 Активировать триал» — пробный период (если доступно);\n"
            "• «🔐 Конфиги WireGuard» — .conf и QR, добавить/удалить устройства;\n"
            "• «📱 Устройства» — список и отключение;\n"
            "• «📖 Инструкция подключения» — как подключить WireGuard.\n\n"
            "Команды:\n"
            "• /start\n"
            "• /help\n"
            "• /instruction\n"
        )
        await message.answer(text, reply_markup=main_menu_keyboard(user_id))

    @dp.message(Command("instruction"))
    async def handle_instruction_cmd(message: Message) -> None:
        """Return the detailed connection instruction on /instruction command."""
        user_id = message.from_user.id if message.from_user else None
        await message.answer(build_instruction_text(), reply_markup=main_menu_keyboard(user_id))

    @dp.message(F.text == "📖 Инструкция подключения")
    async def handle_instruction_button(message: Message) -> None:
        """Return the detailed connection instruction when selected from menu."""
        user_id = message.from_user.id if message.from_user else None
        await message.answer(build_instruction_text(), reply_markup=main_menu_keyboard(user_id))

    @dp.message(F.text == "📊 Статус подписки")
    async def handle_status(message: Message) -> None:
        """Show the user's current subscription status."""
        user = message.from_user
        if user is None:
            await message.answer(
                "Не удалось определить пользователя Telegram.",
                reply_markup=main_menu_keyboard(None),
            )
            return
        try:
            data = await call_backend(
                method="GET", path=f"/api/v1/users/{user.id}/subscription/active"
            )
        except BackendError as exc:
            await message.answer(html.escape(str(exc)), reply_markup=main_menu_keyboard(user.id))
            return
        except Exception:
            logger.exception("Unexpected error in status")
            await message.answer(
                "Ошибка при запросе статуса. Попробуйте позже.",
                reply_markup=main_menu_keyboard(user.id),
            )
            return

        has_sub = bool(data.get("has_active_subscription", False))
        is_trial_active = bool(data.get("is_trial_active", False))
        ends_at = data.get("subscription_ends_at")
        plan_name = data.get("active_plan_name")
        trial_available = bool(data.get("trial_available", False))

        lines: list[str] = ["<b>Ваш статус подписки:</b>", ""]
        if has_sub:
            plan_str = plan_name or "активный тариф"
            lines.append(f"Тариф: <b>{html.escape(str(plan_str))}</b>")
            lines.append(
                "Тип: <b>триал</b>" if is_trial_active else "Тип: <b>платная подписка</b>"
            )
            if ends_at:
                lines.append(f"Действует до: <code>{html.escape(str(ends_at))}</code> (UTC)")
            else:
                lines.append("Срок действия: <b>без ограничения</b>")
        else:
            lines.append("Активной подписки нет.")
            if trial_available:
                lines.append("Можно активировать <b>бесплатный пробный период на 10 дней</b>.")
            else:
                lines.append("Бесплатный пробный период уже был использован.")

        await message.answer("\n".join(lines), reply_markup=main_menu_keyboard(user.id))

    @dp.message(F.text == "🎁 Активировать триал")
    async def handle_activate_trial(message: Message) -> None:
        """Activate a free trial for the user if available."""
        user = message.from_user
        if user is None:
            await message.answer(
                "Не удалось определить пользователя Telegram.",
                reply_markup=main_menu_keyboard(None),
            )
            return
        try:
            data = await call_backend(
                method="POST", path=f"/api/v1/users/{user.id}/trial/activate"
            )
        except BackendError as exc:
            await message.answer(html.escape(str(exc)), reply_markup=main_menu_keyboard(user.id))
            return
        except Exception:
            logger.exception("Unexpected error in trial")
            await message.answer(
                "Ошибка при активации пробного периода. Попробуйте позже.",
                reply_markup=main_menu_keyboard(user.id),
            )
            return

        success = bool(data.get("success", False))
        message_text = str(data.get("message", ""))
        trial_ends_at = data.get("trial_ends_at")
        already_had_trial = bool(data.get("already_had_trial", False))

        lines: list[str] = []
        if success:
            lines.append("<b>Триал активирован.</b>")
            if trial_ends_at:
                lines.append(f"Действует до: <code>{html.escape(str(trial_ends_at))}</code> (UTC)")
            lines.append("")
            lines.append("Теперь откройте «🔐 Конфиги WireGuard» и добавьте устройство.")
        else:
            if already_had_trial:
                lines.append("Триал уже был использован ранее.")
            else:
                lines.append("Не удалось активировать триал.")
            if message_text:
                lines.append("")
                lines.append(html.escape(message_text))

        await message.answer("\n".join(lines), reply_markup=main_menu_keyboard(user.id))

    @dp.message(F.text == "ℹ️ О проекте")
    async def handle_about(message: Message) -> None:
        """Show information about the VPN project."""
        user_id = message.from_user.id if message.from_user else None
        limit_line = "без ограничений" if MAX_CONFIGS_PER_USER <= 0 else str(MAX_CONFIGS_PER_USER)
        text = (
            "<b>О VPN-проекте</b>\n\n"
            "Сервис предоставляет доступ к VPN на базе WireGuard.\n\n"
            "Возможности:\n"
            "• управление устройствами и конфигами WireGuard;\n"
            f"• лимит устройств: <b>{limit_line}</b>;\n"
            "• триал и тарифы (в зависимости от настроек backend).\n\n"
            "Если нужна помощь — используйте «📖 Инструкция подключения»."
        )
        await message.answer(text, reply_markup=main_menu_keyboard(user_id))

    # Fallback for unrecognized commands (non-admin). It should be last.
    # Use a non-blocking handler so that other more specific handlers registered
    # later can still process the message. Without `flags={'block': False}`
    # aiogram would stop processing further handlers after this fallback.
    @dp.message(flags={"block": False})
    async def handle_fallback(message: Message) -> None:
        """Fallback handler for unknown commands."""
        user_id = message.from_user.id if message.from_user else None
        await message.answer(
            "Команда не распознана. Используйте меню или /start, /help, /instruction.",
            reply_markup=main_menu_keyboard(user_id),
        )
