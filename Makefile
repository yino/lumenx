SHELL := /bin/sh
.DEFAULT_GOAL := help

PYTHON ?= .venv/bin/python
PYTHON_BOOTSTRAP ?= python3
NPM ?= npm
COMPOSE ?= docker compose
BACKEND_PORT ?= 17177
FRONTEND_PORT ?= 3008
OPENSPEC_CHANGE ?= harden-cloud-launch-readiness

.PHONY: help doctor install dev backend frontend build build-mac build-windows \
	test test-backend test-frontend lint typecheck check-colors compile check migrate \
	docker-env docker-config docker-production-config docker-build docker-up docker-down \
	docker-logs docker-ps docker-migrate \
	openspec-validate release-check

help:
	@printf '%s\n' \
		'LumenX 常用命令' \
		'' \
		'本地开发：' \
		'  make doctor          检查 Python、Node.js、npm、FFmpeg 和 Docker' \
		'  make install         创建 .venv，并安装后端、根目录和前端依赖' \
		'  make dev             同时启动后端与前端，并自动打开浏览器' \
		'  make backend         仅启动后端（默认 http://localhost:17177）' \
		'  make frontend        仅启动前端（默认 http://localhost:3008）' \
		'' \
		'构建与检查：' \
		'  make build           构建前端生产产物' \
		'  make build-mac       调用现有脚本构建 macOS 桌面应用' \
		'  make build-windows   调用现有脚本构建 Windows 桌面应用' \
		'  make test            运行后端和前端测试' \
		'  make lint            运行前端 ESLint' \
		'  make typecheck       运行前端 TypeScript 类型检查' \
		'  make check           运行测试、静态检查和 Python 编译检查' \
		'  make release-check   运行无付费、无上线副作用的发布前检查' \
		'' \
		'数据库与 Docker：' \
		'  make migrate         对当前 DATABASE_URL 执行 Alembic 迁移' \
		'  make docker-env      为本地 Docker 补齐 .env（不会输出凭据）' \
		'  make docker-config   校验本地 Compose 配置' \
		'  make docker-build    构建本地 Docker 镜像' \
		'  make docker-up       后台启动完整本地 Web 服务' \
		'  make docker-down     停止本地服务（保留命名卷数据）' \
		'  make docker-logs     持续查看本地服务日志' \
		'  make docker-ps       查看本地服务状态' \
		'  make docker-migrate  在本地 Compose 中执行一次数据库迁移' \
		'  make docker-production-config  仅校验生产 Secret 配置' \
		'' \
		'可覆盖变量：PYTHON、PYTHON_BOOTSTRAP、NPM、COMPOSE、BACKEND_PORT、FRONTEND_PORT、OPENSPEC_CHANGE'

doctor:
	@set -eu; \
	for command_name in "$(PYTHON_BOOTSTRAP)" node "$(NPM)" ffmpeg docker openssl; do \
		if ! command -v "$$command_name" >/dev/null 2>&1; then \
			printf '缺少命令：%s\n' "$$command_name" >&2; \
			exit 1; \
		fi; \
	done; \
	$(COMPOSE) version >/dev/null; \
	printf '%s\n' '开发环境检查通过。'

install:
	@if [ ! -x .venv/bin/python ]; then \
		$(PYTHON_BOOTSTRAP) -m venv .venv; \
	fi
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt
	$(NPM) ci
	$(NPM) --prefix frontend ci

dev:
	$(NPM) run dev

backend:
	NO_PROXY='*.aliyuncs.com,localhost,127.0.0.1' \
	no_proxy='*.aliyuncs.com,localhost,127.0.0.1' \
	$(PYTHON) -m uvicorn src.apps.comic_gen.api:app --reload \
		--host 0.0.0.0 --port $(BACKEND_PORT)

frontend:
	PORT=$(FRONTEND_PORT) $(NPM) --prefix frontend run dev

build:
	$(NPM) --prefix frontend run build

build-mac:
	./build_mac.sh

build-windows:
	powershell -NoProfile -ExecutionPolicy Bypass -File ./build_windows.ps1

test: test-backend test-frontend

test-backend:
	$(PYTHON) -m pytest -q

test-frontend:
	$(NPM) --prefix frontend run test:all

lint:
	$(NPM) --prefix frontend run lint

typecheck:
	$(NPM) --prefix frontend run typecheck

check-colors:
	$(NPM) --prefix frontend run check:colors

compile:
	$(PYTHON) -m compileall -q src scripts

check: compile test lint typecheck check-colors

migrate:
	$(PYTHON) -m alembic upgrade head

docker-env:
	./scripts/setup-local-docker-env.sh .env

docker-config: docker-env
	$(COMPOSE) config --quiet

docker-production-config:
	$(COMPOSE) -f docker-compose.yml config --quiet

docker-build: docker-config
	$(COMPOSE) build

docker-up: docker-config
	$(COMPOSE) up -d --build

docker-down: docker-env
	$(COMPOSE) down

docker-logs: docker-env
	$(COMPOSE) logs --follow --tail=200

docker-ps: docker-env
	$(COMPOSE) ps

docker-migrate: docker-config
	$(COMPOSE) run --rm migration

openspec-validate:
	openspec validate $(OPENSPEC_CHANGE) --strict

release-check: check build docker-production-config openspec-validate
