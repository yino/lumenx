#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from sqlalchemy import select

from src.platform.auth.admin_identity import (
    ADMIN_CSRF_COOKIE_NAME,
    ADMIN_SESSION_COOKIE_NAME,
)
from src.platform.auth.sessions import CSRF_COOKIE_NAME
from src.platform.configuration_api import _deployment_resource_fingerprints
from src.platform.contracts import AdminContext
from src.platform.database import Database
from src.platform.db_models import AITaskAttemptRecord, UsageEventRecord
from src.platform.settings import DeploymentMode, get_deployment_settings
from src.platform.ticket_reconciliation import TicketReconciliationService


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
)
EXECUTE_CONFIRMATION = "RUN_ONE_MINIMUM_COST_CANARY"
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "support_review"}
SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,79}$")
RESOURCE_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
ISOLATED_RESOURCES = ("postgresql", "redis", "oss_bucket", "provider_account")


class CanaryFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CanaryResult:
    environment: str
    origin: str
    revision: str
    isolation_manifest_sha256: str
    status: str
    started_at: str
    completed_at: str
    closed_config_version_id: str
    restored_config_version_id: str
    user_id: str
    task_id: str
    attempt_id: str
    provider_request_id: str | None
    provider_task_id: str | None
    media_ids: tuple[str, ...]
    metering_tokens: int
    tokens_per_ticket: int
    charged_microtickets: int
    reconciliation_issue_count: int


def _safe_label(value: str, name: str) -> str:
    normalized = value.strip()
    if not SAFE_LABEL.fullmatch(normalized):
        raise CanaryFailure(f"{name}只能包含字母、数字、点、下划线和连字符")
    return normalized


def _validate_staging_identity(environment: str, confirmation: str | None) -> str:
    normalized = _safe_label(environment, "环境名")
    lowered = normalized.lower()
    environment_tokens = set(re.split(r"[._-]+", lowered))
    if environment_tokens.intersection({"production", "prod", "live"}):
        raise CanaryFailure("真实 canary 禁止以生产环境为目标")
    if not any(marker in lowered for marker in ("staging", "stage", "preprod", "canary")):
        raise CanaryFailure("环境名必须明确标识 staging、preprod 或 canary")
    if confirmation is not None and confirmation != normalized:
        raise CanaryFailure("隔离 staging 确认值必须与环境名完全一致")
    return normalized


def _validate_model_id(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 160 or any(
        character.isspace() or ord(character) < 32 for character in normalized
    ):
        raise CanaryFailure("供应商模型标识无效")
    return normalized


def _validate_origin(origin: str) -> str:
    normalized = origin.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or not parsed.hostname:
        raise CanaryFailure("真实 staging canary 必须通过 HTTPS 域名执行")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CanaryFailure("canary origin 不能包含凭据、查询参数或片段")
    return normalized


def _required_env(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        raise CanaryFailure(f"缺少运行时环境变量 {name}")
    return value


def _positive_integer_id(value: Any, label: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise CanaryFailure(f"{label}缺少有效正整数标识") from exc
    if parsed <= 0:
        raise CanaryFailure(f"{label}缺少有效正整数标识")
    return parsed


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _identifier_hash(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_isolation_manifest(
    path: Path,
    environment: str,
    observed_fingerprints: dict[str, Any] | None,
) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanaryFailure("无法读取 staging 隔离清单") from exc
    if not isinstance(payload, dict) or payload.get("environment") != environment:
        raise CanaryFailure("隔离清单环境与 canary 环境不一致")
    _safe_label(str(payload.get("reviewed_by") or ""), "隔离清单审核人")
    try:
        reviewed_at = datetime.fromisoformat(
            str(payload.get("reviewed_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise CanaryFailure("隔离清单审核时间无效") from exc
    if reviewed_at.tzinfo is None:
        raise CanaryFailure("隔离清单审核时间必须包含时区")
    reviewed_at = reviewed_at.astimezone(UTC)
    now = datetime.now(UTC)
    if reviewed_at > now or reviewed_at < now - timedelta(days=30):
        raise CanaryFailure("隔离清单必须在最近 30 天内完成审核")
    resources = payload.get("resources")
    if not isinstance(resources, dict):
        raise CanaryFailure("隔离清单缺少资源指纹")
    if not isinstance(observed_fingerprints, dict):
        raise CanaryFailure("当前部署未返回资源指纹")
    for resource_name in ISOLATED_RESOURCES:
        resource = resources.get(resource_name)
        if not isinstance(resource, dict):
            raise CanaryFailure(f"隔离清单缺少 {resource_name} 指纹")
        staging_fingerprint = str(resource.get("staging_fingerprint") or "")
        production_fingerprint = str(resource.get("production_fingerprint") or "")
        if not RESOURCE_FINGERPRINT.fullmatch(staging_fingerprint):
            raise CanaryFailure(f"{resource_name} staging 指纹无效")
        if not RESOURCE_FINGERPRINT.fullmatch(production_fingerprint):
            raise CanaryFailure(f"{resource_name} production 指纹无效")
        if staging_fingerprint == production_fingerprint:
            raise CanaryFailure(f"{resource_name} staging 与 production 未隔离")
        observed_fingerprint = str(observed_fingerprints.get(resource_name) or "")
        if not RESOURCE_FINGERPRINT.fullmatch(observed_fingerprint):
            raise CanaryFailure(f"当前部署缺少 {resource_name} 资源指纹")
        if staging_fingerprint != observed_fingerprint:
            raise CanaryFailure(
                f"{resource_name} staging 指纹与当前部署不一致"
            )
    return _file_sha256(path)


class EdgeClient:
    def __init__(
        self,
        origin: str,
        *,
        csrf_cookie_name: str = CSRF_COOKIE_NAME,
    ) -> None:
        self.origin = origin
        self.session = requests.Session()
        self.csrf_cookie_name = csrf_cookie_name

    def request(
        self,
        method: str,
        path: str,
        *,
        expected: int | set[int],
        timeout: float = 30,
        **kwargs: Any,
    ) -> requests.Response:
        method = method.upper()
        headers = dict(kwargs.pop("headers", {}))
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            headers.setdefault("Origin", self.origin)
            csrf_token = self.session.cookies.get(self.csrf_cookie_name)
            if csrf_token:
                headers.setdefault("X-CSRF-Token", csrf_token)
        try:
            response = self.session.request(
                method,
                f"{self.origin}{path}",
                headers=headers,
                timeout=timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise CanaryFailure(f"staging 请求失败：{type(exc).__name__}") from exc
        expected_codes = {expected} if isinstance(expected, int) else expected
        if response.status_code not in expected_codes:
            error_code = "UNKNOWN"
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    error_code = str(payload.get("code") or error_code)
            except ValueError:
                pass
            raise CanaryFailure(
                f"staging API 返回异常：HTTP {response.status_code}，错误码 {error_code}"
            )
        if path.startswith("/api/v1"):
            content_type = response.headers.get("content-type", "").lower()
            if "application/json" not in content_type:
                raise CanaryFailure("staging API 未返回 JSON")
        return response

    def login(self, phone: str, password: str) -> dict[str, Any]:
        payload = self.request(
            "POST",
            "/api/v1/auth/login",
            expected=200,
            json={"phone": phone, "password": password},
        ).json()
        if not self.session.cookies.get("lumenx_session"):
            raise CanaryFailure("登录成功响应未设置安全会话")
        return payload

    def admin_login(self, username: str, password: str) -> dict[str, Any]:
        payload = self.request(
            "POST",
            "/api/v1/admin/auth/login",
            expected=200,
            json={"identifier": username, "password": password},
        ).json()
        if not self.session.cookies.get(ADMIN_SESSION_COOKIE_NAME):
            raise CanaryFailure("管理员登录未设置独立安全会话")
        return payload

    def get_json(self, path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
        return self.request("GET", path, expected=200, **kwargs).json()


def _admin_client(origin: str) -> tuple[EdgeClient, dict[str, Any]]:
    client = EdgeClient(origin, csrf_cookie_name=ADMIN_CSRF_COOKIE_NAME)
    client.admin_login(
        _required_env("LUMENX_CANARY_ADMIN_USERNAME"),
        _required_env("LUMENX_CANARY_ADMIN_PASSWORD"),
    )
    me = client.get_json("/api/v1/admin/auth/me")
    if not isinstance(me, dict) or not me.get("admin", {}).get("id"):
        raise CanaryFailure("canary 管理账号没有独立管理员身份")
    return client, me


def _user_client(origin: str, phone_env: str, password_env: str) -> tuple[EdgeClient, str]:
    client = EdgeClient(origin)
    response = client.login(_required_env(phone_env), _required_env(password_env))
    user_id = str(response.get("user", {}).get("id") or "")
    _positive_integer_id(user_id, "canary 用户响应")
    return client, user_id


def _deployment_state(admin: EdgeClient) -> dict[str, Any]:
    state = admin.get_json("/api/v1/admin/configuration/deployment-state")
    if not isinstance(state, dict):
        raise CanaryFailure("部署状态响应格式无效")
    required = {
        "deployment_mode": "cloud",
        "object_store_adapter": "oss",
        "provider_adapter": "production",
        "test_adapters_enabled": False,
        "oss_private": True,
    }
    mismatches = [key for key, expected in required.items() if state.get(key) != expected]
    if mismatches:
        raise CanaryFailure(
            "目标不是私有 OSS + 真实供应商的 cloud 环境："
            f"{', '.join(mismatches)}"
        )
    try:
        local_fingerprints = _deployment_resource_fingerprints(
            get_deployment_settings()
        )
    except Exception as exc:  # noqa: BLE001 - present a stable canary gate error
        raise CanaryFailure("canary 本地运行环境配置无效") from exc
    _require_matching_runtime_fingerprints(
        state.get("resource_fingerprints"),
        local_fingerprints,
    )
    return state


def _require_matching_runtime_fingerprints(
    edge_fingerprints: Any,
    local_fingerprints: dict[str, str | None],
) -> None:
    if not isinstance(edge_fingerprints, dict):
        raise CanaryFailure("目标部署未返回资源指纹")
    mismatches = [
        resource_name
        for resource_name in ISOLATED_RESOURCES
        if not RESOURCE_FINGERPRINT.fullmatch(
            str(edge_fingerprints.get(resource_name) or "")
        )
        or edge_fingerprints.get(resource_name) != local_fingerprints.get(resource_name)
    ]
    if mismatches:
        raise CanaryFailure(
            "canary 本地运行环境与目标部署资源不一致："
            f"{', '.join(mismatches)}"
        )


def _active_config(admin: EdgeClient) -> dict[str, Any]:
    active = admin.get_json("/api/v1/admin/configuration/active")
    if not isinstance(active, dict):
        raise CanaryFailure("激活配置响应格式无效")
    return active


def _feature_flags(config: dict[str, Any]) -> dict[str, Any]:
    flags = config.get("platform", {}).get("feature_flags")
    if not isinstance(flags, dict):
        raise CanaryFailure("激活配置缺少功能开关")
    return flags


def _require_closed_state(state: dict[str, Any], config: dict[str, Any]) -> None:
    if state.get("registration_emergency_disabled") is not True:
        raise CanaryFailure("注册部署熔断未关闭入口")
    if state.get("new_ai_tasks_emergency_disabled") is not True:
        raise CanaryFailure("AI 部署熔断未关闭入口")
    flags = _feature_flags(config)
    if flags.get("registration_mode") != "disabled":
        raise CanaryFailure("数据库注册模式不是 disabled")
    if flags.get("new_ai_tasks_enabled") is not False:
        raise CanaryFailure("数据库 AI 新任务开关未关闭")


def _require_execute_state(
    state: dict[str, Any],
    config: dict[str, Any],
    capability: str,
    expected_model_id: str,
) -> None:
    if state.get("registration_emergency_disabled") is not True:
        raise CanaryFailure("执行 canary 时注册部署熔断必须保持关闭")
    if state.get("new_ai_tasks_emergency_disabled") is not False:
        raise CanaryFailure("执行 canary 前必须只临时解除 AI 部署熔断")
    flags = _feature_flags(config)
    if flags.get("registration_mode") != "disabled":
        raise CanaryFailure("执行 canary 时数据库注册模式必须保持 disabled")
    if flags.get("new_ai_tasks_enabled") is not True:
        raise CanaryFailure("执行 canary 的临时数据库配置尚未允许 AI 新任务")
    exposed = config.get("platform", {}).get("exposed_capabilities") or []
    if capability not in exposed:
        raise CanaryFailure("临时配置未开放指定 canary 能力")
    matching_routes = [
        route
        for route in config.get("routes") or []
        if route.get("capability") == capability and route.get("enabled")
    ]
    if len(matching_routes) != 1 or not matching_routes[0].get("is_primary"):
        raise CanaryFailure("指定 canary 能力必须只有一个启用的主路由")
    if matching_routes[0].get("provider_model_id") != expected_model_id:
        raise CanaryFailure("临时配置的真实模型与审核值不一致")


def _workspace(client: EdgeClient) -> str:
    payload = client.get_json("/api/v1/workspaces")
    if not isinstance(payload, list) or len(payload) != 1:
        raise CanaryFailure("canary 用户必须且只能有一个隔离工作区")
    workspace_id = str(payload[0].get("id") or "")
    _positive_integer_id(workspace_id, "canary 工作区")
    return workspace_id


def _poll_task(
    client: EdgeClient,
    workspace_id: str,
    task_id: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    headers = {"X-Workspace-ID": workspace_id}
    while time.monotonic() < deadline:
        payload = client.get_json(
            f"/api/v1/ai/tasks/{task_id}/status",
            headers=headers,
        )
        if not isinstance(payload, dict):
            raise CanaryFailure("AI 任务状态响应无效")
        status = payload.get("status")
        if status in TERMINAL_STATUSES:
            if status != "succeeded":
                raise CanaryFailure(f"真实供应商 canary 进入异常终态：{status}")
            return payload
        time.sleep(2)
    raise CanaryFailure("真实供应商 canary 等待超时")


def _database_evidence(
    admin_id: str,
    user_id: str,
    task_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    settings = get_deployment_settings()
    if settings.deployment_mode is not DeploymentMode.CLOUD or not settings.database_url:
        raise CanaryFailure("execute/finalize 必须在同一 staging cloud 部署环境内运行")
    database = Database(settings.database_url)
    admin = AdminContext(
        admin_id=str(_positive_integer_id(admin_id, "canary 管理员")),
        session_id="staging-canary-reconciliation",
    )
    try:
        report = TicketReconciliationService(database).reconcile(
            admin,
            target_user_id=user_id,
        ).as_dict()
        with database.transaction(admin) as session:
            attempt = session.scalar(
                select(AITaskAttemptRecord)
                .where(
                    AITaskAttemptRecord.task_id
                    == _positive_integer_id(task_id, "canary 任务")
                )
                .order_by(AITaskAttemptRecord.attempt_number.desc())
                .limit(1)
            )
            usage = session.scalar(
                select(UsageEventRecord)
                .where(
                    UsageEventRecord.task_id
                    == _positive_integer_id(task_id, "canary 任务")
                )
                .order_by(UsageEventRecord.created_at.desc())
                .limit(1)
            )
        if attempt is None or usage is None:
            raise CanaryFailure("canary 缺少供应商尝试或用量结算记录")
        return report, {
            "attempt_id": str(attempt.id),
            "provider_request_id": attempt.provider_request_id,
            "provider_task_id": attempt.provider_task_id,
            "metering_tokens": usage.metering_tokens,
            "tokens_per_ticket": usage.tokens_per_ticket,
            "charged_microtickets": usage.charged_microtickets,
        }
    finally:
        database.engine.dispose()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_markdown(path: Path, title: str, rows: list[tuple[str, str]], decision: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {title}", ""]
    lines.extend(f"- {name}：{value}" for name, value in rows)
    lines.extend(["", "## 结论", "", decision, ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def preflight(args: argparse.Namespace) -> int:
    environment = _validate_staging_identity(args.environment, None)
    origin = _validate_origin(args.origin)
    revision = _safe_label(args.revision, "修订号")
    admin, _me = _admin_client(origin)
    admin.get_json("/api/v1/health")
    ready = admin.get_json("/api/v1/ready")
    if not isinstance(ready, dict) or ready.get("ready") is not True:
        raise CanaryFailure("staging 未通过 readiness 检查")
    state = _deployment_state(admin)
    isolation_hash = validate_isolation_manifest(
        args.isolation_manifest,
        environment,
        state.get("resource_fingerprints"),
    )
    active = _active_config(admin)
    _require_closed_state(state, active)
    _write_markdown(
        args.evidence_path,
        "LumenX staging canary 预检证据",
        [
            ("环境", f"`{environment}`"),
            ("产物修订", f"`{revision}`"),
            ("目标", origin),
            ("隔离清单 SHA-256", f"`{isolation_hash}`"),
            ("激活配置", f"`{active['id']}`"),
            ("私有 OSS", "通过"),
            ("真实供应商适配器", "通过"),
            ("注册部署熔断", "已关闭入口"),
            ("AI 部署熔断", "已关闭入口"),
        ],
        "预检通过。尚未调用供应商，也未产生费用；"
        "只有审核临时配置后才可进入 execute。",
    )
    print(f"staging canary 预检通过：{args.evidence_path}")
    return 0


def execute(args: argparse.Namespace) -> int:
    environment = _validate_staging_identity(
        args.environment,
        args.confirm_isolated_staging,
    )
    if args.confirm_paid_provider_call != EXECUTE_CONFIRMATION:
        raise CanaryFailure(f"付费调用确认值必须是 {EXECUTE_CONFIRMATION}")
    origin = _validate_origin(args.origin)
    revision = _safe_label(args.revision, "修订号")
    expected_model_id = _validate_model_id(args.expected_provider_model_id)
    if args.max_quoted_microtickets <= 0:
        raise CanaryFailure("最大预扣上限必须为正整数")
    try:
        parameters = json.loads(args.parameters_json)
    except json.JSONDecodeError as exc:
        raise CanaryFailure("canary 参数必须是 JSON 对象") from exc
    if not isinstance(parameters, dict):
        raise CanaryFailure("canary 参数必须是 JSON 对象")

    started_at = _iso_now()
    admin, admin_me = _admin_client(origin)
    state = _deployment_state(admin)
    isolation_hash = validate_isolation_manifest(
        args.isolation_manifest,
        environment,
        state.get("resource_fingerprints"),
    )
    active = _active_config(admin)
    _require_execute_state(
        state,
        active,
        args.capability,
        expected_model_id,
    )
    closed = admin.get_json(
        f"/api/v1/admin/configuration/versions/{args.closed_config_version_id}"
    )
    if not isinstance(closed, dict):
        raise CanaryFailure("关闭态配置响应无效")
    closed_flags = _feature_flags(closed)
    if (
        closed_flags.get("registration_mode") != "disabled"
        or closed_flags.get("new_ai_tasks_enabled") is not False
    ):
        raise CanaryFailure("指定回滚配置不是注册和 AI 双关闭态")

    run_error: Exception | None = None
    result: CanaryResult | None = None
    restored_config_id = ""
    try:
        user, user_id = _user_client(
            origin,
            "LUMENX_CANARY_USER_PHONE",
            "LUMENX_CANARY_USER_PASSWORD",
        )
        secondary, _secondary_user_id = _user_client(
            origin,
            "LUMENX_CANARY_SECONDARY_PHONE",
            "LUMENX_CANARY_SECONDARY_PASSWORD",
        )
        workspace_id = _workspace(user)
        secondary_workspace_id = _workspace(secondary)
        headers = {"X-Workspace-ID": workspace_id}
        uploaded = user.request(
            "POST",
            "/api/v1/media",
            expected=201,
            headers=headers,
            files={"file": ("canary.png", PNG_1X1, "image/png")},
        ).json()
        media_id = str(uploaded.get("id") or "")
        _positive_integer_id(media_id, "canary 媒体")
        secondary.request(
            "GET",
            f"/api/v1/media/{media_id}",
            expected=404,
            headers={"X-Workspace-ID": secondary_workspace_id},
        )
        access = user.get_json(
            f"/api/v1/media/{media_id}/access?expires_seconds=900",
            headers=headers,
        )
        if not isinstance(access, dict) or not access.get("url"):
            raise CanaryFailure("私有媒体未返回短期授权地址")
        policy_ttl = int(active["platform"]["operational"]["signed_media_url_seconds"])
        expires_at = datetime.fromisoformat(str(access["expires_at"]))
        effective_ttl = (expires_at - datetime.now(UTC)).total_seconds()
        if effective_ttl <= 0 or effective_ttl > policy_ttl + 2:
            raise CanaryFailure("私有媒体签名 TTL 超过激活策略")
        media_response = requests.get(str(access["url"]), timeout=30)
        if media_response.status_code != 200 or not media_response.content.startswith(b"\x89PNG"):
            raise CanaryFailure("真实私有 OSS 授权地址无法读取 canary 媒体")

        idempotency_key = f"staging-canary-{uuid.uuid4()}"
        submitted = user.request(
            "POST",
            "/api/v1/ai/generate",
            expected=202,
            headers={
                **headers,
                "Idempotency-Key": idempotency_key,
            },
            json={
                "capability": args.capability,
                "idempotency_key": idempotency_key,
                "resource_ids": {},
                "media_ids": [],
                "content": "LumenX isolated staging canary",
                "parameters": parameters,
            },
        ).json()
        quoted = int(submitted.get("quoted_microtickets") or -1)
        if quoted < 0 or quoted > args.max_quoted_microtickets:
            raise CanaryFailure("真实供应商 canary 预扣超过审核上限")
        if submitted.get("actual_model", {}).get("model_id") != expected_model_id:
            raise CanaryFailure("真实供应商 canary 未使用审核模型")
        task_id = str(submitted.get("task_id") or "")
        _positive_integer_id(task_id, "canary 任务")
        status = _poll_task(user, workspace_id, task_id, args.poll_timeout_seconds)
        if not status.get("media_ids"):
            raise CanaryFailure("真实供应商 canary 未持久化结果媒体")
        detail = user.get_json(
            f"/api/v1/ai/tasks/{task_id}",
            headers=headers,
        )
        if not isinstance(detail, dict) or detail.get("billing", {}).get("open_hold_ids"):
            raise CanaryFailure("真实供应商 canary 结束后仍有开放预扣")
        admin_id = str(admin_me["admin"]["id"])
        reconciliation, database_evidence = _database_evidence(
            admin_id,
            user_id,
            task_id,
        )
        if not reconciliation["is_consistent"]:
            raise CanaryFailure("真实供应商 canary 后算力券对账不一致")
        if database_evidence["metering_tokens"] <= 0:
            raise CanaryFailure("真实供应商 canary 未记录计量 token")
        result = CanaryResult(
            environment=environment,
            origin=origin,
            revision=revision,
            isolation_manifest_sha256=isolation_hash,
            status="awaiting_finalize",
            started_at=started_at,
            completed_at=_iso_now(),
            closed_config_version_id=str(closed["id"]),
            restored_config_version_id="",
            user_id=user_id,
            task_id=task_id,
            attempt_id=str(database_evidence["attempt_id"]),
            provider_request_id=_identifier_hash(
                database_evidence["provider_request_id"]
            ),
            provider_task_id=_identifier_hash(database_evidence["provider_task_id"]),
            media_ids=tuple(str(item) for item in status["media_ids"]),
            metering_tokens=int(database_evidence["metering_tokens"]),
            tokens_per_ticket=int(database_evidence["tokens_per_ticket"]),
            charged_microtickets=int(database_evidence["charged_microtickets"]),
            reconciliation_issue_count=int(reconciliation["issue_count"]),
        )
    except Exception as exc:  # noqa: BLE001 - rollback must run for every canary failure
        run_error = exc
    finally:
        try:
            restored = admin.request(
                "POST",
                f"/api/v1/admin/configuration/versions/{closed['id']}/rollback",
                expected=200,
                json={"reason": "staging canary 结束，恢复注册与 AI 双关闭配置"},
            ).json()
            restored_config_id = str(restored.get("id") or "")
            restored_flags = _feature_flags(restored)
            if (
                restored_flags.get("registration_mode") != "disabled"
                or restored_flags.get("new_ai_tasks_enabled") is not False
            ):
                raise CanaryFailure("canary 结束后数据库功能开关未恢复关闭")
        except Exception as exc:  # noqa: BLE001 - preserve both execution and rollback failures
            run_error = CanaryFailure(
                f"canary 关闭态恢复失败：{type(exc).__name__}"
            )

    if run_error is not None or result is None:
        _write_markdown(
            args.evidence_path,
            "LumenX staging canary 失败证据",
            [
                ("环境", f"`{environment}`"),
                ("产物修订", f"`{revision}`"),
                ("失败阶段", type(run_error).__name__ if run_error else "unknown"),
                ("数据库关闭态恢复", "已尝试；必须人工复核"),
            ],
            "NO-GO。保持注册和新 AI 入口关闭，"
            "检查任务、预扣、流水与供应商受理状态。",
        )
        if isinstance(run_error, CanaryFailure):
            raise run_error
        raise CanaryFailure(
            f"真实供应商 canary 失败：{type(run_error).__name__}"
        ) from run_error

    result = CanaryResult(
        **{
            **asdict(result),
            "restored_config_version_id": restored_config_id,
        }
    )
    state_payload = asdict(result)
    _write_json(args.state_path, state_payload)
    _write_markdown(
        args.evidence_path,
        "LumenX staging 真实云端 canary 证据",
        [
            ("环境", f"`{environment}`"),
            ("产物修订", f"`{revision}`"),
            ("隔离清单 SHA-256", f"`{isolation_hash}`"),
            ("任务", f"`{result.task_id}`"),
            ("尝试", f"`{result.attempt_id}`"),
            ("供应商 request 哈希", f"`{result.provider_request_id or 'none'}`"),
            ("供应商 task 哈希", f"`{result.provider_task_id or 'none'}`"),
            ("结果媒体数", str(len(result.media_ids))),
            ("计量 token", str(result.metering_tokens)),
            ("tokens_per_ticket", str(result.tokens_per_ticket)),
            ("结算 microtickets", str(result.charged_microtickets)),
            ("对账问题数", str(result.reconciliation_issue_count)),
            ("数据库关闭态恢复版本", f"`{restored_config_id}`"),
        ],
        "真实 canary 已通过，但尚未完成 finalize。"
        "必须先把两个部署熔断恢复为 true，再生成最终 GO 证据。",
    )
    print(f"真实 staging canary 已执行，等待 finalize：{args.state_path}")
    return 0


def finalize(args: argparse.Namespace) -> int:
    environment = _validate_staging_identity(
        args.environment,
        args.confirm_isolated_staging,
    )
    origin = _validate_origin(args.origin)
    revision = _safe_label(args.revision, "修订号")
    try:
        state_payload = json.loads(args.state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanaryFailure("无法读取 execute 阶段的脱敏状态文件") from exc
    admin, admin_me = _admin_client(origin)
    deployment = _deployment_state(admin)
    isolation_hash = validate_isolation_manifest(
        args.isolation_manifest,
        environment,
        deployment.get("resource_fingerprints"),
    )
    expected = {
        "environment": environment,
        "origin": origin,
        "revision": revision,
        "isolation_manifest_sha256": isolation_hash,
        "status": "awaiting_finalize",
    }
    mismatches = [key for key, value in expected.items() if state_payload.get(key) != value]
    if mismatches:
        raise CanaryFailure(f"execute 状态与 finalize 目标不一致：{', '.join(mismatches)}")
    active = _active_config(admin)
    _require_closed_state(deployment, active)
    reconciliation, _database_values = _database_evidence(
        str(admin_me["admin"]["id"]),
        str(state_payload["user_id"]),
        str(state_payload["task_id"]),
    )
    if not reconciliation["is_consistent"]:
        raise CanaryFailure("finalize 对账不一致")
    state_payload["status"] = "passed"
    state_payload["finalized_at"] = _iso_now()
    state_payload["final_active_config_version_id"] = str(active["id"])
    state_payload["final_reconciliation_issue_count"] = int(
        reconciliation["issue_count"]
    )
    _write_json(args.state_path, state_payload)
    _write_markdown(
        args.evidence_path,
        "LumenX staging canary 最终证据",
        [
            ("环境", f"`{environment}`"),
            ("产物修订", f"`{revision}`"),
            ("隔离清单 SHA-256", f"`{isolation_hash}`"),
            ("任务", f"`{state_payload['task_id']}`"),
            ("计量 token", str(state_payload["metering_tokens"])),
            ("结算 microtickets", str(state_payload["charged_microtickets"])),
            ("最终激活关闭态配置", f"`{active['id']}`"),
            ("注册部署熔断", "已关闭入口"),
            ("AI 部署熔断", "已关闭入口"),
            ("最终对账问题数", str(reconciliation["issue_count"])),
        ],
        "GO（仅限邀请制灰度）。可以进入 invite_only 开放决策，"
        "不得开放 verified_open 或付费充值。",
    )
    print(f"staging canary 最终门禁通过：{args.evidence_path}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="执行受保护的 LumenX 真实 staging OSS/供应商 canary",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--environment", required=True)
        subparser.add_argument("--origin", required=True)
        subparser.add_argument("--revision", required=True)
        subparser.add_argument("--isolation-manifest", type=Path, required=True)
        subparser.add_argument("--evidence-path", type=Path, required=True)

    preflight_parser = subparsers.add_parser("preflight")
    common(preflight_parser)
    preflight_parser.set_defaults(handler=preflight)

    execute_parser = subparsers.add_parser("execute")
    common(execute_parser)
    execute_parser.add_argument("--confirm-isolated-staging", required=True)
    execute_parser.add_argument("--confirm-paid-provider-call", required=True)
    execute_parser.add_argument("--closed-config-version-id", required=True)
    execute_parser.add_argument("--expected-provider-model-id", required=True)
    execute_parser.add_argument("--capability", default="image.t2i")
    execute_parser.add_argument("--parameters-json", default="{}")
    execute_parser.add_argument("--max-quoted-microtickets", type=int, required=True)
    execute_parser.add_argument("--poll-timeout-seconds", type=int, default=900)
    execute_parser.add_argument("--state-path", type=Path, required=True)
    execute_parser.set_defaults(handler=execute)

    finalize_parser = subparsers.add_parser("finalize")
    common(finalize_parser)
    finalize_parser.add_argument("--confirm-isolated-staging", required=True)
    finalize_parser.add_argument("--state-path", type=Path, required=True)
    finalize_parser.set_defaults(handler=finalize)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        return int(args.handler(args))
    except CanaryFailure as exc:
        print(f"staging canary 未通过：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
