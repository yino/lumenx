"""Small orchestration boundary around LangGraph.

LangGraph is optional at import time so local/desktop tooling can still load
the domain contracts. Production installs pin the dependency in requirements.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .contracts import AgentRunState, AgentStage, StageEvent, StageStatus

logger = logging.getLogger(__name__)

try:  # pragma: no cover - availability depends on deployment image
    from langgraph.graph import END, START, StateGraph

    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by lightweight installs
    END = "__end__"
    START = "__start__"
    StateGraph = None  # type: ignore[assignment,misc]
    LANGGRAPH_AVAILABLE = False


class CheckpointStore(Protocol):
    def save(self, state: AgentRunState) -> None: ...

    def load(self, run_id: str) -> AgentRunState | None: ...


@dataclass
class InMemoryCheckpointStore:
    _states: dict[str, AgentRunState] = field(default_factory=dict)

    def save(self, state: AgentRunState) -> None:
        self._states[state.run_id] = AgentRunState.model_validate(
            copy.deepcopy(state.model_dump(mode="json"))
        )

    def load(self, run_id: str) -> AgentRunState | None:
        state = self._states.get(run_id)
        return (
            AgentRunState.model_validate(copy.deepcopy(state.model_dump(mode="json")))
            if state
            else None
        )


class PostgresCheckpointStore:
    """Adapter for LangGraph's Postgres checkpointer.

    The actual checkpointer is injected by the worker; this class keeps Agent
    state snapshots and audit data behind a stable interface. It intentionally
    does not open a connection itself or receive credentials.
    """

    def __init__(self, saver: Any):
        self.saver = saver
        self._fallback = InMemoryCheckpointStore()

    def save(self, state: AgentRunState) -> None:
        self._fallback.save(state)
        put = getattr(self.saver, "put", None)
        if callable(put):
            try:
                config = {"configurable": {"thread_id": state.run_id}}
                checkpoint = {"channel_values": {"state": state.to_safe_dict()}, "v": 1, "id": state.run_id}
                metadata = {"run_id": state.run_id, "stage": state.current_stage.value}
                result = put(config, checkpoint, metadata, {})
                # Async savers are owned by the worker event loop; do not
                # accidentally block or schedule an unawaited coroutine here.
                if hasattr(result, "__await__"):
                    logger.debug("async checkpoint saver requires worker await")
            except Exception:
                logger.exception("Agent PostgreSQL checkpoint write failed")

    def load(self, run_id: str) -> AgentRunState | None:
        get_tuple = getattr(self.saver, "get_tuple", None)
        if callable(get_tuple):
            try:
                item = get_tuple({"configurable": {"thread_id": run_id}})
                checkpoint = getattr(item, "checkpoint", None) if item is not None else None
                payload = checkpoint.get("channel_values", {}).get("state") if isinstance(checkpoint, Mapping) else None
                if payload:
                    return AgentRunState.migrate(payload)
            except Exception:
                logger.exception("Agent PostgreSQL checkpoint read failed")
        return self._fallback.load(run_id)


Node = Callable[[AgentRunState], AgentRunState]


@dataclass(frozen=True)
class AgentGraph:
    """Compiled graph facade that works with or without LangGraph installed."""

    nodes: Mapping[AgentStage, Node]
    order: tuple[AgentStage, ...]

    def invoke(
        self,
        state: AgentRunState,
        *,
        checkpoint: CheckpointStore | None = None,
        until: AgentStage | None = None,
    ) -> AgentRunState:
        current = state
        store = checkpoint or InMemoryCheckpointStore()
        start_index = self.order.index(current.current_stage)
        for stage in self.order[start_index:]:
            node = self.nodes[stage]
            if stage is not current.current_stage:
                if current.stage_status not in {StageStatus.COMPLETED, StageStatus.SKIPPED}:
                    raise ValueError(f"阶段 {current.current_stage.value} 尚未完成")
                current.current_stage = stage
                current.stage_status = StageStatus.PENDING
            if current.stage_status is not StageStatus.PENDING and stage is current.current_stage:
                # A completed checkpoint is never replayed, which keeps submit
                # side effects idempotent after a worker restart.
                if current.stage_status in {StageStatus.COMPLETED, StageStatus.SKIPPED}:
                    continue
            current.stage_status = StageStatus.RUNNING
            current.stage_events.append(StageEvent(stage=stage, status=StageStatus.RUNNING))
            try:
                current = node(current)
                if getattr(current.status, "value", current.status) == "failed":
                    current.status = "running"
                if current.current_stage is stage and current.stage_status is StageStatus.RUNNING:
                    current.stage_status = StageStatus.COMPLETED
                    current.stage_events.append(StageEvent(stage=stage, status=StageStatus.COMPLETED))
                store.save(current)
            except Exception as exc:
                current.stage_status = StageStatus.FAILED
                current.status = "failed"
                current.stage_events.append(
                    StageEvent(stage=stage, status=StageStatus.FAILED, message=str(exc)[:2000])
                )
                store.save(current)
                raise
            if getattr(current.status, "value", current.status) in {"blocked", "needs_approval", "cancelled", "failed"}:
                break
            if until is stage:
                break
        return current


def compile_langgraph(
    nodes: Mapping[AgentStage, Node], order: tuple[AgentStage, ...]
) -> Any:
    """Compile an actual StateGraph when available, otherwise return facade."""
    if not LANGGRAPH_AVAILABLE:
        return AgentGraph(nodes, order)
    graph = StateGraph(dict)
    for stage, node in nodes.items():
        graph.add_node(stage.value, lambda state, _node=node: _node(AgentRunState.model_validate(state)).model_dump(mode="json"))
    graph.add_edge(START, order[0].value)
    for previous, current in zip(order, order[1:]):
        graph.add_edge(previous.value, current.value)
    graph.add_edge(order[-1].value, END)
    return graph.compile()


def langgraph_import_status() -> dict[str, Any]:
    return {"available": LANGGRAPH_AVAILABLE, "checkpoint": "postgres" if LANGGRAPH_AVAILABLE else "memory"}
