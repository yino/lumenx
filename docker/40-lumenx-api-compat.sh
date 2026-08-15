#!/bin/sh
set -eu

compat_dir=/etc/nginx/lumenx-compat.d
compat_file="${compat_dir}/legacy-api.conf"
compat_log_dir=/var/log/lumenx
mkdir -p "${compat_dir}" "${compat_log_dir}"

case "${LUMENX_LEGACY_API_COMPAT:-true}" in
    true|TRUE|1|yes|YES)
        cat >"${compat_file}" <<'EOF'
# Temporary hosted-client compatibility. Desktop traffic never traverses Nginx.
location ~ ^/(auth|workspaces|wallet|admin|ai|media|playground|projects|series|library|upload|voice|voices|video|bgm|art_direction|prompt_defaults|tasks|health|ready|config|docs|openapi\.json)(/|$) {
    proxy_pass http://backend:17177;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Request-ID $request_id;
    proxy_set_header X-Correlation-ID $request_id;
    proxy_set_header X-LumenX-Legacy-API "1";
    proxy_request_buffering on;
    proxy_buffering off;
    proxy_connect_timeout 10s;
    proxy_send_timeout 75s;
    proxy_read_timeout 75s;
    client_max_body_size 100M;
    include /etc/nginx/snippets/lumenx-security-headers.conf;
    add_header Deprecation "true" always;
    add_header Sunset "Wed, 30 Sep 2026 00:00:00 GMT" always;
    access_log /var/log/lumenx/lumenx-legacy-api.log combined;
}
EOF
        ;;
    false|FALSE|0|no|NO)
        : >"${compat_file}"
        ;;
    *)
        echo "LUMENX_LEGACY_API_COMPAT 必须是 true 或 false" >&2
        exit 1
        ;;
esac
