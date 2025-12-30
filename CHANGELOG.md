<!--
Версия файла: 1.0.0
Описание: Журнал изменений проекта KUZKA VPN Platform
Дата изменения: 2025-12-29
-->

# Changelog

## [1.3.1] - 2025-12-30
### Added
- HTTPS access to WG-Easy via Nginx on port 51821
- Certbot container with auto-renew

### Changed
- Upgraded WG-Easy to v15
- Simplified and cleaned nginx configs

### Fixed
- Duplicate upstream issues
- Certbot HTTP-01 challenge routing

## [1.2.0] - 2025-12-29
### Added
- Внедрены миграции Alembic и первичная схема БД.
- Добавлены модели WireGuard-пиров и связанные таблицы.
- Добавлена переменная WG_EASY_PASSWORD для backend.

### Changed
- Синхронизированы backend API и Telegram-бот.
- Обновлены схемы ответов для статуса подписки и выдачи триала.
- Убраны небезопасные дефолты конфигурации backend.
- Обновлены примеры .env и README.
