#!/usr/bin/env python3
"""Minimal, dependency-light import check for the Agent runtime."""

import sys
from pathlib import Path

# Make the repository root importable when this script is invoked directly
# (``python scripts/check_agent_runtime.py``), not only with PYTHONPATH set.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.core import AgentRunState, LANGGRAPH_AVAILABLE
from src.agents.short_drama import ProfileResolver, PromptCompiler


def main() -> int:
    state = AgentRunState.new(
        run_id="import-check",
        project_id="1",
        workspace_id="1",
        target_profile="grok-imagine-video",
    )
    ProfileResolver().resolve("grok-imagine-video", generation_mode="r2v")
    PromptCompiler()
    print({"agent_state": state.schema_version, "langgraph_available": LANGGRAPH_AVAILABLE})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
