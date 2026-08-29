"""Stable error codes exposed by the Agent service."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentErrorInfo:
    code: str
    message: str
    retryable: bool = False


class AgentError(RuntimeError):
    code = "AGENT_ERROR"
    retryable = False

    def __init__(self, message: str, *, code: str | None = None, retryable: bool | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable

    @property
    def info(self) -> AgentErrorInfo:
        return AgentErrorInfo(self.code, str(self), self.retryable)


class AgentBlockedError(AgentError):
    code = "AGENT_BLOCKED"


class AgentPolicyError(AgentError):
    code = "AGENT_POLICY_VIOLATION"


class AgentRouteError(AgentError):
    code = "AGENT_MODEL_ROUTE_UNAVAILABLE"


class AgentConflictError(AgentError):
    code = "AGENT_STATE_CONFLICT"


class AgentNotFoundError(AgentError):
    code = "AGENT_RUN_NOT_FOUND"
