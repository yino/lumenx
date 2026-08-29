"""PostgreSQL persistence for Agent Run state, scoped by user/workspace."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.contracts import AgentRunState
from ...platform.contracts import UserContext
from ...platform.database import Database
from ...platform.db_models import AgentRunRecord, ProjectRecord
from ...platform.identifiers import parse_database_id


class PostgresAgentRunRepository:
    def __init__(self, database: Database):
        self.database = database

    @contextmanager
    def _session(self, state: AgentRunState | None = None) -> Iterator[Session]:
        if state is None or not state.owner_user_id:
            raise ValueError("Agent Run 必须包含所属用户")
        with self.database.transaction(UserContext(user_id=state.owner_user_id, session_id="agent-runtime")) as session:
            yield session

    def save(self, state: AgentRunState) -> AgentRunState:
        user_id = parse_database_id(state.owner_user_id, field="用户 ID") if state.owner_user_id else None
        workspace_id = parse_database_id(state.workspace_id, field="工作区 ID")
        project_id = parse_database_id(state.project_id, field="项目 ID")
        assert user_id is not None
        with self._session(state) as session:
            scoped_project = session.scalar(
                select(ProjectRecord.id).where(
                    ProjectRecord.id == project_id,
                    ProjectRecord.user_id == user_id,
                    ProjectRecord.workspace_id == workspace_id,
                    ProjectRecord.deleted_at.is_(None),
                )
            )
            if scoped_project is None:
                raise LookupError("项目不存在或无权访问")
            record = session.scalar(select(AgentRunRecord).where(AgentRunRecord.run_id == state.run_id))
            payload = state.to_safe_dict()
            if record is None:
                record = AgentRunRecord(
                    run_id=state.run_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    idempotency_key=state.idempotency_key,
                    payload=payload,
                )
                session.add(record)
            else:
                record.status = state.status.value
                record.current_stage = state.current_stage.value
                record.stage_status = state.stage_status.value
                record.schema_version = state.schema_version
                record.payload = payload
            session.flush()
        return state

    def get(self, run_id: str, *, workspace_id: str, owner_user_id: str) -> AgentRunState | None:
        state_stub = AgentRunState.new(run_id=run_id, project_id="1", workspace_id=workspace_id, owner_user_id=owner_user_id, target_profile="grok-imagine-video")
        with self._session(state_stub) as session:
            record = session.scalar(select(AgentRunRecord).where(AgentRunRecord.run_id == run_id, AgentRunRecord.user_id == parse_database_id(owner_user_id, field="用户 ID"), AgentRunRecord.workspace_id == parse_database_id(workspace_id, field="工作区 ID")))
            return AgentRunState.migrate(record.payload) if record else None

    def by_idempotency(self, key: str, *, workspace_id: str, owner_user_id: str) -> AgentRunState | None:
        stub = AgentRunState.new(run_id="lookup", project_id="1", workspace_id=workspace_id, owner_user_id=owner_user_id, target_profile="grok-imagine-video")
        with self._session(stub) as session:
            record = session.scalar(select(AgentRunRecord).where(AgentRunRecord.idempotency_key == key, AgentRunRecord.user_id == parse_database_id(owner_user_id, field="用户 ID"), AgentRunRecord.workspace_id == parse_database_id(workspace_id, field="工作区 ID")))
            return AgentRunState.migrate(record.payload) if record else None
