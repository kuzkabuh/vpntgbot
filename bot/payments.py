"""
# ----------------------------------------------------------
# Версия файла: 1.0.0
# Описание: Обработка оплат Telegram Stars (successful_payment)
# Дата изменения: 2026-01-12
#
# Логика:
#  - принять successful_payment
#  - вызвать backend /api/v1/payments/telegram/success
#  - уведомить пользователя
#  - уведомить админов
# ----------------------------------------------------------
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
from aiogram import Router, F
from aiogram.types import Message

logger = logging.getLogger("vpn-bot")

router = Router()


def _get_backend_base_url() -> str:
    # В твоих логах уже используется http://backend:8000
    return "http://backend:8000"


def _get_admin_ids_from_env() -> list[int]:
    # Подхватим ADMIN_TELEGRAM_IDS из env бота (так же как в backend)
    # Формат: "123,456"
    import os
    raw = (os.getenv("ADMIN_TELEGRAM_IDS") or "").strip()
    if not raw:
        return []
    ids: list[int] = []
    for x in raw.split(","):
        x = x.strip()
        if not x:
            continue
        try:
            ids.append(int(x))
        except Exception:
            continue
    return ids


async def _notify_admins(bot, text: str) -> None:
    admin_ids = _get_admin_ids_from_env()
    if not admin_ids:
        return
    for aid in admin_ids:
        try:
            await bot.send_message(aid, text)
        except Exception:
            continue


@router.message(F.successful_payment)
async def on_successful_payment(message: Message) -> None:
    sp = message.successful_payment
    if not sp:
        return

    telegram_id = int(message.from_user.id) if message.from_user else 0
    currency = str(getattr(sp, "currency", "") or "")
    total_amount = getattr(sp, "total_amount", None)
    invoice_payload = str(getattr(sp, "invoice_payload", "") or "")

    telegram_payment_charge_id = str(getattr(sp, "telegram_payment_charge_id", "") or "")
    provider_payment_charge_id = str(getattr(sp, "provider_payment_charge_id", "") or "")

    # В Stars total_amount у Telegram часто уже в "целых" единицах Stars для XTR,
    # но чтобы не гадать — мы в вашем проекте уже логируем amount=69.
    # Передадим как float(total_amount) если есть, иначе 0.
    amount = 0.0
    try:
        if total_amount is not None:
            amount = float(total_amount)
    except Exception:
        amount = 0.0

    # Если у вас total_amount приходит 69 — отлично.
    # Если вдруг приходит 6900 (как для валюты в "минимальных единицах") —
    # тогда это надо будет поправить. Пока оставляем как есть и контролируем по факту.
    payload_to_backend: dict[str, Any] = {
        "telegram_id": telegram_id,
        "currency": currency,
        "amount": amount,
        "invoice_payload": invoice_payload,
        "telegram_payment_charge_id": telegram_payment_charge_id,
        "provider_payment_charge_id": provider_payment_charge_id or None,
    }

    logger.info(
        "SUCCESSFUL_PAYMENT: currency=%s amount=%s payload=%s charge_id=%s",
        currency,
        amount,
        invoice_payload,
        telegram_payment_charge_id,
    )

    backend_url = f"{_get_backend_base_url()}/api/v1/payments/telegram/success"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(backend_url, json=payload_to_backend)
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}

        if r.status_code >= 400:
            msg = f"Оплата получена, но активация подписки не удалась (ошибка backend).\n\nКод: {r.status_code}\nДетали: {data}"
            await message.answer(msg)
            await _notify_admins(
                message.bot,
                f"[PAYMENT][ERROR] tg={telegram_id} amount={amount} {currency}\npayload={invoice_payload}\nstatus={r.status_code}\nresp={data}",
            )
            return

        ok = bool(data.get("ok"))
        if ok:
            plan_name = data.get("plan_name") or data.get("plan_code") or "тариф"
            active_until = data.get("active_until")
            if active_until:
                await message.answer(
                    f"Оплата получена. Подписка активирована.\n\nТариф: {plan_name}\nАктивна до: {active_until}"
                )
            else:
                await message.answer(f"Оплата получена. Подписка активирована.\n\nТариф: {plan_name}")

            await _notify_admins(
                message.bot,
                f"[PAYMENT][OK] tg={telegram_id} amount={amount} {currency}\nplan={data.get('plan_code')} until={active_until}\ncharge_id={telegram_payment_charge_id}",
            )
        else:
            await message.answer("Оплата получена, но подписка не активировалась автоматически. Напишите в поддержку.")
            await _notify_admins(
                message.bot,
                f"[PAYMENT][WARN] tg={telegram_id} amount={amount} {currency}\npayload={invoice_payload}\nresp={data}",
            )

    except Exception as exc:
        await message.answer("Оплата получена, но при активации подписки произошла ошибка. Напишите в поддержку.")
        await _notify_admins(
            message.bot,
            f"[PAYMENT][EXC] tg={telegram_id} amount={amount} {currency}\npayload={invoice_payload}\nerr={exc!r}",
        )
