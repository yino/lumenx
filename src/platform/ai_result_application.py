from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .ai_task_state import AITaskStateSnapshot
from .contracts import WorkspaceContext
from .database import Database
from .storyboard_service import CloudStoryboardService


class AIResultApplicationService:
    """Apply completed durable AI results to their owning project documents."""

    def __init__(self, database: Database) -> None:
        self.storyboards = CloudStoryboardService(database)

    def apply(
        self,
        context: WorkspaceContext,
        task: AITaskStateSnapshot,
        media_ids: Sequence[str],
        content: str | Mapping[str, Any] | None,
    ) -> str | dict[str, Any] | None:
        request_content = task.request_payload.get("content")
        if not isinstance(request_content, Mapping):
            return dict(content) if isinstance(content, Mapping) else content
        operation = str(request_content.get("operation") or "")
        if operation != "audio.dialogue.batch":
            return dict(content) if isinstance(content, Mapping) else content
        if task.project_id is None:
            raise ValueError("对白任务缺少项目标识")
        stats = self.storyboards.apply_dialogue_audio_results(
            context,
            task.project_id,
            request_content,
            list(media_ids),
        )
        return {"operation": operation, "_batch_stats": stats}
