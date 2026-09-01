#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"

if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' '缺少 docker，请在本地安装 Docker Desktop 或 Docker Engine。' >&2
  exit 1
fi

image="${LUMENX_FRONTEND_IMAGE:-lumenx-frontend:local}"
platform="${LUMENX_FRONTEND_PLATFORM:-linux/amd64}"
archive="${LUMENX_FRONTEND_ARCHIVE:-}"

printf '构建前端镜像：%s（平台：%s）\n' "$image" "$platform"
docker buildx build \
  --platform "$platform" \
  --file "$repo_root/docker/Dockerfile.frontend" \
  --tag "$image" \
  --load \
  "$repo_root"

docker image inspect "$image" >/dev/null

if [[ -n "$archive" ]]; then
  archive_dir="$(dirname -- "$archive")"
  mkdir -p "$archive_dir"
  printf '导出前端镜像：%s\n' "$archive"
  docker save "$image" | gzip -c > "$archive"
  printf '导出完成：%s\n' "$archive"
fi

printf '%s\n' '本地前端镜像已就绪。服务器导入后请使用 docker-compose-service-prebuilt.yml 启动。'
