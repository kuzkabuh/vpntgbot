"""In-memory storage for the most recent successful payment.

This module stores the details of the last successful payment received via
Telegram's Stars payment system. The data is kept in memory and can be
retrieved by administrators for manual confirmation or troubleshooting.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

__all__ = ["set_last_payment", "get_last_payment"]

# Internal lock and storage for the last payment
_last_payment_lock = asyncio.Lock()
_last_payment: Dict[str, Any] = {}


async def set_last_payment(data: Dict[str, Any]) -> None:
    """
    Save the details of the last successful payment.

    Args:
        data: A dictionary containing payment fields such as telegram_id,
            currency, total_amount, invoice_payload, telegram_payment_charge_id,
            provider_payment_charge_id, etc.
    """
    async with _last_payment_lock:
        _last_payment.clear()
        _last_payment.update(data)


async def get_last_payment() -> Dict[str, Any]:
    """
    Retrieve a copy of the last successful payment details.

    Returns:
        A shallow copy of the stored payment data, or an empty dict if none is stored.
    """
    async with _last_payment_lock:
        return dict(_last_payment)