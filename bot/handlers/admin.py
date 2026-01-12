"""Administrative handlers for payment and subscription management.

This module registers handlers accessible only to admins. It allows
administrators to view active plans, check a user's subscription by
Telegram ID, manually confirm a payment, view the last successful
payment, and navigate between admin and main menus. It also manages
admin-specific text inputs via a simple pending state mechanism.
"""

from __future__ import annotations

import html
import logging
from decimal import Decimal
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

from backend_client import call_backend, BackendError
from keyboards import main_menu_keyboard, admin_payments_keyboard
from pending_state import set_pending, pop_pending, peek_pending
from settings import is_admin
from last_payment import get_last_payment

logger = logging.getLogger("vpn-bot.admin")


def register_handlers(dp: Dispatcher, bot: Bot) -> None:
    """Register admin-only command and menu handlers."""

    @dp.message(F.text == "🛡 Админ: Платежи/подписки")
    async def handle_admin_payments_menu(message: Message) -> None:
        """Enter the admin payments/subscriptions menu."""
        user = message.from_user
        if user is None or not is_admin(user.id):
            await message.answer(
                "Доступ запрещён.",
                reply_markup=main_menu_keyboard(user.id if user else None),
            )
            return
        text = (
            "<b>Админ-панель: Платежи и подписки</b>\n\n"
            "Доступные действия:\n"
            "• посмотреть тарифы (как видит их бот);\n"
            "• проверить статус подписки по Telegram ID;\n"
            "• вручную подтвердить Stars оплату (payload/charge_id);\n"
            "• посмотреть данные последнего платежа из логики бота."
        )
        await message.answer(text, reply_markup=admin_payments_keyboard())

    @dp.message(F.text == "⬅️ Назад в меню")
    async def handle_back_to_main(message: Message) -> None:
        """Return to the main menu from the admin panel."""
        user = message.from_user
        user_id = user.id if user else None
        await message.answer("Главное меню.", reply_markup=main_menu_keyboard(user_id))

    @dp.message(F.text == "🧾 Планы (backend)")
    async def admin_plans(message: Message) -> None:
        """List active subscription plans from the backend."""
        user = message.from_user
        if user is None or not is_admin(user.id):
            await message.answer(
                "Доступ запрещён.",
                reply_markup=main_menu_keyboard(user.id if user else None),
            )
            return
        try:
            data = await call_backend(
                method="GET", path="/api/v1/subscription-plans/active"
            )
        except Exception as exc:
            await message.answer(
                f"Ошибка загрузки планов: {html.escape(str(exc))}",
                reply_markup=admin_payments_keyboard(),
            )
            return
        plans = data.get("plans") or []
        if not isinstance(plans, list) or not plans:
            await message.answer("Планы не найдены.", reply_markup=admin_payments_keyboard())
            return
        lines: list[str] = ["<b>Активные тарифы (backend)</b>", ""]
        for p in plans:
            if not isinstance(p, dict):
                continue
            code = html.escape(str(p.get("code") or ""))
            name = html.escape(str(p.get("name") or ""))
            days = html.escape(str(p.get("duration_days") or ""))
            stars = html.escape(str(p.get("price_stars") or ""))
            is_trial_plan = bool(p.get("is_trial", False))
            max_dev = p.get("max_devices", None)
            max_dev_str = "∞" if max_dev in (None, 0, "") else html.escape(str(max_dev))
            flag = "🎁" if is_trial_plan else "⭐"
            lines.append(
                f"{flag} <b>{name}</b> — <code>{code}</code> — {days} дней — {stars} Stars — max_devices: {max_dev_str}"
            )
        await message.answer("\n".join(lines), reply_markup=admin_payments_keyboard())

    @dp.message(F.text == "🔎 Проверить подписку (TG ID)")
    async def admin_check_sub_prompt(message: Message) -> None:
        """Prompt admin to enter a Telegram ID for subscription check."""
        user = message.from_user
        if user is None or not is_admin(user.id):
            await message.answer(
                "Доступ запрещён.",
                reply_markup=main_menu_keyboard(user.id if user else None),
            )
            return
        await set_pending(user.id, "admin_check_sub")
        await message.answer(
            "Введите Telegram ID пользователя (число).",
            reply_markup=admin_payments_keyboard(),
        )

    @dp.message(F.text == "✅ Подтвердить Stars оплату (payload)")
    async def admin_confirm_payment_prompt(message: Message) -> None:
        """Prompt admin to enter payment data for manual confirmation."""
        user = message.from_user
        if user is None or not is_admin(user.id):
            await message.answer(
                "Доступ запрещён.",
                reply_markup=main_menu_keyboard(user.id if user else None),
            )
            return
        await set_pending(user.id, "admin_confirm_payment")
        await message.answer(
            "Отправьте одной строкой данные для подтверждения.\n\n"
            "Формат:\n"
            "<code>telegram_id|invoice_payload|telegram_payment_charge_id|provider_payment_charge_id|amount</code>\n\n"
            "Пример:\n"
            "<code>123456|vpn_plan:m1_69:123456:1700000000|abc123|def456|69</code>\n\n"
            "provider_payment_charge_id можно оставить пустым, но разделители должны быть:\n"
            "<code>123456|vpn_plan:m1_69:123456:...|abc123||69</code>",
            reply_markup=admin_payments_keyboard(),
        )

    @dp.message(F.text == "🕘 Последний платёж")
    async def admin_last_payment(message: Message) -> None:
        """Show the last successful payment recorded by the bot."""
        user = message.from_user
        if user is None or not is_admin(user.id):
            await message.answer(
                "Доступ запрещён.",
                reply_markup=main_menu_keyboard(user.id if user else None),
            )
            return
        data = await get_last_payment()
        if not data:
            await message.answer(
                "Пока нет сохранённых данных о платежах (successful_payment).",
                reply_markup=admin_payments_keyboard(),
            )
            return
        lines: list[str] = ["<b>Последний successful_payment (в памяти бота)</b>", ""]
        for k in (
            "telegram_id",
            "currency",
            "total_amount",
            "invoice_payload",
            "telegram_payment_charge_id",
            "provider_payment_charge_id",
        ):
            if k in data:
                lines.append(
                    f"{html.escape(k)}: <code>{html.escape(str(data.get(k) or ''))}</code>"
                )
        lines.append("")
        lines.append(
            "Если нужно — используйте «✅ Подтвердить Stars оплату (payload)» и вставьте данные выше."
        )
        await message.answer("\n".join(lines), reply_markup=admin_payments_keyboard())

    # Admin input handler; flags={'block': False} to avoid blocking other handlers
    @dp.message(F.text, flags={'block': False})
    async def handle_admin_input(message: Message) -> None:
        """Process admin inputs when a pending admin action exists."""
        user = message.from_user
        if user is None:
            return
        pending = await peek_pending(user.id)
        if not pending:
            # No pending action; do not intercept
            return
        # Remove pending immediately to avoid duplicates on errors
        pending = await pop_pending(user.id)
        if not pending:
            return
        text = (message.text or "").strip()
        if pending.action == "admin_check_sub":
            # Validate Telegram ID and show subscription status
            try:
                tid = int(text)
            except Exception:
                await message.answer(
                    "Ошибка: нужен Telegram ID числом. Повторите команду.",
                    reply_markup=admin_payments_keyboard(),
                )
                return
            try:
                data = await call_backend(
                    method="GET", path=f"/api/v1/users/{tid}/subscription/active"
                )
            except Exception as exc:
                await message.answer(
                    f"Ошибка запроса: {html.escape(str(exc))}",
                    reply_markup=admin_payments_keyboard(),
                )
                return
            has_sub = bool(data.get("has_active_subscription", False))
            is_trial_active = bool(data.get("is_trial_active", False))
            ends_at = data.get("subscription_ends_at")
            plan_name = data.get("active_plan_name")
            trial_available = bool(data.get("trial_available", False))
            lines = [f"<b>Статус подписки пользователя</b> <code>{tid}</code>", ""]
            if has_sub:
                lines.append(
                    f"Тариф: <b>{html.escape(str(plan_name or 'активный тариф'))}</b>"
                )
                lines.append(
                    "Тип: <b>триал</b>" if is_trial_active else "Тип: <b>платная подписка</b>"
                )
                if ends_at:
                    lines.append(
                        f"До: <code>{html.escape(str(ends_at))}</code> (UTC)"
                    )
                else:
                    lines.append("До: <b>без ограничения</b>")
            else:
                lines.append("Активной подписки нет.")
                lines.append(
                    "Триал доступен: <b>да</b>" if trial_available else "Триал доступен: <b>нет</b>"
                )
            await message.answer(
                "\n".join(lines), reply_markup=admin_payments_keyboard()
            )
            return
        if pending.action == "admin_confirm_payment":
            # Confirm payment manually
            parts = text.split("|")
            if len(parts) != 5:
                await message.answer(
                    "Ошибка формата. Нужно 5 частей через |. Повторите команду.",
                    reply_markup=admin_payments_keyboard(),
                )
                return
            raw_tid, invoice_payload, tg_charge_id, provider_charge_id, raw_amount = [
                p.strip() for p in parts
            ]
            try:
                tid = int(raw_tid)
            except Exception:
                await message.answer(
                    "Ошибка: telegram_id должен быть числом.",
                    reply_markup=admin_payments_keyboard(),
                )
                return
            try:
                amount = int(Decimal(raw_amount))
            except Exception:
                amount = None
            req = {
                "telegram_id": tid,
                "invoice_payload": invoice_payload,
                "currency": "XTR",
                "amount": amount,
                "telegram_payment_charge_id": tg_charge_id,
                "provider_payment_charge_id": provider_charge_id or None,
            }
            try:
                resp = await call_backend(
                    method="POST",
                    path="/api/v1/payments/stars/confirm",
                    json=req,
                )
            except Exception as exc:
                await message.answer(
                    f"Ошибка подтверждения: {html.escape(str(exc))}",
                    reply_markup=admin_payments_keyboard(),
                )
                return
            msg = resp.get("message") or "Готово."
            ok = bool(resp.get("success", True))
            await message.answer(
                f"{'✅' if ok else '⚠️'} {html.escape(str(msg))}",
                reply_markup=admin_payments_keyboard(),
            )
            return
