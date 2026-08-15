#!/bin/sh
set -eu

if [ -n "${POSTGRES_PASSWORD_FILE:-}" ]; then
    if [ ! -r "${POSTGRES_PASSWORD_FILE}" ]; then
        echo "无法读取 PostgreSQL 备份密码" >&2
        exit 1
    fi
    export PGPASSWORD="$(tr -d '\r\n' < "${POSTGRES_PASSWORD_FILE}")"
else
    export PGPASSWORD="${POSTGRES_PASSWORD:?缺少 PostgreSQL 备份密码}"
fi

while true; do
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    backup_file="/backups/lumenx-${timestamp}.dump"
    pg_dump \
        --host="${POSTGRES_HOST:-postgres}" \
        --username="${POSTGRES_USER:-lumenx}" \
        --dbname="${POSTGRES_DB:-lumenx}" \
        --format=custom \
        --file="${backup_file}"
    pg_restore --list "${backup_file}" > "${backup_file}.list"
    sha256sum "${backup_file}" > "${backup_file}.sha256"
    find /backups -type f -name 'lumenx-*.dump' -mtime "+${BACKUP_RETENTION_DAYS:-14}" -delete
    find /backups -type f \( -name 'lumenx-*.dump.list' -o -name 'lumenx-*.dump.sha256' \) \
        -mtime "+${BACKUP_RETENTION_DAYS:-14}" -delete
    sleep "${BACKUP_INTERVAL_SECONDS:-86400}"
done
