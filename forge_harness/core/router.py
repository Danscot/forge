from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .state import Task


@dataclass
class RouteDecision:
    action: str
    next_index: int
    reason: str


class RefinementRouter:
    """Deterministic task-level state transitions; LLMs never control execution state directly."""

    def after_validation(self, task: Task, current_index: int, report: dict[str, Any], max_retries: int) -> RouteDecision:
        if report.get("accepted") is True:
            task.status = "passed"
            task.last_report = report
            return RouteDecision("advance", current_index + 1, "All required acceptance gates passed")
        task.retry_count += 1
        task.last_report = report
        if task.retry_count <= max_retries:
            task.status = "running"
            return RouteDecision("repair", current_index, f"Validation failed; retry {task.retry_count}/{max_retries}")
        task.status = "failed"
        return RouteDecision("block", current_index, "Task retry budget exhausted")

    def dependencies_satisfied(self, task: Task, tasks: list[Task]) -> bool:
        statuses = {item.id: item.status for item in tasks}
        return all(statuses.get(dependency) == "passed" for dependency in task.dependencies)
