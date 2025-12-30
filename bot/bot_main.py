"""
Версия файла: 1.5.0
Описание: Telegram-бот для VPN-сервиса (меню тарифов, активация триала, управление устройствами, выдача WireGuard-конфига)
Дата изменения: 2025-12-29

Основное:
- /start: регистрация пользователя в backend, показ статуса.
- Кнопки:
  - 📊 Мой тариф и статус VPN
  - 🎁 Активировать бесплатный период
  - 🔐 Получить конфиг WireGuard
  - 📱 Мои устройства
  - ℹ️ О проекте
- Управление устройствами:
  - Список устройств через GET  /api/v1/vpn/peers/list?telegram_id=...
  - Отключение устройства через POST /api/v1/vpn/peers/revoke
- Обращения к backend:
  - POST /api/v1/users/from-telegram
  - GET  /api/v1/users/{telegram_id}/subscription/active
  - POST /api/v1/users/{telegram_id}/trial/activate
  - POST /api/v1/vpn/peers/create
  - GET  /api/v1/vpn/peers/list?telegram_id=...
  - POST /api/v1/vpn/peers/revoke

Важно:
- По умолчанию BACKEND_BASE_URL указывает на docker-compose service "backend" (http://backend:8000).
- Конфиг WireGuard отправляется как текст и как файл .conf (в Telegram документом), чтобы избежать ограничений длины.
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import os
from typing import Any, Optional

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# В docker compose сервис называется "backend" (см. docker compose config --services)
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://backend:8000").strip()
BACKEND_TIMEOUT = float(os.getenv("BACKEND_TIMEOUT", "12.0"))
BACKEND_CONNECT_TIMEOUT = float(os.getenv("BACKEND_CONNECT_TIMEOUT", "3.5"))

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
# UI: клавиатуры
# ------------------------------------------------------


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню бота."""
    keyboard = [
        [KeyboardButton(text="📊 Мой тариф и статус VPN")],
        [KeyboardButton(text="🎁 Активировать бесплатный период")],
        [KeyboardButton(text="🔐 Получить конфиг WireGuard")],
        [KeyboardButton(text="📱 Мои устройства")],
        [KeyboardButton(text="ℹ️ О проекте")],
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
    )


def devices_inline_keyboard(peers: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура для управления устройствами.
    Для каждого активного пира — кнопка «Отключить».
    """
    rows: list[list[InlineKeyboardButton]] = []
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
        rows.append([InlineKeyboardButton(text=btn_text, callback_data=f"revoke:{client_id}")])

    if not rows:
        rows = [[InlineKeyboardButton(text="Обновить список", callback_data="devices:refresh")]]
    else:
        rows.append([InlineKeyboardButton(text="🔄 Обновить список", callback_data="devices:refresh")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ------------------------------------------------------
# HTTP: универсальный клиент и обработка ошибок
# ------------------------------------------------------


class BackendError(RuntimeError):
    """Человекочитаемая ошибка backend для вывода пользователю."""


def _extract_backend_detail(payload: Any, status_code: int) -> str:
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        # иногда backend может вернуть message
        msg = payload.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    return f"Ошибка backend (HTTP {status_code})"


async def call_backend(
    method: str,
    path: str,
    json: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: Optional[float] = None,
) -> dict:
    """Универсальная функция для запросов к backend."""
    base = BACKEND_BASE_URL.rstrip("/")
    url = base + path
    logger.info("Backend request: %s %s", method.upper(), url)

    t = httpx.Timeout(timeout or BACKEND_TIMEOUT, connect=BACKEND_CONNECT_TIMEOUT)

    try:
        async with httpx.AsyncClient(timeout=t) as client:
            resp = await client.request(method=method, url=url, json=json, params=params)
    except httpx.ConnectError as exc:
        logger.warning("Backend connect error: %s", exc)
        raise BackendError("Сервер временно недоступен. Попробуйте позже.") from exc
    except httpx.TimeoutException as exc:
        logger.warning("Backend timeout: %s", exc)
        raise BackendError("Сервер отвечает слишком долго. Попробуйте позже.") from exc
    except Exception as exc:
        logger.exception("Backend unexpected error: %s", exc)
        raise BackendError("Ошибка соединения с сервером. Попробуйте позже.") from exc

    # Пытаемся разобрать JSON
    payload: Any
    try:
        payload = resp.json()
    except Exception:
        logger.warning("Backend returned non-JSON: %s", resp.text[:500])
        raise BackendError(f"Сервер вернул некорректный ответ (HTTP {resp.status_code}).")

    if resp.status_code >= 400:
        detail = _extract_backend_detail(payload, resp.status_code)
        logger.warning("Backend error %s: %s", resp.status_code, detail)
        raise BackendError(detail)

    if not isinstance(payload, dict):
        raise BackendError("Сервер вернул неожиданный формат данных.")
    return payload


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
    except BackendError as exc:
        await message.answer(
            "Произошла ошибка при обращении к серверу.\n"
            f"{html.escape(str(exc))}",
            reply_markup=main_menu_keyboard(),
        )
        return
    except Exception:
        logger.exception("Unexpected error in /start")
        await message.answer(
            "Произошла непредвиденная ошибка. Попробуйте ещё раз позже.",
            reply_markup=main_menu_keyboard(),
        )
        return

    greeting = [
        f"Привет, <b>{html.escape(user.full_name)}</b>.",
        "",
        "Это VPN-бот. Здесь можно:",
        "• посмотреть статус подписки;",
        "• активировать бесплатный пробный период (1 раз);",
        "• получить конфигурацию WireGuard;",
        "• управлять устройствами (посмотреть и отключить).",
        "",
    ]

    has_sub = bool(backend_resp.get("has_active_subscription", False))
    is_trial_active = bool(backend_resp.get("is_trial_active", False))
    ends_at = backend_resp.get("subscription_ends_at")
    trial_available = bool(backend_resp.get("trial_available", False))
    plan_name = backend_resp.get("active_plan_name")

    if has_sub:
        plan_label = plan_name or "активный тариф"
        greeting.append(f"Сейчас у вас есть <b>{html.escape(str(plan_label))}</b>.")
        greeting.append("Тип: <b>бесплатный пробный период</b>." if is_trial_active else "Тип: <b>платная подписка</b>.")
        if ends_at:
            greeting.append(f"Действует до: <code>{html.escape(str(ends_at))}</code>.")
    else:
        greeting.append("У вас пока нет активной подписки.")
        if trial_available:
            greeting.append("Вы можете активировать <b>бесплатный пробный период на 10 дней</b>.")

    greeting.append("")
    greeting.append("Выберите пункт меню ниже.")

    await message.answer("\n".join(greeting), reply_markup=main_menu_keyboard())


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "<b>Справка по боту</b>\n\n"
        "Основные возможности:\n"
        "• «📊 Мой тариф и статус VPN» — текущий тариф и срок действия;\n"
        "• «🎁 Активировать бесплатный период» — триал на 10 дней (один раз);\n"
        "• «🔐 Получить конфиг WireGuard» — выдача/обновление конфига (только при активной подписке);\n"
        "• «📱 Мои устройства» — список устройств и возможность отключить;\n"
        "• «ℹ️ О проекте» — информация о сервисе.\n"
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


@dp.message(F.text == "📊 Мой тариф и статус VPN")
async def handle_status(message: Message) -> None:
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    try:
        data = await call_backend(
            method="GET",
            path=f"/api/v1/users/{user.id}/subscription/active",
        )
    except BackendError as exc:
        await message.answer(html.escape(str(exc)), reply_markup=main_menu_keyboard())
        return
    except Exception:
        logger.exception("Unexpected error in status")
        await message.answer("Ошибка при запросе статуса. Попробуйте позже.", reply_markup=main_menu_keyboard())
        return

    has_sub = bool(data.get("has_active_subscription", False))
    is_trial_active = bool(data.get("is_trial_active", False))
    ends_at = data.get("subscription_ends_at")
    plan_name = data.get("active_plan_name")
    trial_available = bool(data.get("trial_available", False))

    lines = ["<b>Ваш статус VPN-подписки:</b>", ""]

    if has_sub:
        plan_str = plan_name or "активный тариф"
        lines.append(f"Текущий тариф: <b>{html.escape(str(plan_str))}</b>.")
        lines.append("Тип: <b>бесплатный пробный период</b>." if is_trial_active else "Тип: <b>платная подписка</b>.")
        if ends_at:
            lines.append(f"Действует до: <code>{html.escape(str(ends_at))}</code>.")
    else:
        lines.append("У вас нет активной подписки.")
        if trial_available:
            lines.append("")
            lines.append("Можно активировать <b>бесплатный пробный период на 10 дней</b>.")
        else:
            lines.append("")
            lines.append("Бесплатный пробный период уже был использован ранее.")

    await message.answer("\n".join(lines), reply_markup=main_menu_keyboard())


@dp.message(F.text == "🎁 Активировать бесплатный период")
async def handle_activate_trial(message: Message) -> None:
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    try:
        data = await call_backend(
            method="POST",
            path=f"/api/v1/users/{user.id}/trial/activate",
        )
    except BackendError as exc:
        await message.answer(html.escape(str(exc)), reply_markup=main_menu_keyboard())
        return
    except Exception:
        logger.exception("Unexpected error in trial")
        await message.answer("Ошибка при активации пробного периода. Попробуйте позже.", reply_markup=main_menu_keyboard())
        return

    success = bool(data.get("success", False))
    message_text = str(data.get("message", ""))
    trial_ends_at = data.get("trial_ends_at")
    already_had_trial = bool(data.get("already_had_trial", False))

    lines: list[str] = []

    if success:
        lines.append("<b>Бесплатный пробный период активирован.</b>")
        if trial_ends_at:
            lines.append(f"Действует до: <code>{html.escape(str(trial_ends_at))}</code> (UTC).")
        lines.append("")
        lines.append("Теперь вы можете получить конфиг WireGuard кнопкой «🔐 Получить конфиг WireGuard».")
    else:
        if already_had_trial:
            lines.append("Бесплатный пробный период уже был использован ранее.")
        else:
            lines.append("Не удалось активировать бесплатный период.")
        if message_text:
            lines.append("")
            lines.append(html.escape(message_text))

    await message.answer("\n".join(lines), reply_markup=main_menu_keyboard())


@dp.message(F.text == "🔐 Получить конфиг WireGuard")
async def handle_get_wireguard_config(message: Message) -> None:
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    await message.answer("⏳ Формируем конфигурацию WireGuard...")

    # имя устройства должно быть стабильным и читабельным
    safe_first = (user.first_name or "device").strip()
    device_name = f"{safe_first}_{user.id}"

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
    except BackendError as exc:
        await message.answer(html.escape(str(exc)), reply_markup=main_menu_keyboard())
        return
    except Exception:
        logger.exception("Unexpected error in create peer")
        await message.answer("Ошибка при обращении к серверу. Попробуйте позже.", reply_markup=main_menu_keyboard())
        return

    config_text = data.get("config")
    client_name = data.get("client_name") or device_name
    location_code = data.get("location_code") or ""
    location_name = data.get("location_name") or ""

    if not config_text:
        await message.answer(
            "Сервер вернул успешный статус, но без конфига. Это похоже на ошибку backend.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # 1) короткое сообщение с метаданными
    meta_lines = [
        "<b>Конфиг WireGuard готов.</b>",
        f"Устройство: <b>{html.escape(str(client_name))}</b>",
    ]
    if location_code or location_name:
        meta_lines.append(f"Локация: <code>{html.escape(str(location_code))}</code> {html.escape(str(location_name))}".strip())
    await message.answer("\n".join(meta_lines), reply_markup=main_menu_keyboard())

    # 2) отправка как файл .conf
    filename = f"wg_{user.id}.conf"
    file_bytes = config_text.encode("utf-8", errors="replace")
    bio = io.BytesIO(file_bytes)
    bio.name = filename

    try:
        await bot.send_document(
            chat_id=message.chat.id,
            document=InputFile(bio, filename=filename),
            caption="Файл конфигурации WireGuard (.conf).",
        )
    except Exception:
        # fallback: отправим текстом (может быть длинно, но обычно влезает)
        logger.exception("Failed to send document, fallback to text")
        conf_escaped = html.escape(str(config_text))
        text = (
            "<b>Ваш конфиг WireGuard:</b>\n\n"
            f"<pre>{conf_escaped}</pre>\n\n"
            "Если конфигурация не работает — напишите в поддержку."
        )
        await message.answer(text, reply_markup=main_menu_keyboard())


@dp.message(F.text == "📱 Мои устройства")
async def handle_devices(message: Message) -> None:
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    try:
        data = await call_backend(
            method="GET",
            path="/api/v1/vpn/peers/list",
            params={"telegram_id": user.id},
        )
    except BackendError as exc:
        await message.answer(html.escape(str(exc)), reply_markup=main_menu_keyboard())
        return
    except Exception:
        logger.exception("Unexpected error in devices list")
        await message.answer("Ошибка при получении списка устройств. Попробуйте позже.", reply_markup=main_menu_keyboard())
        return

    peers = data.get("peers") or []
    if not isinstance(peers, list):
        peers = []

    if not peers:
        await message.answer("У вас пока нет устройств. Сначала получите конфиг WireGuard.", reply_markup=main_menu_keyboard())
        return

    lines = ["<b>Ваши устройства:</b>", ""]
    # показываем кратко
    for i, p in enumerate(peers, start=1):
        client_name = html.escape(str(p.get("client_name") or "device"))
        client_id = html.escape(str(p.get("client_id") or ""))
        location_code = html.escape(str(p.get("location_code") or ""))
        is_active = bool(p.get("is_active", True))
        status_ico = "✅" if is_active else "⛔"
        lines.append(f"{i}. {status_ico} <b>{client_name}</b> — <code>{client_id}</code> ({location_code})")

    await message.answer(
        "\n".join(lines),
        reply_markup=main_menu_keyboard(),
    )

    # отдельным сообщением инлайн-кнопки для отключения
    await message.answer(
        "Управление устройствами:",
        reply_markup=devices_inline_keyboard(peers),
    )


@dp.callback_query(F.data == "devices:refresh")
async def cb_refresh_devices(callback: CallbackQuery) -> None:
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
        await callback.message.edit_reply_markup(reply_markup=devices_inline_keyboard(peers))
    except Exception:
        # если нельзя отредактировать — просто ответим
        pass

    await callback.answer("Список обновлён.")


@dp.callback_query(F.data.startswith("revoke:"))
async def cb_revoke_device(callback: CallbackQuery) -> None:
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

    # обновим клавиатуру
    try:
        data = await call_backend(
            method="GET",
            path="/api/v1/vpn/peers/list",
            params={"telegram_id": user.id},
        )
        peers = data.get("peers") or []
        if not isinstance(peers, list):
            peers = []
        try:
            await callback.message.edit_reply_markup(reply_markup=devices_inline_keyboard(peers))
        except Exception:
            pass
    except Exception:
        pass

    await callback.answer("Устройство отключено.")


@dp.message(F.text == "ℹ️ О проекте")
async def handle_about(message: Message) -> None:
    text = (
        "<b>О VPN-проекте</b>\n\n"
        "Сервис предоставляет доступ к VPN на базе WireGuard.\n\n"
        "Планируется:\n"
        "• оплата через Telegram Stars;\n"
        "• выбор страны/сервера;\n"
        "• несколько тарифов (1/2/3 месяца) и бесплатный триал;\n"
        "• веб-кабинет и расширенное управление устройствами.\n"
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


@dp.message()
async def handle_fallback(message: Message) -> None:
    await message.answer(
        "Команда не распознана. Используйте меню или /start, /help.",
        reply_markup=main_menu_keyboard(),
    )


# ------------------------------------------------------
# Точка входа
# ------------------------------------------------------


async def main() -> None:
    logger.info("Запуск VPN Telegram-бота (long-polling)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
