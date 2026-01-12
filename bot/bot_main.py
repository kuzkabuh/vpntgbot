"""Entry point for the VPN Telegram bot.

This script sets up logging, instantiates the aiogram bot and
dispatcher, registers handlers from submodules, and starts the
long-polling loop. By delegating the bulk of the logic to
``handlers`` modules, this file remains concise and focuses on
orchestration.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

import handlers.admin
import handlers.configs
import handlers.devices
import handlers.general
import handlers.payment
from settings import TELEGRAM_BOT_TOKEN


def setup_logging() -> None:
    """Configure basic logging for the bot."""
    logging.basicConfig(
        level=logging.INFO,
        format="[VPN-BOT] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def main() -> None:
    """Main entry point: create bot, register handlers, run polling."""
    setup_logging()
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в окружении бота.")
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher()
    # Register handlers from submodules. Order matters for fallback handlers.
    # Register specific feature handlers first, and register general handlers last so that
    # its fallback does not block other modules. The order below ensures that
    # admin, configs, devices, and payment handlers get a chance to process
    # messages before the general fallback.
    handlers.configs.register_handlers(dp, bot)
    handlers.devices.register_handlers(dp, bot)
    handlers.payment.register_handlers(dp, bot)
    handlers.admin.register_handlers(dp, bot)
    handlers.general.register_handlers(dp, bot)
    # Start polling
    logging.getLogger("vpn-bot").info("Запуск VPN Telegram-бота (long-polling)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())