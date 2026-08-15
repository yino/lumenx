#!/bin/sh
set -eu

app_user="${LUMENX_DATABASE_USER:-lumenx_app}"
if [ "${app_user}" = "${POSTGRES_USER}" ]; then
    echo "应用数据库角色不能与 PostgreSQL 管理员角色相同" >&2
    exit 1
fi

if [ -n "${POSTGRES_APP_PASSWORD_FILE:-}" ]; then
    if [ ! -r "${POSTGRES_APP_PASSWORD_FILE}" ]; then
        echo "无法读取应用数据库密码" >&2
        exit 1
    fi
    app_password="$(tr -d '\r\n' < "${POSTGRES_APP_PASSWORD_FILE}")"
else
    app_password="${POSTGRES_APP_PASSWORD:-}"
fi
if [ -z "${app_password}" ]; then
    echo "应用数据库密码不能为空" >&2
    exit 1
fi

if [ -n "${POSTGRES_PASSWORD_FILE:-}" ]; then
    if [ ! -r "${POSTGRES_PASSWORD_FILE}" ]; then
        echo "无法读取 PostgreSQL 管理员密码" >&2
        exit 1
    fi
    export PGPASSWORD="$(tr -d '\r\n' < "${POSTGRES_PASSWORD_FILE}")"
else
    export PGPASSWORD="${POSTGRES_PASSWORD:?缺少 PostgreSQL 管理员密码}"
fi
export POSTGRES_APP_PASSWORD="${app_password}"
psql \
    --host="${POSTGRES_HOST:-localhost}" \
    --port="${POSTGRES_PORT:-5432}" \
    --username="${POSTGRES_USER}" \
    --dbname="${POSTGRES_DB}" \
    --set=ON_ERROR_STOP=1 <<'SQL'
\getenv app_user LUMENX_DATABASE_USER
\getenv app_password POSTGRES_APP_PASSWORD
\getenv admin_user POSTGRES_USER

SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
    :'app_user',
    :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') \gexec

SELECT format(
    'ALTER ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
    :'app_user',
    :'app_password'
) \gexec

SELECT format(
    'GRANT CONNECT ON DATABASE %I TO %I',
    current_database(),
    :'app_user'
) \gexec
GRANT USAGE ON SCHEMA public TO :"app_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app_user";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app_user";
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_user" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_user" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_user" IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO :"app_user";
SQL
unset PGPASSWORD POSTGRES_APP_PASSWORD app_password
