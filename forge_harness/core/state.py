from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

TaskStatus = Literal["pending", "running", "passed", "failed", "blocked", "skipped"]


@dataclass
class Task:
    id: str
    type: str
    title: str
    description: str
    screen: str | None = None
    route: str = "/"
    mockup_paths: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    test_plan: list[dict[str, Any]] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    status: TaskStatus = "pending"
    retry_count: int = 0
    last_report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> "Task":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in obj.items() if key in allowed})


@dataclass
class CriticReport:
    status: str
    functional_score: float = 0
    visual_score: float = 0
    quality_score: float = 0
    critical_failure: bool = False
    issues: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunState:
    run_id: str
    project_root: str
    target_spec: str
    mode: str
    execution_queue: list[Task] = field(default_factory=list)
    current_index: int = 0
    last_screenshot_path: str | None = None
    critic_report: dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    metadata: dict[str, Any] = field(default_factory=dict)
