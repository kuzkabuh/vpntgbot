"""Instruction text builder for the VPN Telegram bot.

This module contains a helper function to assemble the long, detailed
instructions for connecting to the WireGuard VPN. Keeping this logic
in a separate module makes it easier to maintain and update the
instruction content without cluttering the main bot logic.
"""

from __future__ import annotations

from typing import List

__all__ = ["build_instruction_text"]


def build_instruction_text() -> str:
    """
    Construct a detailed user-facing instruction for connecting to WireGuard.

    Returns:
        A string containing HTML-formatted instructions that can be sent
        directly to the user via a Telegram message. The instructions
        include two variants: using a QR code and using the `.conf`
        configuration file, along with troubleshooting advice.
    """
    lines: List[str] = [
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