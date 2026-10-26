from __future__ import annotations

import json
from typing import Any

from .base import BaseAgent, AgentOutputError
from ..core.state import Task


class DiagnosticAgent(BaseAgent):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__("diagnostic", "diagnostic.md", *args, **kwargs)

    def diagnose(self, task: Task, evidence: dict[str, Any], approved_plan: dict[str, Any] | None = None) -> dict[str, Any]:
        text = (
            f"TASK:\n{json.dumps(task.to_dict(), ensure_ascii=False, indent=2)}\n\n"
            f"APPROVED CHANGE PLAN:\n{json.dumps(approved_plan or {}, ensure_ascii=False, indent=2)[:10000]}\n\n"
            f"EVIDENCE:\n{json.dumps(evidence, ensure_ascii=False, indent=2)[:22000]}\n\n"
            "Provide a targeted repair diagnosis, separating confirmed evidence from hypotheses."
        )
        parsed, _result = self.ask_json(text)
        if not isinstance(parsed.get("next_steps", []), list):
            raise AgentOutputError("Diagnostic response must include next_steps array")
        return parsed
