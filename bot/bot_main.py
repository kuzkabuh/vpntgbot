"""
Версия файла: 1.4.0
Описание: Telegram-бот для VPN-сервиса (меню тарифов, активация триала, запрос WireGuard-конфига)
Дата изменения: 2025-12-29

Основное:
- /start: регистрация пользователя в backend, показ статуса.
- Кнопки:
  - 📊 Мой тариф и статус VPN
  - 🎁 Активировать бесплатный период
  - 🔐 Получить конфиг WireGuard
  - ℹ️ О проекте
- Обращения к backend:
  - POST /api/v1/users/from-telegram
  - GET  /api/v1/users/{telegram_id}/subscription/active
  - POST /api/v1/users/{telegram_id}/trial/activate
  - POST /api/v1/vpn/peers/create
"""

import asyncio
import html
import logging
import os
from typing import Optional

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

# ------------------------------------------------------
# Логирование
# ------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[VPN-BOT] %(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("vpn-bot")

# ------------------------------------------------------
# Окружение
# ------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://vpn_backend:8000")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в окружении бота.")

# ------------------------------------------------------
# Инициализация бота и диспетчера (aiogram v3.x)
# ------------------------------------------------------

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()

# ------------------------------------------------------
# Вспомогательные функции
# ------------------------------------------------------


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню бота."""
    keyboard = [
        [KeyboardButton(text="📊 Мой тариф и статус VPN")],
        [KeyboardButton(text="🎁 Активировать бесплатный период")],
        [KeyboardButton(text="🔐 Получить конфиг WireGuard")],
        [KeyboardButton(text="ℹ️ О проекте")],
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
    )


async def call_backend(
    method: str,
    path: str,
    json: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: float = 10.0,
) -> dict:
    """Универсальная функция для запросов к backend."""
    url = BACKEND_BASE_URL.rstrip("/") + path
    logger.info("Запрос к backend: %s %s", method, url)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(method=method, url=url, json=json, params=params)
        try:
            data = resp.json()
        except Exception:
            logger.exception("Не удалось разобрать JSON-ответ backend: %s", resp.text)
            raise

        if resp.status_code >= 400:
            logger.warning("Backend вернул ошибку %s: %s", resp.status_code, data)
            raise RuntimeError(
                data.get("detail") if isinstance(data, dict) else f"HTTP {resp.status_code}"
            )

        return data


# ------------------------------------------------------
# Хэндлеры
# ------------------------------------------------------


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Обработка команды /start."""
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
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
    except Exception:
        logger.exception("Ошибка при регистрации пользователя в backend")
        await message.answer(
            "Произошла ошибка при обращении к серверу.\n"
            "Попробуйте ещё раз чуть позже."
        )
        return

    greeting = [
        f"Привет, <b>{html.escape(user.full_name)}</b> 👋",
        "",
        "Это VPN-бот. Здесь можно:",
        "• посмотреть статус подписки;",
        "• активировать бесплатный пробный период (1 раз);",
        "• в будущем — оплачивать тарифы звёздами и получать конфиг WireGuard автоматически.",
        "",
    ]

    has_sub = backend_resp.get("has_active_subscription", False)
    is_trial_active = backend_resp.get("is_trial_active", False)
    ends_at = backend_resp.get("subscription_ends_at")
    trial_available = backend_resp.get("trial_available", False)
    plan_name = backend_resp.get("active_plan_name")

    if has_sub:
        plan_label = plan_name or "активный тариф"
        greeting.append(f"Сейчас у вас есть <b>{html.escape(plan_label)}</b>.")
        if is_trial_active:
            greeting.append("Тип: <b>бесплатный пробный период</b>.")
        if ends_at:
            greeting.append(f"Подписка действительна до: <code>{ends_at}</code>.")
    else:
        greeting.append("У вас пока нет активной подписки.")
        if trial_available:
            greeting.append("Вы можете активировать <b>бесплатный пробный период на 10 дней</b>.")

    greeting.append("")
    greeting.append("Выберите нужный пункт в меню ниже 👇")

    await message.answer("\n".join(greeting), reply_markup=main_menu_keyboard())


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Краткая справка по команде /help."""
    text = (
        "<b>Справка по боту</b>\n\n"
        "Основные возможности:\n"
        "• Просмотр статуса подписки (кнопка «📊 Мой тариф и статус VPN»);\n"
        "• Активация бесплатного пробного периода (кнопка «🎁 Активировать бесплатный период»);\n"
        "• Получение конфигурации WireGuard (кнопка «🔐 Получить конфиг WireGuard»);\n"
        "• Информация о проекте (кнопка «ℹ️ О проекте»).\n"
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


@dp.message(F.text == "📊 Мой тариф и статус VPN")
async def handle_status(message: Message) -> None:
    """Показ текущего статуса подписки."""
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    try:
        data = await call_backend(
            method="GET",
            path=f"/api/v1/users/{user.id}/subscription/active",
        )
    except Exception:
        logger.exception("Ошибка при запросе статуса подписки")
        await message.answer("Ошибка при запросе статуса. Попробуйте позже.")
        return

    has_sub = data.get("has_active_subscription", False)
    is_trial_active = data.get("is_trial_active", False)
    ends_at = data.get("subscription_ends_at")
    plan_name = data.get("active_plan_name")
    trial_available = data.get("trial_available", False)

    lines = ["<b>Ваш статус VPN-подписки:</b>", ""]

    if has_sub:
        plan_str = plan_name or "активный тариф"
        lines.append(f"Текущий тариф: <b>{html.escape(plan_str)}</b>.")
        if is_trial_active:
            lines.append("Тип: <b>бесплатный пробный период</b>.")
        else:
            lines.append("Тип: <b>платная подписка</b>.")
        if ends_at:
            lines.append(f"Действует до: <code>{ends_at}</code>.")
    else:
        lines.append("У вас нет активной подписки.")
        if trial_available:
            lines.append("")
            lines.append("Вы можете активировать <b>бесплатный пробный период на 10 дней</b>.")
        else:
            lines.append("")
            lines.append("Бесплатный пробный период уже был использован ранее.")

    await message.answer("\n".join(lines), reply_markup=main_menu_keyboard())


@dp.message(F.text == "🎁 Активировать бесплатный период")
async def handle_activate_trial(message: Message) -> None:
    """Активация бесплатного пробного периода."""
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    try:
        data = await call_backend(
            method="POST",
            path=f"/api/v1/users/{user.id}/trial/activate",
        )
    except RuntimeError as exc:
        logger.warning("Ошибка от backend при активации trial: %s", exc)
        await message.answer(str(exc), reply_markup=main_menu_keyboard())
        return
    except Exception:
        logger.exception("Неожиданная ошибка при активации trial")
        await message.answer("Ошибка при активации пробного периода. Попробуйте позже.")
        return

    success = data.get("success", False)
    message_text = data.get("message", "Неизвестный ответ.")
    trial_ends_at = data.get("trial_ends_at")
    already_had_trial = data.get("already_had_trial", False)

    lines = []

    if success:
        lines.append("🎉 <b>Бесплатный пробный период активирован!</b>")
        if trial_ends_at:
            lines.append(f"Триал действует до: <code>{trial_ends_at}</code> (UTC).")
        lines.append("")
        lines.append(
            "Скоро здесь появится автоматическая выдача конфигурации WireGuard и выбор сервера/страны."
        )
    else:
        if already_had_trial:
            lines.append("❗ Бесплатный пробный период уже был использован ранее.")
        else:
            lines.append("Не удалось активировать бесплатный период.")
        if message_text:
            lines.append("")
            lines.append(html.escape(message_text))

    await message.answer("\n".join(lines), reply_markup=main_menu_keyboard())


@dp.message(F.text == "🔐 Получить конфиг WireGuard")
async def handle_get_wireguard_config(message: Message) -> None:
    """Запрос конфигурации WireGuard."""
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    await message.answer("⏳ Формируем конфигурацию WireGuard...")

    device_name = f"{(user.first_name or 'device')}_{user.id}"

    try:
        data = await call_backend(
            method="POST",
            path="/api/v1/vpn/peers/create",
            json={
                "telegram_id": user.id,
                "telegram_username": user.username,
                "device_name": device_name,
            },
        )
    except RuntimeError as exc:
        logger.warning("Ошибка от backend при создании VPN-пира: %s", exc)
        await message.answer(str(exc), reply_markup=main_menu_keyboard())
        return
    except Exception:
        logger.exception("Неожиданная ошибка при создании VPN-пира")
        await message.answer("Ошибка при обращении к серверу. Попробуйте позже.")
        return

    config_text = data.get("config")

    if not config_text:
        await message.answer(
            "Сервер вернул успешный статус, но без конфига. Это похоже на ошибку конфигурации backend.",
            reply_markup=main_menu_keyboard(),
        )
        return

    conf_escaped = html.escape(config_text)
    text = (
        "<b>Ваш конфиг WireGuard:</b>\n\n"
        f"<pre>{conf_escaped}</pre>\n\n"
        "⚠️ Если конфигурация не работает, напишите в поддержку."
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


@dp.message(F.text == "ℹ️ О проекте")
async def handle_about(message: Message) -> None:
    """Информация о проекте."""
    text = (
        "<b>О VPN-проекте</b>\n\n"
        "Этот сервис позволяет оформить подписку и получать доступ к VPN на базе WireGuard.\n"
        "Планируется:\n"
        "• оплата через Telegram Stars;\n"
        "• возможность выбора страны и сервера;\n"
        "• несколько тарифов (1, 2, 3 месяца) и бесплатный триал;\n"
        "• в будущем — веб-кабинет и гибкое управление устройствами.\n"
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


@dp.message()
async def handle_fallback(message: Message) -> None:
    """Обработка всех прочих сообщений."""
    await message.answer(
        "Я вас не понял. Пожалуйста, используйте кнопки меню или команды /start и /help.",
        reply_markup=main_menu_keyboard(),
    )


# ------------------------------------------------------
# Точка входа
# ------------------------------------------------------


async def main() -> None:
    """Запуск long-polling бота."""
    logger.info("Запуск VPN Telegram-бота...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
