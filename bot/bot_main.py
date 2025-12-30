# ----------------------------------------------------------
# Версия файла: 1.7.1
# Описание: Telegram-бот для VPN-сервиса (статус подписки, активация триала,
#           меню тарифов, выдача WireGuard-конфига, управление устройствами)
# Дата изменения: 2025-12-30
# Изменения (1.7.1):
#  - Исправлен баг в _PLAN_MAP: при token-ветке искался ключ token, но план сохранялся по code.
#    Теперь для plan_buy_t:* корректно восстанавливаем plan/code.
#  - Добавлено закрытие HTTP-клиента через dp.shutdown.register с сигнатурой (dispatcher),
#    чтобы корректно работать в aiogram v3 (и не упасть при вызове shutdown callbacks).
#  - Добавлена нормализация и очистка in-memory карт (ограничение размера) для защиты от разрастания памяти.
#  - Улучшены сообщения об ошибках/логирование; добавлен validate BACKEND_BASE_URL.
#  - Устранены потенциальные проблемы: использование HTML escape в сообщениях, безопасные pre-блоки,
#    защита от пустых/невалидных данных.
# ----------------------------------------------------------

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
from typing import Any, Optional

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

# В docker compose сервис обычно называется "backend"
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://backend:8000").strip()

BACKEND_TIMEOUT = float(os.getenv("BACKEND_TIMEOUT", "12.0"))
BACKEND_CONNECT_TIMEOUT = float(os.getenv("BACKEND_CONNECT_TIMEOUT", "3.5"))

# Telegram limits
TG_MSG_LIMIT = 4096
TG_CALLBACK_LIMIT = 64

# Пределы на in-memory мапы (защита от утечки памяти при большом количестве действий)
MAX_TOKEN_MAP_PER_USER = int(os.getenv("MAX_TOKEN_MAP_PER_USER", "200"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в окружении бота.")

if not BACKEND_BASE_URL.startswith(("http://", "https://")):
    raise RuntimeError("BACKEND_BASE_URL должен начинаться с http:// или https://")

# ------------------------------------------------------
# Инициализация бота и диспетчера (aiogram v3.x)
# ------------------------------------------------------

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()

# ------------------------------------------------------
# Runtime storage (in-memory)
# ------------------------------------------------------
# Маппинг токенов callback -> client_id, чтобы не превышать лимит callback_data
# Формат: {telegram_id: {token: client_id}}
_REVOKE_TOKEN_MAP: dict[int, dict[str, str]] = {}

# Маппинг тарифов (key -> plan_dict) для пользователя.
# Ключом может быть как code, так и token (для plan_buy_t).
# Формат: {telegram_id: {key: plan_dict}}
_PLAN_MAP: dict[int, dict[str, dict[str, Any]]] = {}

# ------------------------------------------------------
# HTTP client (reused)
# ------------------------------------------------------

_http_client: Optional[httpx.AsyncClient] = None


def _get_http_timeout(timeout: Optional[float] = None) -> httpx.Timeout:
    return httpx.Timeout(timeout or BACKEND_TIMEOUT, connect=BACKEND_CONNECT_TIMEOUT)


async def _ensure_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=_get_http_timeout())
    return _http_client


async def _close_http_client() -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


# ------------------------------------------------------
# UI: клавиатуры
# ------------------------------------------------------

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню бота."""
    keyboard = [
        [KeyboardButton(text="📊 Мой тариф и статус VPN")],
        [KeyboardButton(text="💳 Тарифы и оплата")],
        [KeyboardButton(text="🎁 Активировать бесплатный период")],
        [KeyboardButton(text="🔐 Получить конфиг WireGuard")],
        [KeyboardButton(text="📱 Мои устройства")],
        [KeyboardButton(text="ℹ️ О проекте")],
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
    )


def _trim_user_map(user_map: dict[str, Any]) -> None:
    """
    Ограничивает размер пользовательского словаря (на случай, если пользователь кликает бесконечно).
    Простая стратегия: если больше MAX_TOKEN_MAP_PER_USER — удаляем самые "старые" элементы по порядку вставки.
    (В Python 3.7+ dict сохраняет порядок вставки.)
    """
    if MAX_TOKEN_MAP_PER_USER <= 0:
        return
    while len(user_map) > MAX_TOKEN_MAP_PER_USER:
        try:
            first_key = next(iter(user_map.keys()))
            user_map.pop(first_key, None)
        except StopIteration:
            break


def _make_revoke_callback_data(telegram_id: int, client_id: str) -> str:
    """
    Генерирует callback_data для revoke с учетом лимита Telegram (64 байта).
    Если client_id не влезает — заменяем на токен и сохраняем в памяти.
    """
    raw = f"revoke:{client_id}"
    if len(raw.encode("utf-8")) <= TG_CALLBACK_LIMIT:
        return raw

    token = hashlib.sha1(client_id.encode("utf-8", errors="ignore")).hexdigest()[:12]
    user_map = _REVOKE_TOKEN_MAP.setdefault(telegram_id, {})
    user_map[token] = client_id
    _trim_user_map(user_map)
    return f"revoke_t:{token}"


def devices_inline_keyboard(telegram_id: int, peers: list[dict[str, Any]]) -> InlineKeyboardMarkup:
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

        cb_data = _make_revoke_callback_data(telegram_id=telegram_id, client_id=client_id)
        rows.append([InlineKeyboardButton(text=btn_text, callback_data=cb_data)])

    if not rows:
        rows = [[InlineKeyboardButton(text="Обновить список", callback_data="devices:refresh")]]
    else:
        rows.append([InlineKeyboardButton(text="🔄 Обновить список", callback_data="devices:refresh")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def plans_inline_keyboard(telegram_id: int, plans: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура тарифов. Кнопки "Купить" (пока заглушка под Stars).
    callback_data: plan_buy:<code> или plan_buy_t:<token> если слишком длинно.
    """
    rows: list[list[InlineKeyboardButton]] = []
    user_map = _PLAN_MAP.setdefault(telegram_id, {})

    for p in plans:
        code = str(p.get("code", "")).strip()
        name = str(p.get("name", "")).strip() or code or "plan"
        is_active = bool(p.get("is_active", True))

        if not code:
            continue
        if not is_active:
            continue

        # Сохраняем план в память по code (для обычной ветки plan_buy:<code>)
        user_map[code] = p

        btn_text = f"Купить: {name}"
        cb_data = f"plan_buy:{code}"

        # лимит callback 64 байта
        if len(cb_data.encode("utf-8")) > TG_CALLBACK_LIMIT:
            # на всякий случай: токенизируем code и сохраняем план по token тоже
            token = hashlib.sha1(code.encode("utf-8", errors="ignore")).hexdigest()[:12]
            user_map[token] = p
            cb_data = f"plan_buy_t:{token}"

        rows.append([InlineKeyboardButton(text=btn_text, callback_data=cb_data)])

    _trim_user_map(user_map)

    if not rows:
        rows = [[InlineKeyboardButton(text="Обновить тарифы", callback_data="plans:refresh")]]
    else:
        rows.append([InlineKeyboardButton(text="🔄 Обновить тарифы", callback_data="plans:refresh")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ------------------------------------------------------
# HTTP: обработка ошибок backend
# ------------------------------------------------------

class BackendError(RuntimeError):
    """Человекочитаемая ошибка backend для вывода пользователю."""


def _extract_backend_detail(payload: Any, status_code: int) -> str:
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()

        msg = payload.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()

        err = payload.get("error")
        if isinstance(err, str) and err.strip():
            return err.strip()

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

    client = await _ensure_http_client()

    try:
        resp = await client.request(
            method=method,
            url=url,
            json=json,
            params=params,
            timeout=_get_http_timeout(timeout),
        )
    except httpx.ConnectError as exc:
        logger.warning("Backend connect error: %s", exc)
        raise BackendError("Сервер временно недоступен. Попробуйте позже.") from exc
    except httpx.TimeoutException as exc:
        logger.warning("Backend timeout: %s", exc)
        raise BackendError("Сервер отвечает слишком долго. Попробуйте позже.") from exc
    except Exception as exc:
        logger.exception("Backend unexpected error: %s", exc)
        raise BackendError("Ошибка соединения с сервером. Попробуйте позже.") from exc

    payload: Any
    try:
        payload = resp.json()
    except Exception:
        snippet = (resp.text or "")[:500]
        logger.warning("Backend returned non-JSON (HTTP %s): %s", resp.status_code, snippet)
        raise BackendError(f"Сервер вернул некорректный ответ (HTTP {resp.status_code}).")

    if resp.status_code >= 400:
        detail = _extract_backend_detail(payload, resp.status_code)
        logger.warning("Backend error %s: %s", resp.status_code, detail)
        raise BackendError(detail)

    if not isinstance(payload, dict):
        raise BackendError("Сервер вернул неожиданный формат данных.")

    return payload


# ------------------------------------------------------
# Helpers
# ------------------------------------------------------

_DEVICE_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-\.]+")


def make_safe_device_name(first_name: Optional[str], telegram_id: int) -> str:
    """
    Делает стабильное, читабельное и безопасное имя устройства.
    Ограничиваем длину, убираем проблемные символы.
    """
    base = (first_name or "device").strip()
    if not base:
        base = "device"

    base = base.replace(" ", "_")
    base = _DEVICE_SAFE_RE.sub("", base)
    if not base:
        base = "device"

    base = base[:24]
    return f"{base}_{telegram_id}"


def truncate_for_tg(text: str, limit: int = TG_MSG_LIMIT) -> str:
    if len(text) <= limit:
        return text
    cut = max(0, limit - 80)
    return text[:cut] + "\n...\n(Сообщение обрезано. Используйте файл .conf)"


async def fetch_plans_from_backend() -> list[dict[str, Any]]:
    """
    Пытаемся получить тарифы из backend.
    Поддерживаем несколько путей (на случай разных реализаций):
      - /api/v1/subscription-plans/active
      - /api/v1/subscription-plans
      - /api/v1/plans/active
    Ожидаем ответ:
      - {"plans": [...]} или {"items": [...]} или {"data": [...]} — нормализуем.
    """
    candidate_paths = [
        "/api/v1/subscription-plans/active",
        "/api/v1/subscription-plans",
        "/api/v1/plans/active",
    ]

    last_error: Optional[str] = None

    for path in candidate_paths:
        try:
            data = await call_backend(method="GET", path=path)
        except BackendError as exc:
            last_error = str(exc)
            continue
        except Exception as exc:
            last_error = f"unexpected error: {exc}"
            continue

        plans: Any = None
        if isinstance(data, dict):
            if "plans" in data:
                plans = data.get("plans")
            elif "items" in data:
                plans = data.get("items")
            elif "data" in data:
                plans = data.get("data")

        if isinstance(plans, list):
            result: list[dict[str, Any]] = []
            for p in plans:
                if isinstance(p, dict):
                    result.append(p)
            return result

        if isinstance(data, list):
            result2: list[dict[str, Any]] = []
            for p in data:
                if isinstance(p, dict):
                    result2.append(p)
            return result2

        last_error = "Сервер вернул неожиданный формат тарифов."

    raise BackendError(
        "Не удалось получить список тарифов. "
        "Вероятно, в backend ещё не реализован публичный эндпоинт тарифов. "
        f"Последняя ошибка: {last_error or 'нет деталей'}"
    )


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
        "• выбрать тариф и подготовиться к оплате;",
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
        "• «💳 Тарифы и оплата» — список тарифов (подготовка к оплате/Stars);\n"
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


@dp.message(F.text == "💳 Тарифы и оплата")
async def handle_plans(message: Message) -> None:
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя Telegram.", reply_markup=main_menu_keyboard())
        return

    await message.answer("⏳ Загружаем тарифы...")

    try:
        plans = await fetch_plans_from_backend()
    except BackendError as exc:
        text = (
            "<b>Тарифы пока недоступны</b>\n\n"
            f"{html.escape(str(exc))}\n\n"
            "Что нужно сделать:\n"
            "• добавить в backend публичный эндпоинт тарифов (например /api/v1/subscription-plans/active);\n"
            "• возвращать список тарифов (code, name, duration_days, price_stars, max_devices, is_active).\n"
        )
        await message.answer(text, reply_markup=main_menu_keyboard())
        return
    except Exception:
        logger.exception("Unexpected error in plans")
        await message.answer("Ошибка при загрузке тарифов. Попробуйте позже.", reply_markup=main_menu_keyboard())
        return

    if not plans:
        await message.answer("Список тарифов пуст. Обратитесь в поддержку.", reply_markup=main_menu_keyboard())
        return

    lines: list[str] = ["<b>Доступные тарифы:</b>", ""]
    normalized: list[dict[str, Any]] = []

    for p in plans:
        if not isinstance(p, dict):
            continue
        if not bool(p.get("is_active", True)):
            continue

        code = str(p.get("code", "")).strip()
        name = str(p.get("name", "")).strip() or code
        duration_days = p.get("duration_days")
        price_stars = p.get("price_stars")
        max_devices = p.get("max_devices")

        if not code:
            continue

        normalized.append(p)

        dur_str = f"{duration_days} дн." if isinstance(duration_days, int) else "—"
        price_str = f"{price_stars} ⭐" if isinstance(price_stars, (int, float)) else "—"
        dev_str = "безлимит устройств" if max_devices in (None, 0, "") else f"до {max_devices} устройств"

        lines.append(f"• <b>{html.escape(name)}</b> (<code>{html.escape(code)}</code>)")
        lines.append(
            f"  Срок: <b>{html.escape(str(dur_str))}</b> | "
            f"Цена: <b>{html.escape(str(price_str))}</b> | "
            f"{html.escape(str(dev_str))}"
        )
        lines.append("")

    if not normalized:
        await message.answer("Нет активных тарифов. Обратитесь в поддержку.", reply_markup=main_menu_keyboard())
        return

    await message.answer("\n".join(lines).strip(), reply_markup=main_menu_keyboard())

    await message.answer(
        "Выберите тариф для оплаты (Stars будет подключено на следующем шаге):",
        reply_markup=plans_inline_keyboard(telegram_id=user.id, plans=normalized),
    )


@dp.callback_query(F.data == "plans:refresh")
async def cb_refresh_plans(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    try:
        plans = await fetch_plans_from_backend()
    except BackendError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception:
        logger.exception("Unexpected error in refresh plans")
        await callback.answer("Ошибка обновления тарифов.", show_alert=True)
        return

    if not isinstance(plans, list):
        plans = []

    try:
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=plans_inline_keyboard(telegram_id=user.id, plans=plans))
    except Exception:
        pass

    await callback.answer("Тарифы обновлены.")


def _resolve_plan(telegram_id: int, callback_data: str) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """
    Возвращает (plan_code, plan_dict) по callback_data.
    Поддерживает:
      - plan_buy:<code>
      - plan_buy_t:<token> (plan_dict берется из in-memory map)
    """
    user_map = _PLAN_MAP.get(telegram_id, {})

    if callback_data.startswith("plan_buy:"):
        code = callback_data.split("plan_buy:", 1)[-1].strip()
        if not code:
            return None, None
        plan = user_map.get(code)
        return code, plan

    if callback_data.startswith("plan_buy_t:"):
        token = callback_data.split("plan_buy_t:", 1)[-1].strip()
        if not token:
            return None, None
        plan = user_map.get(token)
        if not isinstance(plan, dict):
            return None, None
        code = str(plan.get("code", "")).strip() or None
        return code, plan

    return None, None


@dp.callback_query(F.data.startswith("plan_buy:") | F.data.startswith("plan_buy_t:"))
async def cb_plan_buy(callback: CallbackQuery) -> None:
    """
    Заглушка под оплату Stars.
    На следующем шаге здесь будет генерация invoice/Stars и запись платежа в backend.
    """
    user = callback.from_user
    if user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    data = callback.data or ""
    code, plan = _resolve_plan(telegram_id=user.id, callback_data=data)
    if not code:
        await callback.answer("Некорректная кнопка. Обновите тарифы.", show_alert=True)
        return

    if not isinstance(plan, dict):
        plan = {}

    name = str(plan.get("name", "")).strip() or code
    price = plan.get("price_stars")
    duration = plan.get("duration_days")
    max_devices = plan.get("max_devices")

    price_str = f"{price} ⭐" if isinstance(price, (int, float)) else "—"
    dur_str = f"{duration} дней" if isinstance(duration, int) else "—"
    dev_str = "безлимит устройств" if max_devices in (None, 0, "") else f"до {max_devices} устройств"

    text = (
        "<b>Оплата тарифа (в разработке)</b>\n\n"
        f"Тариф: <b>{html.escape(name)}</b>\n"
        f"Код: <code>{html.escape(code)}</code>\n"
        f"Срок: <b>{html.escape(dur_str)}</b>\n"
        f"Лимит: {html.escape(dev_str)}\n"
        f"Цена: <b>{html.escape(price_str)}</b>\n\n"
        "Следующий шаг:\n"
        "• подключаем оплату через Telegram Stars;\n"
        "• backend будет создавать подписку после успешного платежа.\n"
    )

    if callback.message:
        await callback.message.answer(text, reply_markup=main_menu_keyboard())
    await callback.answer("Оплата будет подключена на следующем шаге.")


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

    device_name = make_safe_device_name(user.first_name, user.id)

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

    meta_lines = [
        "<b>Конфиг WireGuard готов.</b>",
        f"Устройство: <b>{html.escape(str(client_name))}</b>",
    ]
    if location_code or location_name:
        loc = f"{str(location_code).strip()} {str(location_name).strip()}".strip()
        meta_lines.append(f"Локация: <code>{html.escape(loc)}</code>")
    await message.answer("\n".join(meta_lines), reply_markup=main_menu_keyboard())

    filename = f"wg_{user.id}.conf"
    file_bytes = str(config_text).encode("utf-8", errors="replace")
    doc = BufferedInputFile(file_bytes, filename=filename)

    try:
        await bot.send_document(
            chat_id=message.chat.id,
            document=doc,
            caption="Файл конфигурации WireGuard (.conf).",
        )
        return
    except Exception:
        logger.exception("Failed to send document, fallback to text")

    conf_escaped = html.escape(str(config_text))
    text = "<b>Ваш конфиг WireGuard:</b>\n\n" + f"<pre>{conf_escaped}</pre>"
    text = truncate_for_tg(text, TG_MSG_LIMIT)

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
    for i, p in enumerate(peers, start=1):
        client_name = html.escape(str(p.get("client_name") or "device"))
        client_id = html.escape(str(p.get("client_id") or ""))
        location_code = html.escape(str(p.get("location_code") or ""))
        is_active = bool(p.get("is_active", True))
        status_ico = "✅" if is_active else "⛔"
        loc = f" ({location_code})" if location_code else ""
        lines.append(f"{i}. {status_ico} <b>{client_name}</b> — <code>{client_id}</code>{loc}")

    await message.answer("\n".join(lines), reply_markup=main_menu_keyboard())

    await message.answer(
        "Управление устройствами:",
        reply_markup=devices_inline_keyboard(telegram_id=user.id, peers=peers),
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
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=devices_inline_keyboard(telegram_id=user.id, peers=peers))
    except Exception:
        pass

    await callback.answer("Список обновлён.")


def _resolve_revoke_client_id(telegram_id: int, callback_data: str) -> Optional[str]:
    """
    Извлекает client_id из callback_data.
    Поддерживает:
      - revoke:<client_id>
      - revoke_t:<token>  (client_id берется из in-memory map)
    """
    if callback_data.startswith("revoke:"):
        client_id = callback_data.split("revoke:", 1)[-1].strip()
        return client_id or None

    if callback_data.startswith("revoke_t:"):
        token = callback_data.split("revoke_t:", 1)[-1].strip()
        if not token:
            return None
        user_map = _REVOKE_TOKEN_MAP.get(telegram_id, {})
        return user_map.get(token)

    return None


@dp.callback_query(F.data.startswith("revoke:") | F.data.startswith("revoke_t:"))
async def cb_revoke_device(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    data = callback.data or ""
    client_id = _resolve_revoke_client_id(telegram_id=user.id, callback_data=data)
    if not client_id:
        await callback.answer("Некорректная кнопка/устройство. Обновите список.", show_alert=True)
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
        new_data = await call_backend(
            method="GET",
            path="/api/v1/vpn/peers/list",
            params={"telegram_id": user.id},
        )
        peers = new_data.get("peers") or []
        if not isinstance(peers, list):
            peers = []
        try:
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=devices_inline_keyboard(telegram_id=user.id, peers=peers))
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
# Lifecycle
# ------------------------------------------------------

async def on_shutdown(_dispatcher: Dispatcher) -> None:
    logger.info("Остановка бота: закрываем HTTP-клиент...")
    await _close_http_client()


dp.shutdown.register(on_shutdown)

# ------------------------------------------------------
# Точка входа
# ------------------------------------------------------

async def main() -> None:
    logger.info("Запуск VPN Telegram-бота (long-polling)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
