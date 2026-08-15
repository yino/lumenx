#!/usr/bin/env python3
from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from sqlalchemy import func, select, text

from src.platform.auth.sessions import CSRF_COOKIE_NAME
from src.platform.auth.invitations import hash_invitation_secret
from src.platform.bootstrap_admin import PlatformAdminBootstrapService
from src.platform.configuration_schemas import ConfigurationDraft
from src.platform.configuration_service import ConfigurationService
from src.platform.contracts import UserContext
from src.platform.database import Database, set_transaction_invitation_hash
from src.platform.db_models import (
    AITaskRecord,
    AuthSessionRecord,
    RegistrationInvitationRecord,
    WorkspaceRecord,
)
from src.platform.maintenance import RetentionCleanupService, TaskHoldMaintenanceService
from src.platform.runtime_policy import RuntimePolicyResolver
from src.platform.settings import DeploymentMode, get_deployment_settings
from src.platform.test_adapters import DeterministicPrivateObjectStore
from src.platform.ticket_reconciliation import TicketReconciliationService


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
)
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class SmokeFailure(RuntimeError):
    pass


class RecordingRecoveryDispatcher:
    def __init__(self) -> None:
        self.task_ids: list[str] = []

    def dispatch(self, task_id: str) -> None:
        self.task_ids.append(task_id)


class EdgeSession:
    def __init__(self, origin: str, checks: list[str]) -> None:
        self.origin = origin.rstrip("/")
        self.session = requests.Session()
        self.checks = checks

    def _cookie_header(self) -> str | None:
        values = self.session.cookies.get_dict()
        if not values:
            return None
        return "; ".join(f"{name}={value}" for name, value in values.items())

    def request(
        self,
        method: str,
        path: str,
        *,
        expected: int | set[int],
        label: str,
        csrf: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        method = method.upper()
        headers = dict(kwargs.pop("headers", {}))
        cookie_header = self._cookie_header()
        if cookie_header:
            headers["Cookie"] = cookie_header
        if method in UNSAFE_METHODS:
            headers.setdefault("Origin", self.origin)
            csrf_token = self.session.cookies.get(CSRF_COOKIE_NAME)
            if csrf and csrf_token:
                headers.setdefault("X-CSRF-Token", csrf_token)
        response = self.session.request(
            method,
            f"{self.origin}{path}",
            headers=headers,
            timeout=30,
            **kwargs,
        )
        expected_codes = {expected} if isinstance(expected, int) else expected
        if response.status_code not in expected_codes:
            raise SmokeFailure(
                f"{label}失败：HTTP {response.status_code}，响应 {response.text[:500]}"
            )
        if path.startswith("/api/v1"):
            content_type = response.headers.get("content-type", "").lower()
            if "application/json" not in content_type:
                raise SmokeFailure(f"{label}未返回 JSON：{content_type}")
        self.checks.append(label)
        return response


def _draft(*, tokens_per_ticket: int, reason: str) -> dict[str, Any]:
    return {
        "reason": reason,
        "platform": {
            "tokens_per_ticket": tokens_per_ticket,
            "registration_initial_grant_microtickets": 10_000_000,
            "session_idle_seconds": 3600,
            "session_absolute_seconds": 86400,
            "max_sessions_per_user": 2,
            "max_ai_concurrency_per_user": 2,
            "exposed_capabilities": ["image.t2i"],
            "feature_flags": {
                "registration_mode": "invite_only",
                "new_ai_tasks_enabled": True,
            },
            "operational": {
                "signed_media_url_seconds": 120,
                "soft_delete_retention_days": 30,
                "stale_hold_minutes": 30,
            },
        },
        "routes": [
            {
                "capability": "image.t2i",
                "display_name_zh": "确定性发布测试图像模型",
                "provider": "deterministic",
                "provider_model_id": "release-test-image-v1",
                "enabled": True,
                "is_primary": True,
                "priority": 1,
                "default_parameters": {
                    "count": 1,
                    "resolution": "1024x1024",
                },
                "parameter_schema": [
                    {
                        "name": "count",
                        "value_type": "integer",
                        "minimum": 1,
                        "maximum": 2,
                    },
                    {
                        "name": "resolution",
                        "value_type": "string",
                        "choices": ["1024x1024"],
                    },
                ],
                "metering_formula": {
                    "kind": "image",
                    "base_tokens": 10,
                    "per_image_tokens": 100,
                    "max_images": 2,
                    "resolution_multipliers": {"1024x1024": 1},
                    "option_multipliers": {},
                },
                "fallback_policy": {"enabled": False, "max_attempts": 1},
                "secret_ref": "RELEASE_TEST_PROVIDER_SECRET",
            }
        ],
    }


def _login(origin: str, checks: list[str], phone: str, password: str, label: str) -> EdgeSession:
    client = EdgeSession(origin, checks)
    response = client.request(
        "POST",
        "/api/v1/auth/login",
        expected=200,
        label=label,
        json={"phone": phone, "password": password},
    )
    cookies = response.headers.get("set-cookie", "")
    if "Secure" not in cookies or "HttpOnly" not in cookies or "SameSite=lax" not in cookies:
        raise SmokeFailure("登录 Cookie 缺少 Secure、HttpOnly 或 SameSite 属性")
    checks.append(f"{label}安全 Cookie")
    return client


def _create_invitation(
    admin: EdgeSession,
    phone: str,
    label: str,
    *,
    expires_at: datetime | None = None,
) -> tuple[str, str]:
    response = admin.request(
        "POST",
        "/api/v1/auth/admin/invitations",
        expected=201,
        label=label,
        json={
            "phone": phone,
            "expires_at": (
                expires_at or datetime.now(UTC) + timedelta(hours=1)
            ).isoformat(),
            "reason": "发布测试一次性邀请",
        },
    )
    payload = response.json()
    code = payload.get("invitation_code")
    if not isinstance(code, str) or not code:
        raise SmokeFailure("邀请接口未一次性返回邀请码")
    invitation_id = payload.get("id")
    if not isinstance(invitation_id, str) or not invitation_id:
        raise SmokeFailure("邀请接口未返回邀请标识")
    return code, invitation_id


def _register(
    origin: str,
    checks: list[str],
    phone: str,
    password: str,
    code: str,
    label: str,
) -> EdgeSession:
    client = EdgeSession(origin, checks)
    client.request(
        "POST",
        "/api/v1/auth/register",
        expected=201,
        label=label,
        json={"phone": phone, "password": password, "invitation_code": code},
    )
    return client


def _workspace(client: EdgeSession, label: str) -> str:
    response = client.request(
        "GET",
        "/api/v1/workspaces",
        expected=200,
        label=label,
    )
    items = response.json()
    if not isinstance(items, list) or len(items) != 1:
        raise SmokeFailure("注册用户没有且仅有一个默认工作区")
    return str(items[0]["id"])


def _poll_task(client: EdgeSession, workspace_id: str, task_id: str, label: str) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        response = client.request(
            "GET",
            f"/api/v1/ai/tasks/{task_id}/status",
            expected=200,
            label=f"{label}状态查询",
            headers={"X-Workspace-ID": workspace_id},
        )
        payload = response.json()
        if payload["status"] == "succeeded":
            return payload
        if payload["status"] in {"failed", "cancelled", "support_review"}:
            raise SmokeFailure(f"{label}进入异常终态：{payload}")
        time.sleep(0.5)
    raise SmokeFailure(f"{label}等待超时")


def _submit_task(
    client: EdgeSession,
    workspace_id: str,
    key: str,
    expected_rate: str,
    label: str,
) -> tuple[str, dict[str, Any]]:
    response = client.request(
        "POST",
        "/api/v1/ai/generate",
        expected=202,
        label=f"{label}提交",
        headers={"X-Workspace-ID": workspace_id, "Idempotency-Key": key},
        json={
            "capability": "image.t2i",
            "idempotency_key": key,
            "resource_ids": {},
            "media_ids": [],
            "content": "发布测试画面",
            "parameters": {"count": 1},
        },
    )
    payload = response.json()
    if payload["tokens_per_ticket"] != expected_rate:
        raise SmokeFailure(f"{label}未使用预期算力券汇率")
    task_id = str(payload["task_id"])
    return task_id, _poll_task(client, workspace_id, task_id, label)


def _write_evidence(
    *,
    path: Path,
    checks: list[str],
    migration_head: str,
    config_version: str,
    task_ids: list[str],
    reconciliation: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    revision = os.getenv("LUMENX_RELEASE_REVISION", "local")
    test_summary = os.getenv("LUMENX_RELEASE_TEST_SUMMARY", "部署栈 smoke 已通过")
    lines = [
        "# LumenX 云端发布证据",
        "",
        f"- 产物修订：`{revision}`",
        f"- 数据库迁移 head：`{migration_head}`",
        f"- 最终激活配置版本：`{config_version}`",
        f"- 自动化测试摘要：{test_summary}",
        f"- 边缘与业务检查数：{len(checks)}",
        f"- 确定性 canary 任务：{', '.join(f'`{item}`' for item in task_ids)}",
        "- 真实 OSS / 供应商 canary：未在确定性 CI 中执行，发布 staging 必须另行填写",
        f"- 算力券对账：{'一致' if reconciliation['is_consistent'] else '不一致'}",
        f"- 对账问题数：{reconciliation['issue_count']}",
        "",
        "## 已通过检查",
        "",
        *[f"- {item}" for item in checks],
        "",
        "## 发布结论",
        "",
        "确定性部署栈门禁已通过。真实 OSS 和低成本供应商 canary 完成前，",
        "注册及新 AI 工作仍应保持关闭；canary 和最终对账通过后，只开放邀请注册。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    settings = get_deployment_settings()
    if settings.deployment_mode is not DeploymentMode.TEST or not settings.database_url:
        raise SmokeFailure("部署栈 smoke 只能在发布测试模式中运行")
    origin = os.getenv("LUMENX_RELEASE_ORIGIN", "http://frontend").rstrip("/")
    evidence_path = Path(
        os.getenv("LUMENX_RELEASE_EVIDENCE_PATH", "/artifacts/cloud-release-evidence.md")
    )
    checks: list[str] = []
    database = Database(settings.database_url)
    admin_phone = "13800138000"
    admin_password = "ReleaseSmoke!2026-Admin"
    user_password = "ReleaseSmoke!2026-User"
    try:
        bootstrap = PlatformAdminBootstrapService(
            database,
            settings.session_secret.get_secret_value(),
        ).bootstrap(admin_phone, admin_password)
        admin_identity = UserContext(
            user_id=bootstrap.user_id,
            session_id="release-smoke-setup",
            is_platform_admin=True,
        )
        configuration = ConfigurationService(database)
        initial = configuration.create_version(
            admin_identity,
            ConfigurationDraft.model_validate(
                _draft(tokens_per_ticket=1000, reason="发布测试初始配置")
            ),
        )
        configuration.validate_version(admin_identity, initial.id)
        configuration.activate_version(
            admin_identity,
            initial.id,
            reason="启动发布测试栈",
        )
        warmed_initial = configuration.get_active(admin_identity)
        if warmed_initial.id != initial.id:
            raise SmokeFailure("配置读取器未缓存初始激活版本")

        edge = EdgeSession(origin, checks)
        edge.request("GET", "/api/v1/health", expected=200, label="边缘健康检查")
        edge.request("GET", "/api/v1/ready", expected=200, label="边缘就绪检查")
        unknown = edge.request(
            "GET",
            "/api/v1/release-smoke-missing",
            expected=404,
            label="未知 API JSON 404",
        )
        if "text/html" in unknown.headers.get("content-type", "").lower():
            raise SmokeFailure("未知 API 错误回退到了 SPA")
        spa = requests.get(f"{origin}/release-smoke-client-route", timeout=30)
        if spa.status_code != 200 or "text/html" not in spa.headers.get("content-type", ""):
            raise SmokeFailure("SPA 路由与 API 命名空间未正确分离")
        checks.append("SPA 路由分离")

        admin = _login(origin, checks, admin_phone, admin_password, "管理员登录")
        invitation1, _ = _create_invitation(admin, "13800138001", "创建用户一邀请")
        invitation2, _ = _create_invitation(admin, "13800138002", "创建用户二邀请")
        rollback_invitation, _ = _create_invitation(
            admin,
            "13800138003",
            "创建事务回滚邀请",
        )
        concurrent_invitation, _ = _create_invitation(
            admin,
            "13800138004",
            "创建并发消费邀请",
        )
        mismatch_invitation, _ = _create_invitation(
            admin,
            "13800138005",
            "创建手机号错配邀请",
        )
        revoked_invitation, revoked_invitation_id = _create_invitation(
            admin,
            "13800138006",
            "创建待撤销邀请",
        )
        expiry_time = datetime.now(UTC) + timedelta(seconds=2)
        expired_invitation, _ = _create_invitation(
            admin,
            "13800138007",
            "创建短期邀请",
            expires_at=expiry_time,
        )
        rls_invitation, rls_invitation_id = _create_invitation(
            admin,
            "13800138008",
            "创建 RLS 验证邀请",
        )
        invitation_list = admin.request(
            "GET",
            "/api/v1/auth/admin/invitations",
            expected=200,
            label="管理员邀请列表",
        ).json()
        if any(item.get("invitation_code") for item in invitation_list):
            raise SmokeFailure("邀请列表不应回显邀请码明文")
        admin.request(
            "POST",
            f"/api/v1/auth/admin/invitations/{revoked_invitation_id}/revoke",
            expected=200,
            label="管理员撤销邀请",
            json={"reason": "验证撤销后不可注册"},
        )
        user1_registered = _register(
            origin,
            checks,
            "13800138001",
            user_password,
            invitation1,
            "邀请注册用户一",
        )
        replay = EdgeSession(origin, checks).request(
            "POST",
            "/api/v1/auth/register",
            expected=409,
            label="已消费邀请重放拒绝",
            json={
                "phone": "13800138001",
                "password": user_password,
                "invitation_code": invitation1,
            },
        )
        if replay.json().get("code") != "INVITATION_INVALID":
            raise SmokeFailure("已消费邀请重放泄露了账号或邀请状态")
        user2 = _register(
            origin,
            checks,
            "13800138002",
            user_password,
            invitation2,
            "邀请注册用户二",
        )
        mismatch = EdgeSession(origin, checks).request(
            "POST",
            "/api/v1/auth/register",
            expected=409,
            label="邀请手机号错配拒绝",
            json={
                "phone": "13900139005",
                "password": user_password,
                "invitation_code": mismatch_invitation,
            },
        )
        if mismatch.json().get("code") != "INVITATION_INVALID":
            raise SmokeFailure("邀请手机号错配响应泄露了绑定信息")
        _register(
            origin,
            checks,
            "13800138005",
            user_password,
            mismatch_invitation,
            "手机号错配后邀请可安全重试",
        )
        revoked = EdgeSession(origin, checks).request(
            "POST",
            "/api/v1/auth/register",
            expected=409,
            label="已撤销邀请拒绝",
            json={
                "phone": "13800138006",
                "password": user_password,
                "invitation_code": revoked_invitation,
            },
        )
        if revoked.json().get("code") != "INVITATION_INVALID":
            raise SmokeFailure("已撤销邀请响应泄露了邀请状态")
        time.sleep(max((expiry_time - datetime.now(UTC)).total_seconds() + 0.25, 0))
        expired = EdgeSession(origin, checks).request(
            "POST",
            "/api/v1/auth/register",
            expected=409,
            label="已过期邀请拒绝",
            json={
                "phone": "13800138007",
                "password": user_password,
                "invitation_code": expired_invitation,
            },
        )
        if expired.json().get("code") != "INVITATION_INVALID":
            raise SmokeFailure("已过期邀请响应泄露了邀请状态")
        preauth_identity = UserContext(
            user_id=str(uuid.UUID(int=0)),
            session_id="release-smoke-preauth",
        )
        with database.transaction(preauth_identity) as session:
            set_transaction_invitation_hash(
                session,
                hash_invitation_secret(
                    rls_invitation,
                    settings.session_secret.get_secret_value(),
                ),
            )
            visible_invitation_ids = tuple(
                str(identifier)
                for identifier in session.scalars(
                    select(RegistrationInvitationRecord.id)
                )
            )
        if visible_invitation_ids != (rls_invitation_id,):
            raise SmokeFailure(
                f"预认证邀请 RLS 暴露范围异常：{visible_invitation_ids}"
            )
        checks.append("PostgreSQL 预认证邀请 RLS")
        rollback_client = EdgeSession(origin, checks)
        rollback_client.request(
            "POST",
            "/api/v1/auth/register",
            expected=422,
            label="注册失败不消费邀请",
            json={
                "phone": "13800138003",
                "password": "weak",
                "invitation_code": rollback_invitation,
            },
        )
        _register(
            origin,
            checks,
            "13800138003",
            user_password,
            rollback_invitation,
            "失败后邀请可安全重试",
        )

        def concurrent_register(index: int) -> int:
            candidate = EdgeSession(origin, checks)
            response = candidate.request(
                "POST",
                "/api/v1/auth/register",
                expected={201, 409},
                label=f"并发邀请消费请求 {index}",
                json={
                    "phone": "13800138004",
                    "password": user_password,
                    "invitation_code": concurrent_invitation,
                },
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_statuses = sorted(executor.map(concurrent_register, (1, 2)))
        if concurrent_statuses != [201, 409]:
            raise SmokeFailure(f"同一邀请并发消费结果异常：{concurrent_statuses}")
        checks.append("PostgreSQL 邀请单次并发消费")
        workspace1 = _workspace(user1_registered, "用户一工作区可见")
        workspace2 = _workspace(user2, "用户二工作区可见")
        user1_id = user1_registered.request(
            "GET",
            "/api/v1/auth/me",
            expected=200,
            label="用户一身份可见",
        ).json()["user"]["id"]
        user2_id = user2.request(
            "GET",
            "/api/v1/auth/me",
            expected=200,
            label="用户二身份可见",
        ).json()["user"]["id"]
        user1_registered.request(
            "POST",
            f"/api/v1/workspaces/{workspace1}/select",
            expected=200,
            label="CSRF 工作区选择",
        )
        user1_registered.request(
            "POST",
            f"/api/v1/workspaces/{workspace2}/select",
            expected=404,
            label="跨用户工作区选择拒绝",
        )
        user1_registered.request(
            "POST",
            "/api/v1/workspaces",
            expected=403,
            label="缺少 CSRF 的写请求被拒绝",
            csrf=False,
            json={"name": "不应创建"},
        )
        user1_registered.request("GET", "/api/v1/wallet", expected=200, label="用户钱包可见")
        user2.request("GET", "/api/v1/admin/users", expected=403, label="普通用户管理端拒绝")
        with database.transaction(
            UserContext(user_id=user1_id, session_id="release-smoke-user-rls")
        ) as session:
            foreign_workspace = session.scalar(
                select(WorkspaceRecord.id).where(
                    WorkspaceRecord.id == uuid.UUID(workspace2)
                )
            )
            exposed_invitation = session.scalar(
                select(RegistrationInvitationRecord.id).limit(1)
            )
        if foreign_workspace is not None or exposed_invitation is not None:
            raise SmokeFailure("普通用户 PostgreSQL RLS 暴露了他人工作区或注册邀请")
        checks.append("PostgreSQL 用户与邀请 RLS 隔离")

        upload = user1_registered.request(
            "POST",
            "/api/v1/media",
            expected=201,
            label="私有媒体上传",
            headers={"X-Workspace-ID": workspace1},
            files={"file": ("pixel.png", PNG_1X1, "image/png")},
        ).json()
        media_id = str(upload["id"])
        user2.request(
            "GET",
            f"/api/v1/media/{media_id}",
            expected=404,
            label="跨用户媒体拒绝",
            headers={"X-Workspace-ID": workspace2},
        )
        access_requested_at = datetime.now(UTC)
        access = user1_registered.request(
            "GET",
            f"/api/v1/media/{media_id}/access?expires_seconds=900",
            expected=200,
            label="私有媒体授权",
            headers={"X-Workspace-ID": workspace1},
        ).json()
        access_expires_at = datetime.fromisoformat(access["expires_at"])
        effective_ttl = (access_expires_at - access_requested_at).total_seconds()
        if not 0 < effective_ttl <= 121:
            raise SmokeFailure(f"签名媒体 TTL 未受配置上限约束：{effective_ttl:.3f} 秒")
        if access.get("policy_config_version_id") != str(initial.id):
            raise SmokeFailure("签名媒体授权未记录当前配置版本")
        checks.append("签名媒体 TTL 与配置版本")
        media = requests.get(urljoin(origin, access["url"]), timeout=30)
        if media.status_code != 200 or not media.content.startswith(b"\x89PNG"):
            raise SmokeFailure("签名媒体地址无法通过边缘读取")
        checks.append("签名媒体读取")

        task_a, task_a_status = _submit_task(
            user1_registered,
            workspace1,
            "release-smoke-config-a",
            "1000",
            "配置 A AI 任务",
        )
        if not task_a_status.get("media_ids"):
            raise SmokeFailure("配置 A AI 任务未生成私有媒体")

        draft_b = _draft(tokens_per_ticket=2000, reason="发布测试配置传播")
        created_b = admin.request(
            "POST",
            "/api/v1/admin/configuration/versions",
            expected=201,
            label="管理员创建配置 B",
            json=draft_b,
        ).json()
        version_b = str(created_b["id"])
        admin.request(
            "POST",
            f"/api/v1/admin/configuration/versions/{version_b}/validate",
            expected=200,
            label="管理员校验配置 B",
        )
        admin.request(
            "POST",
            f"/api/v1/admin/configuration/versions/{version_b}/activate",
            expected=200,
            label="管理员激活配置 B",
            json={"reason": "验证跨进程配置传播"},
        )
        refreshed = configuration.get_active(admin_identity)
        if refreshed.id != version_b:
            raise SmokeFailure("已缓存的独立配置读取器未观察到配置 B")
        checks.append("PostgreSQL 跨实例配置激活可见")
        task_b, task_b_status = _submit_task(
            user1_registered,
            workspace1,
            "release-smoke-config-b",
            "2000",
            "配置 B AI 任务",
        )
        if not task_b_status.get("media_ids"):
            raise SmokeFailure("配置 B AI 任务未生成私有媒体")

        user1_registered.request(
            "GET",
            "/api/v1/wallet/history?view=usage",
            expected=200,
            label="AI 结算历史可见",
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            login2, login3 = tuple(
                executor.map(
                    lambda item: _login(
                        origin,
                        checks,
                        "13800138001",
                        user_password,
                        f"用户一并发会话 {item}",
                    ),
                    (2, 3),
                )
            )
        user1_registered.request(
            "GET",
            "/api/v1/auth/me",
            expected=401,
            label="最旧会话按上限撤销",
        )
        login3.request("GET", "/api/v1/auth/me", expected=200, label="最新会话仍有效")
        with database.transaction(
            UserContext(user_id=user1_id, session_id="release-smoke-session-limit")
        ) as session:
            active_sessions = session.scalar(
                select(func.count(AuthSessionRecord.id)).where(
                    AuthSessionRecord.user_id == user1_id,
                    AuthSessionRecord.revoked_at.is_(None),
                )
            )
        if active_sessions != 2:
            raise SmokeFailure(f"并发登录后活跃会话数应为 2，实际为 {active_sessions}")
        checks.append("PostgreSQL 会话上限并发执行")
        login2.request(
            "POST",
            "/api/v1/auth/logout",
            expected=200,
            label="用户登出",
        )

        with database.transaction(admin_identity) as session:
            records = {
                str(record.id): record
                for record in session.scalars(
                    select(AITaskRecord).where(AITaskRecord.id.in_([task_a, task_b]))
                )
            }
            if records[task_a].config_snapshot["config_version_id"] == version_b:
                raise SmokeFailure("旧任务配置快照被新配置覆盖")
            if records[task_b].config_snapshot["config_version_id"] != version_b:
                raise SmokeFailure("新任务未观察到配置 B")
        checks.append("跨进程配置传播与旧任务快照保留")

        maintenance_checked_at = datetime.now(UTC)
        expired_workspace_id = uuid.uuid4()
        retained_workspace_id = uuid.uuid4()
        with database.transaction(admin_identity) as session:
            session.add_all(
                [
                    WorkspaceRecord(
                        id=expired_workspace_id,
                        user_id=uuid.UUID(user2_id),
                        name="发布测试已过保留期工作区",
                        deleted_at=maintenance_checked_at - timedelta(days=31),
                    ),
                    WorkspaceRecord(
                        id=retained_workspace_id,
                        user_id=uuid.UUID(user2_id),
                        name="发布测试保留期内工作区",
                        deleted_at=maintenance_checked_at - timedelta(days=29),
                    ),
                ]
            )
        runtime_policy = RuntimePolicyResolver(database, verification_available=False)
        retention_report = RetentionCleanupService(
            database,
            DeterministicPrivateObjectStore(settings),
            runtime_policy=runtime_policy,
        ).run(admin_identity, now=maintenance_checked_at)
        with database.transaction(admin_identity) as session:
            expired_workspace = session.get(WorkspaceRecord, expired_workspace_id)
            retained_workspace = session.get(WorkspaceRecord, retained_workspace_id)
        if expired_workspace is not None or retained_workspace is None:
            raise SmokeFailure("PostgreSQL 保留期策略未按 30 天边界执行")
        if (
            retention_report.policy_config_version_id != version_b
            or retention_report.retention_days != 30
        ):
            raise SmokeFailure("保留期维护未记录配置 B 及其阈值")
        checks.append("PostgreSQL 配置化保留期维护")

        recovery_dispatcher = RecordingRecoveryDispatcher()
        hold_report = TaskHoldMaintenanceService(
            database,
            recovery_dispatcher,
            runtime_policy=runtime_policy,
        ).run(admin_identity, now=maintenance_checked_at)
        if (
            hold_report.policy_config_version_id != version_b
            or hold_report.stale_hold_minutes != 30
            or not hold_report.reconciliation_consistent
        ):
            raise SmokeFailure("stale-hold 维护未采用配置 B 或对账不一致")
        checks.append("PostgreSQL stale-hold 策略与对账")

        report = TicketReconciliationService(database).reconcile(admin_identity).as_dict()
        if not report["is_consistent"]:
            raise SmokeFailure(f"算力券对账失败：{report['issues']}")
        checks.append("算力券与任务对账")
        with database.session_factory() as session:
            migration_head = str(session.scalar(text("SELECT version_num FROM alembic_version")))
        _write_evidence(
            path=evidence_path,
            checks=checks,
            migration_head=migration_head,
            config_version=version_b,
            task_ids=[task_a, task_b],
            reconciliation=report,
        )
        print(f"部署栈验收通过：{len(checks)} 项，证据文件 {evidence_path}")
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
