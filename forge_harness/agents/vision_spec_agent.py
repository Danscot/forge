from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import BaseAgent, AgentOutputError
from ..core.state import Task
from ..tools.file_ops import FileOps


class VisionSpecAgent(BaseAgent):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__("vision", "vision.md", *args, **kwargs)

    def specify(self, task: Task, target_spec: str, file_ops: FileOps, registry: dict[str, Any]) -> dict[str, Any]:
        inventory = []
        for raw_path in task.mockup_paths:
            try:
                path = file_ops.resolve(raw_path)
                if path.is_file():
                    inventory.append({"path": raw_path, "bytes": path.stat().st_size})
            except Exception:
                continue
        user_text = (
            f"TASK:\n{json.dumps(task.to_dict(), ensure_ascii=False, indent=2)}\n\n"
            f"APPLICATION SPECIFICATION:\n{target_spec[:12000]}\n\n"
            f"AVAILABLE MOCKUPS:\n{json.dumps(inventory, indent=2)}\n\n"
            f"EXISTING COMPONENT REGISTRY:\n{json.dumps(registry, ensure_ascii=False, indent=2)[:10000]}\n\n"
            "Inspect the image(s), then create a precise React Native-compatible implementation specification."
        )
        parsed, _result = self.ask_json(user_text, image_paths=[str(file_ops.resolve(item["path"])) for item in inventory])
        if not any(key in parsed for key in ("layout", "regions", "design_tokens")):
            raise AgentOutputError("Vision specification is missing layout information")
        return parsed
