"""Typed contracts shared by Agent workflows.

The Agent layer deliberately owns domain state only. Provider credentials,
signed URLs and network clients remain outside these models.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AgentStage(str, Enum):
    INTAKE = "intake"
    STORYBOARD_PLAN = "storyboard_plan"
    CONTINUITY_CHECK = "continuity_check"
    ASSET_BINDING = "asset_binding"
    PROMPT_COMPILE = "prompt_compile"
    QUALITY_GATE = "quality_gate"
    HUMAN_APPROVAL = "human_approval"
    SUBMIT_TASK = "submit_task"
    MONITOR_TASK = "monitor_task"


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"


class AgentRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_APPROVAL = "needs_approval"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ApprovalDecision(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TaskStatus(str, Enum):
    NOT_SUBMITTED = "not_submitted"
    SUBMITTED = "submitted"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FindingSeverity(str, Enum):
    BLOCKING = "blocking"
    WARNING = "warning"
    SUGGESTION = "suggestion"


class ReferenceType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"


class AuthorizationStatus(str, Enum):
    UNKNOWN = "unknown"
    AUTHORIZED = "authorized"
    UNAUTHORIZED = "unauthorized"
    EXPIRED = "expired"


class AgentContractError(ValueError):
    """Raised when a state or node contract cannot be accepted."""


class InvalidStageTransitionError(AgentContractError):
    pass


class SensitiveDataError(AgentContractError):
    pass


class TimelineSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: float = Field(ge=0)
    end: float = Field(gt=0)
    action: str = Field(min_length=1, max_length=4000)
    camera: str = ""
    audio: str = ""

    @model_validator(mode="after")
    def validate_interval(self) -> "TimelineSegment":
        if self.end <= self.start:
            raise ValueError("时间段结束时间必须大于开始时间")
        return self


class ShotReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1, max_length=120)
    purpose: str = Field(default="visual reference", min_length=1, max_length=500)
    type: ReferenceType = ReferenceType.IMAGE
    order: int | None = Field(default=None, ge=1)


class ShotPackage(BaseModel):
    """Provider-neutral description of one short-drama shot."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    shot_id: str = Field(min_length=1, max_length=120)
    generation_mode: str = Field(default="r2v", pattern=r"^(t2v|i2v|r2v)$")
    duration: float = Field(gt=0, le=600)
    aspect_ratio: str = Field(default="16:9", min_length=3, max_length=16)
    purpose: str = ""
    subject: str = ""
    scene: str = ""
    start_state: str = ""
    end_state: str = ""
    actions: list[str] = Field(default_factory=list, max_length=64)
    camera: str = ""
    dialogue: str = ""
    audio: str = ""
    style: str = ""
    timeline: list[TimelineSegment] = Field(default_factory=list, max_length=120)
    references: list[ShotReference] = Field(default_factory=list, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("shot_id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", normalized):
            raise ValueError("镜头 ID 格式无效")
        return normalized


class AssetReference(BaseModel):
    """Canonical reference bound to an application-owned media/resource ID."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1, max_length=120)
    type: ReferenceType
    purpose: str = Field(min_length=1, max_length=500)
    source: str = Field(default="application_asset", min_length=1, max_length=120)
    media_id: str | None = Field(default=None, min_length=1, max_length=64)
    resource_id: str | None = Field(default=None, min_length=1, max_length=64)
    authorization: AuthorizationStatus = AuthorizationStatus.UNKNOWN
    order: int = Field(default=1, ge=1)
    semantic_role: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_binding(self) -> "AssetReference":
        if self.authorization is AuthorizationStatus.AUTHORIZED and not (
            self.media_id or self.resource_id
        ):
            raise ValueError("已授权素材必须绑定 media_id 或 resource_id")
        return self


class ValidationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: FindingSeverity
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=2000)
    validator: str = Field(min_length=1, max_length=120)
    shot_id: str | None = Field(default=None, max_length=120)
    path: str | None = Field(default=None, max_length=300)
    details: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: str = Field(default="passed", pattern=r"^(passed|needs_approval|blocked)$")
    findings: list[ValidationFinding] = Field(default_factory=list, max_length=1000)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def derive_status(self) -> "ValidationReport":
        severities = {finding.severity for finding in self.findings}
        if FindingSeverity.BLOCKING in severities:
            self.status = "blocked"
        elif FindingSeverity.WARNING in severities:
            self.status = "needs_approval"
        else:
            self.status = "passed"
        return self

    @property
    def blocking(self) -> list[ValidationFinding]:
        return [f for f in self.findings if f.severity is FindingSeverity.BLOCKING]

    @property
    def warnings(self) -> list[ValidationFinding]:
        return [f for f in self.findings if f.severity is FindingSeverity.WARNING]


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision = ApprovalDecision.PENDING
    actor_id: str | None = None
    reason: str | None = Field(default=None, max_length=2000)
    decided_at: datetime | None = None


class StageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: AgentStage
    status: StageStatus
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_ms: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    message: str | None = Field(default=None, max_length=2000)


class AgentRunState(BaseModel):
    """Versioned, JSON-safe state passed between Agent nodes."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: int = Field(default=1, ge=1)
    run_id: str = Field(min_length=1, max_length=120)
    owner_user_id: str | None = Field(default=None, max_length=120)
    project_id: str = Field(min_length=1, max_length=120)
    workspace_id: str = Field(min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=1, max_length=160)
    skill: str = Field(default="short-drama-production", min_length=1, max_length=120)
    skill_alias: str | None = Field(default=None, max_length=120)
    target_profile: str = Field(min_length=1, max_length=120)
    generation_mode: str = Field(default="r2v", pattern=r"^(t2v|i2v|r2v)$")
    status: AgentRunStatus = AgentRunStatus.QUEUED
    current_stage: AgentStage = AgentStage.INTAKE
    stage_status: StageStatus = StageStatus.PENDING
    shots: list[ShotPackage] = Field(default_factory=list, max_length=500)
    asset_references: list[AssetReference] = Field(default_factory=list, max_length=1000)
    validation_report: ValidationReport = Field(default_factory=ValidationReport)
    approval: ApprovalRecord = Field(default_factory=ApprovalRecord)
    task_status: TaskStatus = TaskStatus.NOT_SUBMITTED
    submitted_task_ids: list[str] = Field(default_factory=list, max_length=500)
    stage_events: list[StageEvent] = Field(default_factory=list, max_length=5000)
    audit_events: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)
    input_summary: dict[str, Any] = Field(default_factory=dict)
    compiled_package: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", normalized):
            raise ValueError("幂等键格式无效")
        return normalized

    @model_validator(mode="after")
    def validate_terminal_state(self) -> "AgentRunState":
        if self.status is AgentRunStatus.COMPLETED and self.task_status not in {
            TaskStatus.COMPLETED,
            TaskStatus.SUBMITTED,
        }:
            raise ValueError("已完成 Agent 必须有完成或已提交任务状态")
        if self.status is AgentRunStatus.BLOCKED and not self.validation_report.blocking:
            raise ValueError("blocked 状态必须至少包含一个阻断质检项")
        return self

    @classmethod
    def new(
        cls,
        *,
        run_id: str,
        owner_user_id: str | None = None,
        project_id: str,
        workspace_id: str,
        target_profile: str,
        generation_mode: str = "r2v",
        idempotency_key: str | None = None,
        skill: str = "short-drama-production",
        skill_alias: str | None = None,
        input_summary: Mapping[str, Any] | None = None,
    ) -> "AgentRunState":
        key = idempotency_key or cls.derive_idempotency_key(
            project_id, run_id, target_profile, generation_mode
        )
        return cls(
            run_id=run_id,
            owner_user_id=owner_user_id,
            project_id=project_id,
            workspace_id=workspace_id,
            target_profile=target_profile,
            generation_mode=generation_mode,
            idempotency_key=key,
            skill=skill,
            skill_alias=skill_alias,
            input_summary=dict(input_summary or {}),
        )

    @staticmethod
    def derive_idempotency_key(
        project_id: str, run_id: str, profile: str, generation_mode: str
    ) -> str:
        digest = hashlib.sha256(
            "|".join((project_id, run_id, profile, generation_mode)).encode()
        ).hexdigest()[:40]
        return f"agent:{project_id}:{digest}"

    def transition(self, stage: AgentStage, status: StageStatus, *, message: str | None = None) -> None:
        stage = AgentStage(stage)
        status = StageStatus(status)
        allowed_statuses = {
            StageStatus.PENDING: {StageStatus.RUNNING, StageStatus.SKIPPED},
            StageStatus.RUNNING: {StageStatus.COMPLETED, StageStatus.BLOCKED, StageStatus.FAILED},
            StageStatus.COMPLETED: set(),
            StageStatus.SKIPPED: set(),
            StageStatus.BLOCKED: set(),
            StageStatus.FAILED: set(),
        }
        if status is not self.stage_status and status not in allowed_statuses[self.stage_status]:
            raise InvalidStageTransitionError(
                f"阶段 {self.current_stage.value} 不能从 {self.stage_status.value} 转换为 {status.value}"
            )
        expected = list(AgentStage)
        if stage is not self.current_stage:
            current_index = expected.index(self.current_stage)
            target_index = expected.index(stage)
            if target_index != current_index + 1:
                raise InvalidStageTransitionError(
                    f"不能从 {self.current_stage.value} 跳转到 {stage.value}"
                )
            if self.stage_status not in {StageStatus.COMPLETED, StageStatus.SKIPPED}:
                raise InvalidStageTransitionError("当前阶段尚未完成")
            self.current_stage = stage
        self.stage_status = status
        if status is StageStatus.COMPLETED:
            self.status = AgentRunStatus.RUNNING
        elif status is StageStatus.BLOCKED:
            self.status = AgentRunStatus.BLOCKED
        elif status is StageStatus.FAILED:
            self.status = AgentRunStatus.FAILED
        self.stage_events.append(StageEvent(stage=stage, status=status, message=message))
        self.updated_at = datetime.now(UTC)

    def to_safe_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        return redact_sensitive(payload)

    def to_json(self) -> str:
        return json.dumps(self.to_safe_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, value: str | bytes) -> "AgentRunState":
        return cls.migrate(json.loads(value))

    @classmethod
    def migrate(cls, payload: Mapping[str, Any]) -> "AgentRunState":
        data = dict(payload)
        version = int(data.get("schema_version", 1))
        if version > 1:
            raise AgentContractError(f"不支持的 Agent State schema 版本 {version}")
        # v0 used `tasks` and `profile`; accepting it makes checkpoint rollout safe.
        if "target_profile" not in data and data.get("profile"):
            data["target_profile"] = data.pop("profile")
        if "submitted_task_ids" not in data and isinstance(data.get("tasks"), list):
            data["submitted_task_ids"] = data.pop("tasks")
        data.setdefault("schema_version", 1)
        return cls.model_validate(data)


_SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|secret|token|password|signature|signed[_-]?url|credential)",
    re.IGNORECASE,
)


def redact_sensitive(value: Any) -> Any:
    """Bounded recursive redaction for audit/checkpoint output."""
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            output[key_text] = "[REDACTED]" if _SENSITIVE_KEY_RE.search(key_text) else redact_sensitive(item)
        return output
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    return value


def record_audit_event(state: AgentRunState, event: str, **fields: Any) -> None:
    """Append a bounded, redacted operational event to an Agent Run."""
    state.audit_events.append(
        redact_sensitive(
            {
                "event": event,
                "at": datetime.now(UTC).isoformat(),
                **fields,
            }
        )
    )


def make_finding(
    severity: FindingSeverity,
    code: str,
    message: str,
    validator: str,
    *,
    shot_id: str | None = None,
    path: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> ValidationFinding:
    return ValidationFinding(
        severity=severity,
        code=code,
        message=message,
        validator=validator,
        shot_id=shot_id,
        path=path,
        details=dict(details or {}),
    )
