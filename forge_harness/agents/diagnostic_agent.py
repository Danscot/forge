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
        llm_cfg = self.config.get("llm", {}) or {}
        effort_cfg = llm_cfg.get("effort_policy", {}) or {}
        escalation = effort_cfg.get("diagnostic_escalation", {}) if isinstance(effort_cfg, dict) else {}
        threshold = max(1, int(escalation.get("after_task_retries", 2)))
        profile_override = None
        if task.retry_count >= threshold:
            # Only spend extra reasoning after the first targeted repair did not work.
            profile_override = {
                "thinking_enabled": True,
                "thinking_budget": max(1024, int(escalation.get("thinking_budget", 2048))),
                "max_output_tokens": max(4000, int(escalation.get("max_output_tokens", 5000))),
            }
        parsed, _result = self.ask_json(text, profile_override=profile_override)
        if not isinstance(parsed.get("next_steps", []), list):
            raise AgentOutputError("Diagnostic response must include next_steps array")
        return parsed
