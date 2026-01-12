"""Handler package for the VPN Telegram bot.

This package contains modules responsible for registering various
handlers with the aiogram Dispatcher. Splitting handlers into
separate modules keeps the code organized and easier to maintain.
"""

from . import general, admin, configs, devices, payment

__all__ = [
    "general",
    "admin",
    "configs",
    "devices",
    "payment",
]