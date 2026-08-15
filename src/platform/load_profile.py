from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InitialScaleTarget:
    registered_users: int = 10_000
    daily_ai_tasks: int = 10_000
    max_concurrency_per_user: int = 2
    global_worker_concurrency: int = 8

    def __post_init__(self) -> None:
        values = (
            self.registered_users,
            self.daily_ai_tasks,
            self.max_concurrency_per_user,
            self.global_worker_concurrency,
        )
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in values):
            raise ValueError("负载目标必须使用正整数")


@dataclass(frozen=True, slots=True)
class BackpressureSimulation:
    submitted: int
    admitted: int
    per_user_rejected: int
    peak_running: int
    peak_queued: int
    completed: int


def deterministic_task_users(target: InitialScaleTarget) -> tuple[int, ...]:
    """Distribute daily tasks deterministically without allocating user content."""
    return tuple(index % target.registered_users for index in range(target.daily_ai_tasks))


def simulate_backpressure(
    target: InitialScaleTarget,
    *,
    submissions: tuple[int, ...] | None = None,
) -> BackpressureSimulation:
    task_users = submissions if submissions is not None else deterministic_task_users(target)
    active_by_user: Counter[int] = Counter()
    queue: deque[int] = deque()
    running: deque[int] = deque()
    rejected = 0
    peak_running = 0
    peak_queued = 0
    admitted = 0

    for user_id in task_users:
        if active_by_user[user_id] >= target.max_concurrency_per_user:
            rejected += 1
            continue
        active_by_user[user_id] += 1
        queue.append(user_id)
        admitted += 1
        while queue and len(running) < target.global_worker_concurrency:
            running.append(queue.popleft())
        peak_running = max(peak_running, len(running))
        peak_queued = max(peak_queued, len(queue))

    completed = 0
    while running or queue:
        if running:
            user_id = running.popleft()
            active_by_user[user_id] -= 1
            completed += 1
        while queue and len(running) < target.global_worker_concurrency:
            running.append(queue.popleft())
        peak_running = max(peak_running, len(running))
        peak_queued = max(peak_queued, len(queue))

    if any(active_by_user.values()):
        raise RuntimeError("背压模拟结束后仍有未释放的用户并发槽")
    return BackpressureSimulation(
        submitted=len(task_users),
        admitted=admitted,
        per_user_rejected=rejected,
        peak_running=peak_running,
        peak_queued=peak_queued,
        completed=completed,
    )
