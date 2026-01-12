"""
# ----------------------------------------------------------
# Версия файла: 1.8.0
# Описание: Telegram-бот для VPN-сервиса (тарифы/триал, управление WireGuard-устройствами,
#          повторная выдача конфигов, выдача QR-кода, инструкция подключения,
#          подготовка Stars оплаты + админ-панель платежей/подписок)
# Дата изменения: 2026-01-12
#
# Изменения (1.8.0):
#  - Добавлено админ-меню "🛡 Админ: Платежи/подписки" (видно только ADMIN_TELEGRAM_IDS)
#  - Добавлены админ-кнопки:
#      * "🧾 Планы (backend)" — список активных тарифов
#      * "🔎 Проверить подписку (TG ID)" — проверка статуса пользователя по Telegram ID
#      * "✅ Подтвердить Stars оплату (payload)" — ручное подтверждение платежа (backend /payments/stars/confirm)
#      * "🕘 Последний платёж" — показывает данные последнего successful_payment в боте
#  - Добавлен FSM-like ввод (через ожидание текста) для админ-действий
#  - Сохранение последнего платежа (in-memory) для быстрого админ-подтверждения
# ----------------------------------------------------------
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import io
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple

import httpx
import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# В docker compose сервис называется "backend"
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://backend:8000").strip()
BACKEND_TIMEOUT = float(os.getenv("BACKEND_TIMEOUT", "12.0"))
BACKEND_CONNECT_TIMEOUT = float(os.getenv("BACKEND_CONNECT_TIMEOUT", "3.5"))

# Лимит конфигов на пользователя:
#  - если 0 или меньше -> безлимит
MAX_CONFIGS_PER_USER = int(os.getenv("MAX_CONFIGS_PER_USER", "0"))

# TTL для callback-токенов
_CALLBACK_TTL_SEC = int(os.getenv("CALLBACK_TOKEN_TTL_SEC", "3600"))  # 1 час

# Параметры Stars оплаты
STARS_ENABLED = os.getenv("STARS_ENABLED", "1").strip() == "1"
STARS_CURRENCY = "XTR"  # Telegram Stars currency
STARS_PROVIDER_TOKEN = ""  # для Stars provider_token оставляют пустым
STARS_PAYLOAD_PREFIX = "vpn_plan:"
STARS_START_PARAMETER_PREFIX = "vpn_plan"

# Админы
ADMIN_TELEGRAM_IDS_RAW = (os.getenv("ADMIN_TELEGRAM_IDS") or "").strip()
ADMIN_TELEGRAM_IDS: set[int] = set()
if ADMIN_TELEGRAM_IDS_RAW:
    for part in ADMIN_TELEGRAM_IDS_RAW.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            ADMIN_TELEGRAM_IDS.add(int(p))
        except Exception:
            continue

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
# Простое состояние ввода (без FSM-хранилища)
# ------------------------------------------------------

@dataclass
class PendingInput:
    action: str
    created_ts: float


_pending_lock = asyncio.Lock()
_pending_by_user: dict[int, PendingInput] = {}
_PENDING_TTL = 600  # 10 минут


async def set_pending(user_id: int, action: str) -> None:
    async with _pending_lock:
        _pending_by_user[user_id] = PendingInput(action=action, created_ts=time.time())


async def pop_pending(user_id: int) -> Optional[PendingInput]:
    now = time.time()
    async with _pending_lock:
        pi = _pending_by_user.get(user_id)
        if not pi:
            return None
        if now - pi.created_ts > _PENDING_TTL:
            _pending_by_user.pop(user_id, None)
            return None
        _pending_by_user.pop(user_id, None)
        return pi


async def peek_pending(user_id: int) -> Optional[PendingInput]:
    now = time.time()
    async with _pending_lock:
        pi = _pending_by_user.get(user_id)
        if not pi:
            return None
        if now - pi.created_ts > _PENDING_TTL:
            _pending_by_user.pop(user_id, None)
            return None
        return pi


def is_admin(user_id: int) -> bool:
    return int(user_id) in ADMIN_TELEGRAM_IDS


# ------------------------------------------------------
# Безопасные callback токены (не храним client_id в callback_data напрямую)
# ------------------------------------------------------

_callback_lock = asyncio.Lock()
_callback_map: dict[str, Tuple[str, float]] = {}  # token -> (client_id, created_ts)


def _cleanup_callback_map(now: float) -> None:
    to_del = [k for k, (_, ts) in _callback_map.items() if now - ts > _CALLBACK_TTL_SEC]
    for k in to_del:
        _callback_map.pop(k, None)


async def register_client_id_for_callback(client_id: str) -> str:
    now = time.time()
    base = f"{client_id}|{now}".encode("utf-8", errors="replace")
    token = hashlib.sha256(base).hexdigest()[:16]
    async with _callback_lock:
        _cleanup_callback_map(now)
        _callback_map[token] = (client_id, now)
    return token


async def resolve_client_id_from_callback(token: str) -> Optional[str]:
    now = time.time()
    async with _callback_lock:
        _cleanup_callback_map(now)
        item = _callback_map.get(token)
        if not item:
            return None
        client_id, ts = item
        if now - ts > _CALLBACK_TTL_SEC:
            _callback_map.pop(token, None)
            return None
        return client_id


# ------------------------------------------------------
# UI: клавиатуры
# ------------------------------------------------------

def main_menu_keyboard(user_id: Optional[int] = None) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="📊 Статус подписки"), KeyboardButton(text="🎁 Активировать триал")],
        [KeyboardButton(text="🔐 Конфиги WireGuard"), KeyboardButton(text="📱 Устройства")],
        [KeyboardButton(text="📖 Инструкция подключения"), KeyboardButton(text="⭐ Купить подписку")],
        [KeyboardButton(text="ℹ️ О проекте")],
    ]

    if user_id is not None and is_admin(user_id):
        keyboard.append([KeyboardButton(text="🛡 Админ: Платежи/подписки")])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def admin_payments_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="🧾 Планы (backend)"), KeyboardButton(text="🔎 Проверить подписку (TG ID)")],
        [KeyboardButton(text="✅ Подтвердить Stars оплату (payload)"), KeyboardButton(text="🕘 Последний платёж")],
        [KeyboardButton(text="⬅️ Назад в меню")],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def _is_unlimited() -> bool:
    return MAX_CONFIGS_PER_USER <= 0


def devices_inline_keyboard(peers: list[dict[str, Any]]) -> InlineKeyboardMarkup:
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
        rows = [[InlineKeyboardButton(text="🔄 Обновить список", callback_data="devices:refresh")]]
    else:
        rows.append([InlineKeyboardButton(text="🔄 Обновить список", callback_data="devices:refresh")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def configs_inline_keyboard(peers: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    rows.append([InlineKeyboardButton(text="➕ Добавить устройство", callback_data="cfg:add")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить список", callback_data="cfg:refresh")])

    for p in peers:
        client_id = str(p.get("client_id", "")).strip()
        client_name = str(p.get("client_name", "")).strip() or "device"
        location_code = str(p.get("location_code", "")).strip()
        is_active_peer = bool(p.get("is_active", True))

        if not client_id:
            continue

        token = await register_client_id_for_callback(client_id)

        title = client_name
        if location_code:
            title += f" ({location_code})"
        if not is_active_peer:
            title += " ⛔"

        rows.append(
            [
                InlineKeyboardButton(text=f"⬇️ .conf: {title}", callback_data=f"cfg:dl:{token}"),
                InlineKeyboardButton(text=f"📷 QR: {title}", callback_data=f"cfg:qr:{token}"),
            ]
        )
        rows.append([InlineKeyboardButton(text=f"🗑 Удалить: {title}", callback_data=f"cfg:rv:{token}")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ------------------------------------------------------
# QR генерация
# ------------------------------------------------------

def build_qr_png_bytes(text: str) -> bytes:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    return bio.getvalue()


def safe_filename(name: str, default: str = "wireguard.conf") -> str:
    n = (name or "").strip()
    if not n:
        return default
    n = re.sub(r"[^0-9a-zA-Zа-яА-Я _\-\.\(\)]", "_", n)
    n = n.strip()
    if not n:
        return default
    if not n.lower().endswith(".conf"):
        n += ".conf"
    return n


# ------------------------------------------------------
# HTTP: backend клиент
# ------------------------------------------------------

class BackendError(RuntimeError):
    pass


def _extract_backend_detail(payload: Any, status_code: int) -> str:
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
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
# Текст инструкции
# ------------------------------------------------------

def build_instruction_text() -> str:
    lines = [
        "<b>Инструкция по подключению WireGuard</b>",
        "",
        "<b>Вариант A — через QR-код (быстрее)</b>",
        "1) Установите приложение <b>WireGuard</b>:",
        "   • Android: Google Play / RuStore (если доступно)",
        "   • iPhone: App Store",
        "   • Windows/macOS: с официального сайта WireGuard",
        "2) В боте откройте: <b>🔐 Конфиги WireGuard</b>",
        "3) Нажмите кнопку <b>📷 QR</b> напротив нужного устройства",
        "4) В приложении WireGuard нажмите <b>+</b> → <b>Сканировать QR-код</b>",
        "5) Сохраните туннель и включите переключатель (VPN ON).",
        "",
        "<b>Вариант B — через файл .conf</b>",
        "1) В боте откройте: <b>🔐 Конфиги WireGuard</b>",
        "2) Нажмите <b>⬇️ .conf</b> — бот пришлёт файл конфигурации",
        "3) Импортируйте конфиг:",
        "   • Android: WireGuard → <b>+</b> → <b>Импорт из файла</b> → выберите .conf",
        "   • iPhone: WireGuard → <b>+</b> → <b>Create from file or archive</b> → выберите .conf",
        "   • Windows: WireGuard → <b>Add Tunnel</b> → <b>Import tunnel(s) from file</b>",
        "   • macOS: WireGuard → <b>Import tunnel(s) from file</b>",
        "4) Включите туннель.",
        "",
        "<b>Если не подключается</b>",
        "• Проверьте, что туннель включён и нет другого VPN одновременно.",
        "• Попробуйте выключить/включить Wi-Fi/мобильную сеть.",
        "• Удалите туннель и импортируйте заново.",
        "• Если проблема сохраняется — напишите в поддержку (раздел «ℹ️ О проекте»).",
    ]
    return "\n".join(lines)


# ------------------------------------------------------
# Хранилище последнего платежа (in-memory)
# ------------------------------------------------------

_last_payment_lock = asyncio.Lock()
_last_payment: dict[str, Any] = {}


async def set_last_payment(data: dict[str, Any]) -> None:
    async with _last_payment_lock:
        _last_payment.clear()
        _last_payment.update(data)


async def get_last_payment() -> dict[str, Any]:
    async with _last_payment_lock:
        return dict(_last_payment)


# ------------------------------------------------------
# /start
# ------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
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

    greeting = [
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
        greeting.append("Тип: <b>бесплатный пробный период</b>." if is_trial_active else "Тип: <b>платная подписка</b>.")
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
async def cmd_help(message: Message) -> None:
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
async def cmd_instruction(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    await message.answer(build_instruction_text(), reply_markup=main_menu_keyboard(user_id))


# ------------------------------------------------------
# Админ: вход в меню платежей/подписок
# ------------------------------------------------------

@dp.message(F.text == "🛡 Админ: Платежи/подписки")
async def handle_admin_payments_menu(message: Message) -> None:
    user = message.from_user
    if user is None or not is_admin(user.id):
        await message.answer("Доступ запрещён.", reply_markup=main_menu_keyboard(user.id if user else None))
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
    user_id = message.from_user.id if message.from_user else None
    await message.answer("Главное меню.", reply_markup=main_menu_keyboard(user_id))


@dp.message(F.text == "🧾 Планы (backend)")
async def admin_plans(message: Message) -> None:
    user = message.from_user
    if user is None or not is_admin(user.id):
        await message.answer("Доступ запрещён.", reply_markup=main_menu_keyboard(user.id if user else None))
        return

    try:
        data = await call_backend(method="GET", path="/api/v1/subscription-plans/active")
    except Exception as exc:
        await message.answer(f"Ошибка загрузки планов: {html.escape(str(exc))}", reply_markup=admin_payments_keyboard())
        return

    plans = data.get("plans") or []
    if not isinstance(plans, list) or not plans:
        await message.answer("Планы не найдены.", reply_markup=admin_payments_keyboard())
        return

    lines = ["<b>Активные тарифы (backend)</b>", ""]
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
        lines.append(f"{flag} <b>{name}</b> — <code>{code}</code> — {days} дней — {stars} Stars — max_devices: {max_dev_str}")

    await message.answer("\n".join(lines), reply_markup=admin_payments_keyboard())


@dp.message(F.text == "🔎 Проверить подписку (TG ID)")
async def admin_check_sub_prompt(message: Message) -> None:
    user = message.from_user
    if user is None or not is_admin(user.id):
        await message.answer("Доступ запрещён.", reply_markup=main_menu_keyboard(user.id if user else None))
        return
    await set_pending(user.id, "admin_check_sub")
    await message.answer("Введите Telegram ID пользователя (число).", reply_markup=admin_payments_keyboard())


@dp.message(F.text == "✅ Подтвердить Stars оплату (payload)")
async def admin_confirm_payment_prompt(message: Message) -> None:
    user = message.from_user
    if user is None or not is_admin(user.id):
        await message.answer("Доступ запрещён.", reply_markup=main_menu_keyboard(user.id if user else None))
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
    user = message.from_user
    if user is None or not is_admin(user.id):
        await message.answer("Доступ запрещён.", reply_markup=main_menu_keyboard(user.id if user else None))
        return

    data = await get_last_payment()
    if not data:
        await message.answer("Пока нет сохранённых данных о платежах (successful_payment).", reply_markup=admin_payments_keyboard())
        return

    lines = ["<b>Последний successful_payment (в памяти бота)</b>", ""]
    for k in ("telegram_id", "currency", "total_amount", "invoice_payload", "telegram_payment_charge_id", "provider_payment_charge_id"):
        if k in data:
            lines.append(f"{html.escape(k)}: <code>{html.escape(str(data.get(k) or ''))}</code>")

    lines.append("")
    lines.append("Если нужно — используйте «✅ Подтвердить Stars оплату (payload)» и вставьте данные выше.")
    await message.answer("\n".join(lines), reply_markup=admin_payments_keyboard())


# ------------------------------------------------------
# Обработчик ввода админ-данных
# ------------------------------------------------------

@dp.message(F.text)
async def handle_text_inputs(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    pending = await peek_pending(user.id)
    if not pending:
        return

    # Снимаем pending сразу, чтобы не было повторов при ошибках
    pending = await pop_pending(user.id)
    if not pending:
        return

    text = (message.text or "").strip()

    if pending.action == "admin_check_sub":
        try:
            tid = int(text)
        except Exception:
            await message.answer("Ошибка: нужен Telegram ID числом. Повторите команду.", reply_markup=admin_payments_keyboard())
            return

        try:
            data = await call_backend(method="GET", path=f"/api/v1/users/{tid}/subscription/active")
        except Exception as exc:
            await message.answer(f"Ошибка запроса: {html.escape(str(exc))}", reply_markup=admin_payments_keyboard())
            return

        has_sub = bool(data.get("has_active_subscription", False))
        is_trial_active = bool(data.get("is_trial_active", False))
        ends_at = data.get("subscription_ends_at")
        plan_name = data.get("active_plan_name")
        trial_available = bool(data.get("trial_available", False))

        lines = [f"<b>Статус подписки пользователя</b> <code>{tid}</code>", ""]
        if has_sub:
            lines.append(f"Тариф: <b>{html.escape(str(plan_name or 'активный тариф'))}</b>")
            lines.append("Тип: <b>триал</b>" if is_trial_active else "Тип: <b>платная подписка</b>")
            if ends_at:
                lines.append(f"До: <code>{html.escape(str(ends_at))}</code> (UTC)")
            else:
                lines.append("До: <b>без ограничения</b>")
        else:
            lines.append("Активной подписки нет.")
            lines.append("Триал доступен: <b>да</b>" if trial_available else "Триал доступен: <b>нет</b>")

        await message.answer("\n".join(lines), reply_markup=admin_payments_keyboard())
        return

    if pending.action == "admin_confirm_payment":
        parts = text.split("|")
        if len(parts) != 5:
            await message.answer("Ошибка формата. Нужно 5 частей через |. Повторите команду.", reply_markup=admin_payments_keyboard())
            return

        raw_tid, invoice_payload, tg_charge_id, provider_charge_id, raw_amount = [p.strip() for p in parts]

        try:
            tid = int(raw_tid)
        except Exception:
            await message.answer("Ошибка: telegram_id должен быть числом.", reply_markup=admin_payments_keyboard())
            return

        try:
            amount = int(raw_amount)
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
            resp = await call_backend(method="POST", path="/api/v1/payments/stars/confirm", json=req)
        except Exception as exc:
            await message.answer(f"Ошибка подтверждения: {html.escape(str(exc))}", reply_markup=admin_payments_keyboard())
            return

        msg = resp.get("message") or "Готово."
        ok = bool(resp.get("success", True))
        await message.answer(
            f"{'✅' if ok else '⚠️'} {html.escape(str(msg))}",
            reply_markup=admin_payments_keyboard(),
        )
        return


# ------------------------------------------------------
# Статус / Триал / Инструкция / Конфиги / Устройства
# (оставлено как было, только reply_markup теперь с main_menu_keyboard(user.id))
# ------------------------------------------------------

@dp.message(F.text == "📊 Статус подписки")
async def handle_status(message: Message) -> None:
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    try:
        data = await call_backend(method="GET", path=f"/api/v1/users/{user.id}/subscription/active")
    except BackendError as exc:
        await message.answer(html.escape(str(exc)), reply_markup=main_menu_keyboard(user.id))
        return
    except Exception:
        logger.exception("Unexpected error in status")
        await message.answer("Ошибка при запросе статуса. Попробуйте позже.", reply_markup=main_menu_keyboard(user.id))
        return

    has_sub = bool(data.get("has_active_subscription", False))
    is_trial_active = bool(data.get("is_trial_active", False))
    ends_at = data.get("subscription_ends_at")
    plan_name = data.get("active_plan_name")
    trial_available = bool(data.get("trial_available", False))

    lines = ["<b>Ваш статус подписки:</b>", ""]

    if has_sub:
        plan_str = plan_name or "активный тариф"
        lines.append(f"Тариф: <b>{html.escape(str(plan_str))}</b>")
        lines.append("Тип: <b>триал</b>" if is_trial_active else "Тип: <b>платная подписка</b>")
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
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    try:
        data = await call_backend(method="POST", path=f"/api/v1/users/{user.id}/trial/activate")
    except BackendError as exc:
        await message.answer(html.escape(str(exc)), reply_markup=main_menu_keyboard(user.id))
        return
    except Exception:
        logger.exception("Unexpected error in trial")
        await message.answer("Ошибка при активации пробного периода. Попробуйте позже.", reply_markup=main_menu_keyboard(user.id))
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


@dp.message(F.text == "📖 Инструкция подключения")
async def handle_instruction_menu(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    await message.answer(build_instruction_text(), reply_markup=main_menu_keyboard(user_id))


@dp.message(F.text == "🔐 Конфиги WireGuard")
async def handle_configs(message: Message) -> None:
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    try:
        data = await call_backend(method="GET", path="/api/v1/vpn/peers/list", params={"telegram_id": user.id})
    except BackendError as exc:
        await message.answer(html.escape(str(exc)), reply_markup=main_menu_keyboard(user.id))
        return
    except Exception:
        logger.exception("Unexpected error in configs list")
        await message.answer("Ошибка при получении списка конфигов. Попробуйте позже.", reply_markup=main_menu_keyboard(user.id))
        return

    peers = data.get("peers") or []
    if not isinstance(peers, list):
        peers = []

    used = len(peers)
    if _is_unlimited():
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
    await message.answer("Управление конфигами:", reply_markup=await configs_inline_keyboard(peers))


@dp.callback_query(F.data == "cfg:refresh")
async def cb_configs_refresh(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    try:
        data = await call_backend(method="GET", path="/api/v1/vpn/peers/list", params={"telegram_id": user.id})
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
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=await configs_inline_keyboard(peers))
    except Exception:
        pass

    await callback.answer("Список обновлён.")


@dp.callback_query(F.data == "cfg:add")
async def cb_configs_add(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    try:
        data = await call_backend(method="GET", path="/api/v1/vpn/peers/list", params={"telegram_id": user.id})
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

    if (not _is_unlimited()) and len(peers) >= MAX_CONFIGS_PER_USER:
        await callback.answer(f"Лимит устройств: {MAX_CONFIGS_PER_USER}. Удалите старое устройство.", show_alert=True)
        return

    await callback.answer("Создаём новое устройство...")

    safe_first = (user.first_name or "device").strip()
    device_name = f"{safe_first}_{user.id}_{len(peers) + 1}"

    try:
        created = await call_backend(
            method="POST",
            path="/api/v1/vpn/peers/create",
            json={"telegram_id": user.id, "telegram_username": user.username, "device_name": device_name},
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
            await callback.message.answer("\n".join(meta_lines), reply_markup=main_menu_keyboard(user.id))

        await bot.send_document(
            chat_id=user.id,
            document=conf_file,
            caption="Файл конфигурации WireGuard (.conf).",
        )
        await bot.send_photo(
            chat_id=user.id,
            photo=qr_file,
            caption="QR-код для добавления туннеля в WireGuard.",
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

    try:
        data2 = await call_backend(method="GET", path="/api/v1/vpn/peers/list", params={"telegram_id": user.id})
        peers2 = data2.get("peers") or []
        if not isinstance(peers2, list):
            peers2 = []
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=await configs_inline_keyboard(peers2))
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

    filename = safe_filename(f"wg_{user.id}_{client_name}.conf", default=f"wg_{user.id}.conf")
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
            await callback.message.answer(f"<b>Ваш конфиг WireGuard:</b>\n\n<pre>{conf_escaped}</pre>\n")

    await callback.answer("Файл отправлен.")


@dp.callback_query(F.data.startswith("cfg:qr:"))
async def cb_configs_qr(callback: CallbackQuery) -> None:
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

    try:
        data = await call_backend(method="GET", path="/api/v1/vpn/peers/list", params={"telegram_id": user.id})
        peers = data.get("peers") or []
        if not isinstance(peers, list):
            peers = []
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=await configs_inline_keyboard(peers))
    except Exception:
        pass

    await callback.answer("Устройство удалено.")


@dp.message(F.text == "📱 Устройства")
async def handle_devices(message: Message) -> None:
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    try:
        data = await call_backend(method="GET", path="/api/v1/vpn/peers/list", params={"telegram_id": user.id})
    except BackendError as exc:
        await message.answer(html.escape(str(exc)), reply_markup=main_menu_keyboard(user.id))
        return
    except Exception:
        logger.exception("Unexpected error in devices list")
        await message.answer("Ошибка при получении списка устройств. Попробуйте позже.", reply_markup=main_menu_keyboard(user.id))
        return

    peers = data.get("peers") or []
    if not isinstance(peers, list):
        peers = []

    if not peers:
        await message.answer("У вас пока нет устройств. Откройте «🔐 Конфиги WireGuard» и добавьте устройство.", reply_markup=main_menu_keyboard(user.id))
        return

    lines = ["<b>Ваши устройства:</b>", ""]
    for i, p in enumerate(peers, start=1):
        client_name = html.escape(str(p.get("client_name") or "device"))
        client_id = html.escape(str(p.get("client_id") or ""))
        location_code = html.escape(str(p.get("location_code") or ""))
        is_active_peer = bool(p.get("is_active", True))
        status_ico = "✅" if is_active_peer else "⛔"
        lines.append(f"{i}. {status_ico} <b>{client_name}</b> — <code>{client_id}</code> ({location_code})")

    await message.answer("\n".join(lines), reply_markup=main_menu_keyboard(user.id))
    await message.answer("Отключение устройств:", reply_markup=devices_inline_keyboard(peers))


@dp.callback_query(F.data == "devices:refresh")
async def cb_refresh_devices(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    try:
        data = await call_backend(method="GET", path="/api/v1/vpn/peers/list", params={"telegram_id": user.id})
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

    try:
        data = await call_backend(method="GET", path="/api/v1/vpn/peers/list", params={"telegram_id": user.id})
        peers = data.get("peers") or []
        if not isinstance(peers, list):
            peers = []
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=devices_inline_keyboard(peers))
    except Exception:
        pass

    await callback.answer("Устройство отключено.")


# ------------------------------------------------------
# Stars: тарифы и оплата
# ------------------------------------------------------

async def fetch_active_plans() -> list[dict[str, Any]]:
    data = await call_backend(method="GET", path="/api/v1/subscription-plans/active")
    plans = data.get("plans") or []
    if not isinstance(plans, list):
        return []
    result = []
    for p in plans:
        if not isinstance(p, dict):
            continue
        if bool(p.get("is_trial", False)):
            continue
        if not bool(p.get("is_active", True)):
            continue
        result.append(p)
    return result


def plans_pay_inline_keyboard(plans: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for p in plans:
        code = str(p.get("code") or "").strip()
        name = str(p.get("name") or "Тариф").strip()
        price_stars = p.get("price_stars")
        if not code:
            continue

        try:
            stars_amount = int(float(str(price_stars)))
        except Exception:
            stars_amount = 0

        btn_text = f"⭐ {name} — {stars_amount} Stars"
        rows.append([InlineKeyboardButton(text=btn_text, callback_data=f"pay:{code}")])

    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="pay:refresh")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(F.text == "⭐ Купить подписку")
async def handle_buy_subscription(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not STARS_ENABLED:
        await message.answer("Оплата временно отключена. Попробуйте позже.", reply_markup=main_menu_keyboard(user_id))
        return

    try:
        plans = await fetch_active_plans()
    except BackendError as exc:
        await message.answer(html.escape(str(exc)), reply_markup=main_menu_keyboard(user_id))
        return
    except Exception:
        logger.exception("Plans load error")
        await message.answer("Не удалось загрузить тарифы. Попробуйте позже.", reply_markup=main_menu_keyboard(user_id))
        return

    if not plans:
        await message.answer("Активные тарифы не найдены. Попробуйте позже.", reply_markup=main_menu_keyboard(user_id))
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
    if not STARS_ENABLED:
        await callback.answer("Оплата отключена.", show_alert=True)
        return

    try:
        plans = await fetch_active_plans()
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
        plans = await fetch_active_plans()
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
        await callback.answer("Тариф не найден или отключён. Обновите список.", show_alert=True)
        return

    name = str(selected.get("name") or "VPN тариф").strip()
    price_stars = selected.get("price_stars")

    try:
        amount = int(float(str(price_stars)))
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
    try:
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    except Exception:
        logger.exception("pre_checkout answer failed")


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message) -> None:
    sp = message.successful_payment
    if sp is None:
        return

    payload = getattr(sp, "invoice_payload", "") or ""
    currency = getattr(sp, "currency", "") or ""
    total_amount = getattr(sp, "total_amount", None)
    tg_charge_id = getattr(sp, "telegram_payment_charge_id", "") or ""
    provider_charge_id = getattr(sp, "provider_payment_charge_id", "") or ""

    logger.info("SUCCESSFUL_PAYMENT: currency=%s amount=%s payload=%s", currency, total_amount, payload)

    # сохраняем в память бота для админа
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

    # Автоподтверждение на backend (если эндпоинт есть)
    # Если backend ещё не обновлён — будет BackendError, и мы просто покажем пользователю сообщение.
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
                _ = await call_backend(method="POST", path="/api/v1/payments/stars/confirm", json=req)
    except Exception as exc:
        logger.warning("Auto-confirm failed: %s", exc)

    await message.answer(
        "<b>Оплата получена.</b>\n\n"
        "Подписка будет активирована автоматически.\n"
        "Если в течение пары минут статус не изменится — напишите в поддержку.",
        reply_markup=main_menu_keyboard(message.from_user.id if message.from_user else None),
    )


# ------------------------------------------------------
# О проекте
# ------------------------------------------------------

@dp.message(F.text == "ℹ️ О проекте")
async def handle_about(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    limit_line = "без ограничений" if _is_unlimited() else str(MAX_CONFIGS_PER_USER)
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


# ------------------------------------------------------
# Fallback
# ------------------------------------------------------

@dp.message()
async def handle_fallback(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    await message.answer(
        "Команда не распознана. Используйте меню или /start, /help, /instruction.",
        reply_markup=main_menu_keyboard(user_id),
    )


# ------------------------------------------------------
# Точка входа
# ------------------------------------------------------

async def main() -> None:
    logger.info("Запуск VPN Telegram-бота (long-polling)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
