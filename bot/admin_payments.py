"""
# ----------------------------------------------------------
# Версия файла: 1.0.0
# Описание: Админ-команды бота для просмотра платежей
# Дата изменения: 2026-01-12
#
# Команды:
#  - /payments_last20
#  - /user_payments <telegram_id>
#
# Требует:
#  - ADMIN_TELEGRAM_IDS в env бота
#  - MGMT_API_TOKEN в env бота (для админских backend эндпоинтов)
# ----------------------------------------------------------
"""

from __future__ import annotations

import os
import logging
from typing import Any

import httpx
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger("vpn-bot")
router = Router()


def _admin_ids() -> set[int]:
    raw = (os.getenv("ADMIN_TELEGRAM_IDS") or "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for x in raw.split(","):
        x = x.strip()
        if not x:
            continue
        try:
            out.add(int(x))
        except Exception:
            continue
    return out


def _is_admin(telegram_id: int) -> bool:
    return telegram_id in _admin_ids()


def _backend_base_url() -> str:
    return "http://backend:8000"


def _mgmt_token() -> str:
    t = (os.getenv("MGMT_API_TOKEN") or "").strip()
    return t


async def _backend_get(path: str) -> tuple[int, Any]:
    url = f"{_backend_base_url()}{path}"
    headers = {"X-Mgmt-Token": _mgmt_token()}
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, headers=headers)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}
        return r.status_code, data


@router.message(Command("payments_last20"))
async def payments_last20(message: Message) -> None:
    tg = int(message.from_user.id) if message.from_user else 0
    if not _is_admin(tg):
        await message.answer("Доступ запрещен.")
        return

    if not _mgmt_token():
        await message.answer("MGMT_API_TOKEN не настроен в окружении бота.")
        return

    code, data = await _backend_get("/api/v1/admin/payments?limit=20")
    if code >= 400:
        await message.answer(f"Ошибка backend: {code}\n{data}")
        return

    if not isinstance(data, list) or not data:
        await message.answer("Платежей пока нет.")
        return

    lines: list[str] = ["Последние платежи (20):\n"]
    for p in data:
        try:
            pid = p.get("id")
            tgid = p.get("telegram_id")
            amt = p.get("amount")
            cur = p.get("currency")
            plan = p.get("plan_code") or p.get("plan_name") or "-"
            created = p.get("created_at")
            status = p.get("status")
            lines.append(f"#{pid} | tg:{tgid} | {amt} {cur} | {plan} | {status} | {created}")
        except Exception:
            continue

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n...\n(обрезано)"
    await message.answer(text)


@router.message(Command("user_payments"))
async def user_payments(message: Message) -> None:
    tg = int(message.from_user.id) if message.from_user else 0
    if not _is_admin(tg):
        await message.answer("Доступ запрещен.")
        return

    if not _mgmt_token():
        await message.answer("MGMT_API_TOKEN не настроен в окружении бота.")
        return

    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.answer("Использование: /user_payments <telegram_id>")
        return

    try:
        user_tid = int(parts[1])
    except Exception:
        await message.answer("telegram_id должен быть числом.")
        return

    code, data = await _backend_get(f"/api/v1/admin/users/{user_tid}/payments?limit=50")
    if code >= 400:
        await message.answer(f"Ошибка backend: {code}\n{data}")
        return

    if not isinstance(data, list) or not data:
        await message.answer("Платежей по этому пользователю не найдено.")
        return

    lines: list[str] = [f"Платежи пользователя tg:{user_tid}:\n"]
    for p in data:
        try:
            pid = p.get("id")
            amt = p.get("amount")
            cur = p.get("currency")
            plan = p.get("plan_code") or p.get("plan_name") or "-"
            created = p.get("created_at")
            status = p.get("status")
            lines.append(f"#{pid} | {amt} {cur} | {plan} | {status} | {created}")
        except Exception:
            continue

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n...\n(обрезано)"
    await message.answer(text)
