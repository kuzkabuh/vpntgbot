"""Handlers for subscription plans and Stars payments.

This module registers handlers related to purchasing VPN subscriptions via
Telegram Stars. It handles listing active plans, initiating payment,
processing payment callbacks, and storing the last successful payment.
"""

from __future__ import annotations

import html
import logging
import time
from decimal import Decimal
from typing import Any, List, Dict

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    LabeledPrice,
    PreCheckoutQuery,
)

from backend_client import call_backend, BackendError
from keyboards import main_menu_keyboard, plans_pay_inline_keyboard
from last_payment import set_last_payment
from settings import (
    STARS_ENABLED,
    STARS_CURRENCY,
    STARS_PROVIDER_TOKEN,
    STARS_PAYLOAD_PREFIX,
    STARS_START_PARAMETER_PREFIX,
)

logger = logging.getLogger("vpn-bot.payment")


async def _fetch_active_plans() -> List[Dict[str, Any]]:
    """Fetch active non-trial plans from backend and filter inactive/trial ones."""
    data = await call_backend(method="GET", path="/api/v1/subscription-plans/active")
    plans = data.get("plans") or []
    if not isinstance(plans, list):
        return []
    result: List[Dict[str, Any]] = []
    for p in plans:
        if not isinstance(p, dict):
            continue
        if bool(p.get("is_trial", False)):
            continue
        if not bool(p.get("is_active", True)):
            continue
        result.append(p)
    return result


def register_handlers(dp: Dispatcher, bot: Bot) -> None:
    """Register payment-related handlers on the dispatcher."""

    @dp.message(F.text == "⭐ Купить подписку")
    async def handle_buy_subscription(message: Message) -> None:
        """Present active subscription plans and initiate payment via Stars."""
        user_id = message.from_user.id if message.from_user else None
        if not STARS_ENABLED:
            await message.answer(
                "Оплата временно отключена. Попробуйте позже.",
                reply_markup=main_menu_keyboard(user_id),
            )
            return
        try:
            plans = await _fetch_active_plans()
        except BackendError as exc:
            await message.answer(
                html.escape(str(exc)), reply_markup=main_menu_keyboard(user_id)
            )
            return
        except Exception:
            logger.exception("Plans load error")
            await message.answer(
                "Не удалось загрузить тарифы. Попробуйте позже.",
                reply_markup=main_menu_keyboard(user_id),
            )
            return
        if not plans:
            await message.answer(
                "Активные тарифы не найдены. Попробуйте позже.",
                reply_markup=main_menu_keyboard(user_id),
            )
            return
        text = (
            "<b>Оплата подписки через Telegram Stars</b>\n\n"
            "Выберите тариф ниже. После оплаты я активирую подписку.\n"
            "Если оплата прошла, а подписка не активировалась — напишите в поддержку.\n\n"
            "Важно: Stars — внутренняя валюта Telegram. Оплата происходит прямо в Telegram."
        )
        await message.answer(text, reply_markup=main_menu_keyboard(user_id))
        await message.answer("Тарифы:", reply_markup=plans_pay_inline_keyboard(plans))

    @dp.callback_query(F.data == "pay:refresh")
    async def cb_pay_refresh(callback: CallbackQuery) -> None:
        """Refresh the list of subscription plans and update the keyboard."""
        if not STARS_ENABLED:
            await callback.answer("Оплата отключена.", show_alert=True)
            return
        try:
            plans = await _fetch_active_plans()
        except Exception:
            await callback.answer("Не удалось обновить тарифы.", show_alert=True)
            return
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=plans_pay_inline_keyboard(plans))
            except Exception:
                pass
        await callback.answer("Обновлено.")

    @dp.callback_query(F.data.startswith("pay:"))
    async def cb_pay_plan(callback: CallbackQuery) -> None:
        """Initiate payment for the selected subscription plan."""
        user = callback.from_user
        if user is None:
            await callback.answer("Не удалось определить пользователя.", show_alert=True)
            return
        if not STARS_ENABLED:
            await callback.answer("Оплата отключена.", show_alert=True)
            return
        plan_code = (callback.data or "").split("pay:", 1)[-1].strip()
        if not plan_code or plan_code == "refresh":
            await callback.answer("Некорректный тариф.", show_alert=True)
            return
        try:
            plans = await _fetch_active_plans()
        except BackendError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        except Exception:
            await callback.answer("Не удалось загрузить тариф.", show_alert=True)
            return
        selected = None
        for p in plans:
            if str(p.get("code") or "").strip() == plan_code:
                selected = p
                break
        if not selected:
            await callback.answer(
                "Тариф не найден или отключён. Обновите список.",
                show_alert=True,
            )
            return
        name = str(selected.get("name") or "VPN тариф").strip()
        price_stars = selected.get("price_stars")
        try:
            amount = int(Decimal(str(price_stars)))
        except Exception:
            amount = 0
        if amount <= 0:
            await callback.answer("Некорректная цена тарифа.", show_alert=True)
            return
        await callback.answer("Открываю оплату...")
        payload = f"{STARS_PAYLOAD_PREFIX}{plan_code}:{user.id}:{int(time.time())}"
        prices = [LabeledPrice(label=name, amount=amount)]
        try:
            await bot.send_invoice(
                chat_id=user.id,
                title=f"VPN подписка: {name}",
                description="Оплата подписки VPN через Telegram Stars.",
                payload=payload,
                currency=STARS_CURRENCY,
                prices=prices,
                provider_token=STARS_PROVIDER_TOKEN,
                start_parameter=f"{STARS_START_PARAMETER_PREFIX}_{plan_code}",
                need_name=False,
                need_phone_number=False,
                need_email=False,
                need_shipping_address=False,
                is_flexible=False,
            )
        except Exception:
            logger.exception("Failed to send invoice")
            await callback.answer("Не удалось создать счёт. Попробуйте позже.", show_alert=True)

    @dp.pre_checkout_query()
    async def pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
        """Answer the pre-checkout query to proceed with payment."""
        try:
            await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
        except Exception:
            logger.exception("pre_checkout answer failed")

    @dp.message(F.successful_payment)
    async def on_successful_payment(message: Message) -> None:
        """Handle successful payment callback from Telegram and auto-confirm it."""
        sp = message.successful_payment
        if sp is None:
            return
        payload = getattr(sp, "invoice_payload", "") or ""
        currency = getattr(sp, "currency", "") or ""
        total_amount = getattr(sp, "total_amount", None)
        tg_charge_id = getattr(sp, "telegram_payment_charge_id", "") or ""
        provider_charge_id = getattr(sp, "provider_payment_charge_id", "") or ""
        logger.info(
            "SUCCESSFUL_PAYMENT: currency=%s amount=%s payload=%s",
            currency,
            total_amount,
            payload,
        )
        # Save last payment details for admin
        try:
            await set_last_payment(
                {
                    "telegram_id": (message.from_user.id if message.from_user else None),
                    "currency": currency,
                    "total_amount": total_amount,
                    "invoice_payload": payload,
                    "telegram_payment_charge_id": tg_charge_id,
                    "provider_payment_charge_id": provider_charge_id,
                }
            )
        except Exception:
            pass
        # Auto-confirm on backend
        try:
            if currency == "XTR" and payload:
                user_id = message.from_user.id if message.from_user else None
                if user_id is not None:
                    req = {
                        "telegram_id": user_id,
                        "invoice_payload": payload,
                        "currency": currency,
                        "amount": total_amount,
                        "telegram_payment_charge_id": tg_charge_id,
                        "provider_payment_charge_id": provider_charge_id or None,
                    }
                    _ = await call_backend(
                        method="POST",
                        path="/api/v1/payments/stars/confirm",
                        json=req,
                    )
        except Exception as exc:
            logger.warning("Auto-confirm failed: %s", exc)
        # Inform user
        await message.answer(
            "<b>Оплата получена.</b>\n\n"
            "Подписка будет активирована автоматически.\n"
            "Если в течение пары минут статус не изменится — напишите в поддержку.",
            reply_markup=main_menu_keyboard(
                message.from_user.id if message.from_user else None
            ),
        )
