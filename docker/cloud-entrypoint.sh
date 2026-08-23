#!/bin/sh
set -eu

load_secret() {
    variable_name="$1"
    file_variable_name="${variable_name}_FILE"
    eval "secret_file=\${${file_variable_name}:-}"
    if [ -n "${secret_file}" ]; then
        if [ ! -r "${secret_file}" ]; then
            echo "无法读取 Docker secret: ${file_variable_name}" >&2
            exit 1
        fi
        secret_value=$(tr -d '\r\n' < "${secret_file}")
        export "${variable_name}=${secret_value}"
    fi
}

load_secret LUMENX_SESSION_SECRET
load_secret LUMENX_TEST_SIGNING_SECRET
load_secret LUMENX_OSS_ACCESS_KEY_ID
load_secret LUMENX_OSS_ACCESS_KEY_SECRET
load_secret DASHSCOPE_API_KEY
load_secret XLINKS_API_KEY
load_secret LUMENX_DATABASE_PASSWORD

case "${LUMENX_DEPLOYMENT_MODE:-desktop}" in
cloud|test)
    cloud_like=true
    ;;
*)
    cloud_like=false
    ;;
esac

if [ "$cloud_like" = "true" ] && [ -z "${LUMENX_DATABASE_URL:-}" ]; then
    : "${LUMENX_DATABASE_PASSWORD:?缺少数据库密码}"
    database_user="${LUMENX_DATABASE_USER:-lumenx}"
    database_host="${LUMENX_DATABASE_HOST:-postgres}"
    database_port="${LUMENX_DATABASE_PORT:-5432}"
    database_name="${LUMENX_DATABASE_NAME:-lumenx}"
    export LUMENX_DATABASE_URL="postgresql+psycopg://${database_user}:${LUMENX_DATABASE_PASSWORD}@${database_host}:${database_port}/${database_name}"
fi

exec "$@"
