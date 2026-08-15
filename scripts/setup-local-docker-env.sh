#!/bin/sh
set -eu

env_file="${1:-.env}"
umask 077

if [ ! -f "${env_file}" ]; then
    printf '未找到 %s，请先从 .env.example 创建并填写本地 AI/OSS 配置。\n' "${env_file}" >&2
    exit 1
fi

read_env_value() {
    key="$1"
    value=$(awk -v key="${key}" '
        index($0, key "=") == 1 {
            print substr($0, length(key) + 2)
            exit
        }
    ' "${env_file}")
    case "${value}" in
        \"*\") value=${value#\"}; value=${value%\"} ;;
        \'*\') value=${value#\'}; value=${value%\'} ;;
    esac
    printf '%s' "${value}"
}

read_secret_file() {
    path="$1"
    if [ -r "${path}" ]; then
        tr -d '\r\n' < "${path}"
    fi
}

first_env_value() {
    for key in "$@"; do
        value=$(read_env_value "${key}")
        if [ -n "${value}" ]; then
            printf '%s' "${value}"
            return
        fi
    done
}

append_if_missing() {
    key="$1"
    value="$2"
    if [ -n "$(read_env_value "${key}")" ]; then
        return
    fi
    if [ -z "${value}" ]; then
        printf '缺少 %s 的本地配置，未修改 %s。\n' "${key}" "${env_file}" >&2
        exit 1
    fi
    printf '\n%s=%s\n' "${key}" "${value}" >> "${env_file}"
}

random_value() {
    openssl rand -hex 32
}

oss_endpoint=$(first_env_value LUMENX_LOCAL_OSS_ENDPOINT LUMENX_OSS_ENDPOINT OSS_ENDPOINT)
oss_bucket=$(first_env_value LUMENX_LOCAL_OSS_BUCKET_NAME LUMENX_OSS_BUCKET_NAME OSS_BUCKET_NAME)
oss_access_key_id=$(first_env_value LUMENX_LOCAL_OSS_ACCESS_KEY_ID LUMENX_OSS_ACCESS_KEY_ID ALIBABA_CLOUD_ACCESS_KEY_ID)
oss_access_key_secret=$(first_env_value LUMENX_LOCAL_OSS_ACCESS_KEY_SECRET LUMENX_OSS_ACCESS_KEY_SECRET ALIBABA_CLOUD_ACCESS_KEY_SECRET)
dashscope_api_key=$(first_env_value LUMENX_LOCAL_DASHSCOPE_API_KEY DASHSCOPE_API_KEY)

if [ -z "${oss_access_key_id}" ]; then
    oss_access_key_id=$(read_secret_file secrets/oss_access_key_id.txt)
fi
if [ -z "${oss_access_key_secret}" ]; then
    oss_access_key_secret=$(read_secret_file secrets/oss_access_key_secret.txt)
fi
if [ -z "${dashscope_api_key}" ]; then
    dashscope_api_key=$(read_secret_file secrets/dashscope_api_key.txt)
fi

append_if_missing LUMENX_LOCAL_POSTGRES_PASSWORD "$(random_value)"
append_if_missing LUMENX_LOCAL_POSTGRES_APP_PASSWORD "$(random_value)"
append_if_missing LUMENX_LOCAL_SESSION_SECRET "$(random_value)"
append_if_missing LUMENX_LOCAL_OSS_ENDPOINT "${oss_endpoint:-oss-cn-beijing.aliyuncs.com}"
append_if_missing LUMENX_LOCAL_OSS_BUCKET_NAME "${oss_bucket:-lumenx-private}"
append_if_missing LUMENX_LOCAL_OSS_ACCESS_KEY_ID "${oss_access_key_id}"
append_if_missing LUMENX_LOCAL_OSS_ACCESS_KEY_SECRET "${oss_access_key_secret}"
append_if_missing LUMENX_LOCAL_DASHSCOPE_API_KEY "${dashscope_api_key}"

chmod 600 "${env_file}"
printf '本地 Docker 环境已就绪：%s（未输出任何凭据）。\n' "${env_file}"
