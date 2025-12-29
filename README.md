<!--
Версия файла: 1.0.0
Описание: README для проекта VPN-платформы (WireGuard + WG-Easy + FastAPI + Telegram-бот)
Дата изменения: 2025-12-29
-->

# KUZKA VPN Platform (vpntgbot) — v1.0.0

Проект представляет собой VPN-платформу на базе WireGuard с панелью управления WG-Easy, backend-сервисом на FastAPI и Telegram-ботом для взаимодействия с пользователями.

Цель проекта — предоставить удобный и безопасный способ выдачи WireGuard-конфигураций клиентам через Telegram-бота и web-интерфейс, а также заложить фундамент для тарификации, биллинга и дальнейшего масштабирования.

---

## 1. Архитектура проекта

Проект разворачивается в Docker-контейнерах и включает следующие сервисы:

- **PostgreSQL (`vpn_db`)**  
  хранилище данных проекта (пользователи, VPN-аккаунты, тарифы и т.д.).

- **Backend (`vpn_backend`)**  
  FastAPI-приложение:
  - инициализация базы данных;
  - REST API для Telegram-бота и админских скриптов;
  - health-эндпоинт `/health`;
  - интеграция с WG-Easy API (WireGuard peers);
  - логирование.

- **Telegram-бот (`vpn_bot`)**  
  Python-бот для:
  - авторизации пользователей;
  - выдачи конфигураций WireGuard;
  - в перспективе — управления подписками и тарифами.

- **Nginx (`vpn_nginx`)**  
  реверс-прокси:
  - пробрасывает HTTP-запросы к `vpn_backend`;
  - отдает `/health` наружу;
  - готов к доработке до HTTPS (через отдельный контейнер или интеграцию с внешним nginx/Traefik).

- **WG-Easy (`wg_dashboard`)**  
  панель управления WireGuard:
  - web-интерфейс на порту `51821/tcp`;
  - WireGuard-интерфейс `wg0` на порту `51820/udp`;
  - используется как low-level менеджер конфигурации VPN.

Все сервисы описаны в `docker-compose.yml` и объединены в сеть `vpn-service_vpn_net`.

---

## 2. Требования

- Сервер: **Ubuntu 22.04 / 24.04**
- Установленные пакеты:
  - `docker`
  - `docker-compose` (или `docker compose` из Docker CLI)
- Свободные порты на хосте:
  - `80/tcp` — для HTTP-доступа к backend через nginx;
  - `51820/udp` — WireGuard;
  - `51821/tcp` — WG-Easy dashboard.

---

## 3. Структура проекта

Примерная структура каталога `/opt/vpn-service`:

/opt/vpn-service
├── backend/              # Код FastAPI backend
├── bot/                  # Код Telegram-бота
├── deploy/               # Скрипты и вспомогательные конфиги (при наличии)
├── nginx/
│   └── conf.d/           # Конфиги nginx для проксирования backend
├── db-data/              # Данные PostgreSQL (том, добавляется автоматически)
├── backups/              # Каталог для бэкапов БД (по ТЗ, на будущее)
├── docker-compose.yml    # Docker Compose файл для всех сервисов
├── .env.example          # Пример конфигурации окружения
├── .env                  # Рабочий конфиг окружения (НЕ коммитится)
├── .gitignore            # Игнорируемые файлы и каталоги
└── README.md             # Текущий файл с документацией

---

## 4. Конфигурация окружения (.env)

Файл .env создаётся на основе .env.example:

cp .env.example .env
nano .env
В .env задаются:

БД:

DB_NAME

DB_USER

DB_PASSWORD

BACKEND_DB_DSN

Backend:

APP_ENV

APP_DEBUG

APP_HOST

APP_PORT

BACKEND_BASE_URL

MGMT_API_TOKEN

Telegram-бот:

TELEGRAM_BOT_TOKEN

WG-Easy / WireGuard:

WG_HOST — публичный IP или домен сервера (для генерации клиентских конфигов);

WG_DEFAULT_LOCATION_CODE

WG_DEFAULT_LOCATION_NAME

WG_DASHBOARD_PASSWORD_HASH — bcrypt-хэш пароля от панели WG-Easy (переменная PASSWORD_HASH в docker-compose).

Бэкапы (на будущее):

BACKUP_ENABLED

BACKUP_LOCAL_DIR

BACKUP_RETENTION_DAYS

BACKUP_REMOTE_HOST

BACKUP_REMOTE_USER

BACKUP_REMOTE_DIR

BACKUP_SSH_KEY

Файл .env не должен попадать в репозиторий (добавлен в .gitignore).

---

## 5. Развёртывание

## 5.1. Клонирование репозитория
cd /opt
sudo git clone git@github.com:kuzkabuh/vpntgbot.git vpn-service
cd vpn-service
## 5.2. Подготовка .env
cp .env.example .env
nano .env
Заполнить все значения (особенно пароли, токены, DSN БД, WG_HOST и PASSWORD_HASH для WG-Easy).
## 5.3. Запуск контейнеров
sudo docker compose up -d --build
## 5.4. Проверка статуса контейнеров
sudo docker ps
Ожидаемые контейнеры:

vpn_db

vpn_backend

vpn_bot

vpn_nginx

wg_dashboard

## 5.5. Health-check backend через Nginx
На сервере:

curl http://127.0.0.1/health
Ожидаемый ответ (пример):

{
  "status": "ok" | "degraded",
  "timestamp": "...",
  "database_ok": true | false,
  "wg_easy_url": "http://wg_dashboard:51821"
}
При status: "ok" система в рабочем состоянии.

## 5.6. Доступ к WG-Easy
По умолчанию WG-Easy доступен по адресу:

http://<SERVER_IP>:51821/
Логин/пароль:

Логин: по умолчанию не требуется (форма только пароля).

Пароль: значение, соответствующее PASSWORD_HASH, заданному в docker-compose.yml / .env.

---

## 6. Обновление проекта

6.1. Обновление кода из GitHub
cd /opt/vpn-service
git pull origin main
sudo docker compose up -d --build
6.2. Обновление WG-Easy (образ)
cd /opt/vpn-service
sudo docker compose pull wg_dashboard
sudo docker compose up -d wg_dashboard
При смене major-версий WG-Easy необходимо сверяться с официальной документацией.

## 7. Версионирование
Проект использует Semantic Versioning:

MAJOR.MINOR.PATCH
MAJOR — несовместимые изменения;

MINOR — новый функционал без ломания обратной совместимости;

PATCH — багфиксы.

Текущая версия: v1.0.0

Релизы помечаются git-тегами:

git tag -a v1.0.0 -m "Initial stable version 1.0.0"
git push origin v1.0.0
## 8. Планируемое развитие
Реализация тарифной системы и биллинга.

Личный кабинет пользователя (web-интерфейс).

Расширенная аналитика по использованию VPN.

Мульти-локации / несколько WG-серверов.

Автоматические бэкапы PostgreSQL и конфигурации WireGuard.

Усиление безопасности (JWT, RBAC, ограничения по IP и т.п.).

## 9. Лицензия
Тип лицензии может быть определён позже (MIT / Proprietary / иное). По умолчанию проект рассматривается как закрытый внутренний продукт.


---
