from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import BaseAgent, AgentOutputError
from ..core.state import Task


class CriticAgent(BaseAgent):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__("critic", "critic.md", *args, **kwargs)

    def inspect(
        self,
        task: Task,
        target_spec: str,
        vision_spec: dict[str, Any] | None,
        validation: dict[str, Any],
        mockup_paths: list[str | Path],
        screenshot_path: str | Path | None,
    ) -> dict[str, Any]:
        images: list[str | Path] = []
        images.extend([path for path in mockup_paths if Path(path).is_file()])
        if screenshot_path and Path(screenshot_path).is_file():
            images.append(screenshot_path)
        validation_cfg = self.config.get("validation", {}) or {}
        functional_weight = float(validation_cfg.get("functional_weight", 0.6))
        visual_weight = float(validation_cfg.get("visual_weight", 0.4))
        user_text = (
            f"CURRENT VALIDATION SCORING POLICY: functional_weight={functional_weight}; "
            f"visual_weight={visual_weight}; quality_score must be the weighted mean of "
            "functional_score and visual_score using these weights.\n\n"
            f"TASK:\n{json.dumps(task.to_dict(), ensure_ascii=False, indent=2)}\n\n"
            f"APPLICATION SPECIFICATION:\n{target_spec[:12000]}\n\n"
            f"VISION SPECIFICATION:\n{json.dumps(vision_spec or {}, ensure_ascii=False, indent=2)[:10000]}\n\n"
            f"VALIDATION DIAGNOSTICS:\n{json.dumps(validation, ensure_ascii=False, indent=2)[:18000]}\n\n"
            "Images are provided in order: reference mockup(s), then the latest rendered screenshot. "
            "If no screenshot image is present, report that visual validation could not be completed and fail the applicable gate."
        )
        parsed, _result = self.ask_json(user_text, image_paths=images)
        if str(parsed.get("status", "")).upper() not in {"PASS", "FAIL"}:
            raise AgentOutputError("Critic must return status PASS or FAIL")
        for key in ("functional_score", "visual_score", "quality_score"):
            try:
                parsed[key] = max(0.0, min(100.0, float(parsed.get(key, 0))))
            except (TypeError, ValueError):
                parsed[key] = 0.0
        # Preserve the model's stated score for diagnostics, but never let an
        # internally contradictory quality_score disagree with the configured
        # functional/visual sub-scores used by the acceptance gate.
        parsed["reported_quality_score"] = parsed["quality_score"]
        total_weight = max(0.0001, functional_weight + visual_weight)
        parsed["quality_score"] = round(
            (parsed["functional_score"] * functional_weight + parsed["visual_score"] * visual_weight)
            / total_weight,
            2,
        )
        parsed["critical_failure"] = bool(parsed.get("critical_failure", False))
        parsed.setdefault("issues", [])
        return parsed
