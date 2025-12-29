#!/usr/bin/env bash
# ----------------------------------------------------------
# Версия файла: 1.0.0
# Описание: Резервное копирование БД PostgreSQL (vpn_service)
#           с сохранением локального дампа и отправкой на
#           удалённый резервный сервер по SSH (scp).
#           Настройки берём из /opt/vpn-service/.env
# Дата изменения: 2025-12-29
# Изменения:
#  - начальная версия скрипта с ротацией и поддержкой удалённого сервера
# ----------------------------------------------------------

set -euo pipefail

PROJECT_DIR="/opt/vpn-service"
ENV_FILE="${PROJECT_DIR}/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "[ОШИБКА] Файл ${ENV_FILE} не найден. Невозможно загрузить настройки."
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

BACKUP_ENABLED_LOWER="$(echo "${BACKUP_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')"
if [[ "${BACKUP_ENABLED_LOWER}" != "true" ]]; then
  echo "[ИНФО] BACKUP_ENABLED=${BACKUP_ENABLED} -> резервное копирование отключено, выходим."
  exit 0
fi

DB_NAME="${DB_NAME:-}"
DB_USER="${DB_USER:-}"

if [[ -z "${DB_NAME}" || -z "${DB_USER}" ]]; then
  echo "[ОШИБКА] Переменные DB_NAME или DB_USER не заданы в .env"
  exit 1
fi

DB_CONTAINER_NAME="${DB_CONTAINER_NAME:-vpn_db}"

BACKUP_LOCAL_DIR="${BACKUP_LOCAL_DIR:-${PROJECT_DIR}/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"

mkdir -p "${BACKUP_LOCAL_DIR}"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILENAME="db_${DB_NAME}_${TIMESTAMP}.sql.gz"
BACKUP_FILE_LOCAL="${BACKUP_LOCAL_DIR}/${BACKUP_FILENAME}"

echo "[ИНФО] Начинаем резервное копирование БД '${DB_NAME}' (контейнер: ${DB_CONTAINER_NAME})..."
echo "[ИНФО] Локальный файл бэкапа: ${BACKUP_FILE_LOCAL}"

if ! docker ps --format '{{.Names}}' | grep -q "^${DB_CONTAINER_NAME}$"; then
  echo "[ОШИБКА] Контейнер с БД '${DB_CONTAINER_NAME}' не запущен."
  exit 1
fi

set +e
docker exec -i "${DB_CONTAINER_NAME}" pg_dump -U "${DB_USER}" "${DB_NAME}" 2>"/tmp/pg_dump_${TIMESTAMP}.log" | gzip > "${BACKUP_FILE_LOCAL}"
DUMP_EXIT_CODE=$?
set -e

if [[ ${DUMP_EXIT_CODE} -ne 0 ]]; then
  echo "[ОШИБКА] Ошибка при выполнении pg_dump. Код: ${DUMP_EXIT_CODE}"
  echo "Лог pg_dump:"
  cat "/tmp/pg_dump_${TIMESTAMP}.log" || true
  rm -f "/tmp/pg_dump_${TIMESTAMP}.log"
  exit ${DUMP_EXIT_CODE}
fi

rm -f "/tmp/pg_dump_${TIMESTAMP}.log"

echo "[ИНФО] Локальный бэкап БД успешно создан."

BACKUP_REMOTE_HOST="${BACKUP_REMOTE_HOST:-}"
BACKUP_REMOTE_USER="${BACKUP_REMOTE_USER:-}"
BACKUP_REMOTE_DIR="${BACKUP_REMOTE_DIR:-}"
BACKUP_SSH_KEY="${BACKUP_SSH_KEY:-}"

REMOTE_BACKUP_PERFORMED=false

if [[ -n "${BACKUP_REMOTE_HOST}" && -n "${BACKUP_REMOTE_USER}" && -n "${BACKUP_REMOTE_DIR}" && -n "${BACKUP_SSH_KEY}" ]]; then
  if [[ ! -f "${BACKUP_SSH_KEY}" ]]; then
    echo "[ПРЕДУПРЕЖДЕНИЕ] SSH-ключ для резервного сервера не найден по пути: ${BACKUP_SSH_KEY}"
    echo "[ПРЕДУПРЕЖДЕНИЕ] Бэкап будет сохранён только локально."
  else
    echo "[ИНФО] Отправляем бэкап на резервный сервер ${BACKUP_REMOTE_USER}@${BACKUP_REMOTE_HOST}:${BACKUP_REMOTE_DIR} ..."
    ssh -i "${BACKUP_SSH_KEY}" -o StrictHostKeyChecking=accept-new \
      "${BACKUP_REMOTE_USER}@${BACKUP_REMOTE_HOST}" \
      "mkdir -p '${BACKUP_REMOTE_DIR}'"

    scp -i "${BACKUP_SSH_KEY}" \
      "${BACKUP_FILE_LOCAL}" \
      "${BACKUP_REMOTE_USER}@${BACKUP_REMOTE_HOST}:${BACKUP_REMOTE_DIR}/"

    REMOTE_BACKUP_PERFORMED=true
    echo "[ИНФО] Бэкап успешно скопирован на резервный сервер."
  fi
else
  echo "[ИНФО] Параметры резервного сервера не заданы. Бэкап сохранён только локально."
fi

echo "[ИНФО] Ротация локальных бэкапов старше ${BACKUP_RETENTION_DAYS} дней..."
find "${BACKUP_LOCAL_DIR}" -type f -name "db_${DB_NAME}_*.sql.gz" -mtime +"${BACKUP_RETENTION_DAYS}" -print -delete || true

if [[ "${REMOTE_BACKUP_PERFORMED}" == "true" ]]; then
  echo "[ИНФО] Ротация бэкапов на резервном сервере..."
  ssh -i "${BACKUP_SSH_KEY}" "${BACKUP_REMOTE_USER}@${BACKUP_REMOTE_HOST}" bash <<EOF_REMOTE_ROTATE
set -euo pipefail
if [ -d "${BACKUP_REMOTE_DIR}" ]; then
  find "${BACKUP_REMOTE_DIR}" -type f -name "db_${DB_NAME}_*.sql.gz" -mtime +"${BACKUP_RETENTION_DAYS}" -print -delete || true
fi
EOF_REMOTE_ROTATE
fi

echo "[ГОТОВО] Резервное копирование БД '${DB_NAME}' завершено."
echo "        Локальный файл: ${BACKUP_FILE_LOCAL}"
if [[ "${REMOTE_BACKUP_PERFORMED}" == "true" ]]; then
  echo "        Также сохранено на резервном сервере: ${BACKUP_REMOTE_USER}@${BACKUP_REMOTE_HOST}:${BACKUP_REMOTE_DIR}/${BACKUP_FILENAME}"
fi
