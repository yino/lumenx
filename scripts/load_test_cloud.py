from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.platform.load_profile import InitialScaleTarget, simulate_backpressure


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 LumenX 云端初始容量与背压模型")
    parser.add_argument("--users", type=int, default=10_000)
    parser.add_argument("--daily-tasks", type=int, default=10_000)
    parser.add_argument("--per-user-concurrency", type=int, default=2)
    parser.add_argument("--worker-concurrency", type=int, default=8)
    arguments = parser.parse_args()
    target = InitialScaleTarget(
        registered_users=arguments.users,
        daily_ai_tasks=arguments.daily_tasks,
        max_concurrency_per_user=arguments.per_user_concurrency,
        global_worker_concurrency=arguments.worker_concurrency,
    )
    report = simulate_backpressure(target)
    print(json.dumps({"target": asdict(target), "result": asdict(report)}, ensure_ascii=False, indent=2))
    return 0 if report.completed == report.admitted else 2


if __name__ == "__main__":
    raise SystemExit(main())
