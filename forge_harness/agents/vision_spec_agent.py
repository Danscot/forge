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
        asset_inventory = self._available_asset_inventory(file_ops)
        user_text = (
            f"TASK:\n{json.dumps(task.to_dict(), ensure_ascii=False, indent=2)}\n\n"
            f"APPLICATION SPECIFICATION:\n{target_spec[:12000]}\n\n"
            f"AVAILABLE MOCKUPS:\n{json.dumps(inventory, indent=2)}\n\n"
            f"EXISTING PROJECT ASSETS (only these paths may be reused as local assets):\n{json.dumps(asset_inventory, ensure_ascii=False, indent=2)}\n\n"
            f"EXISTING COMPONENT REGISTRY:\n{json.dumps(registry, ensure_ascii=False, indent=2)[:10000]}\n\n"
            "Inspect the image(s), then create a precise React Native-compatible implementation specification. "
            "Never invent a local image/font/media path. If an asset is absent from EXISTING PROJECT ASSETS, "
            "set its path to null and recommend a native View/Text placeholder or a safe remote URI."
        )
        image_paths = [str(file_ops.resolve(item["path"])) for item in inventory]
        # One reference needs only a compact extraction pass; multiple mockups require
        # more effort to reconcile shared tokens and layout differences.
        if len(image_paths) <= 1:
            vision_effort = {"thinking_enabled": True, "thinking_budget": 512, "max_output_tokens": 3000}
        elif len(image_paths) >= 5:
            vision_effort = {"thinking_enabled": True, "thinking_budget": 1536, "max_output_tokens": 5000}
        else:
            vision_effort = {"thinking_enabled": True, "thinking_budget": 768, "max_output_tokens": 4000}
        parsed, _result = self.ask_json(user_text, image_paths=image_paths, profile_override=vision_effort)
        if not any(key in parsed for key in ("layout", "regions", "design_tokens")):
            raise AgentOutputError("Vision specification is missing layout information")
        return parsed

    @staticmethod
    def _available_asset_inventory(file_ops: FileOps, limit: int = 120) -> list[dict[str, Any]]:
        """List real image/vector assets, excluding mockups and generated harness artifacts."""
        allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif"}
        candidates: list[dict[str, Any]] = []
        for relative_root in ("input/assets", "assets", "public"):
            try:
                directory = file_ops.resolve(relative_root)
            except Exception:
                continue
            if not directory.is_dir():
                continue
            for asset in sorted(directory.rglob("*")):
                if not asset.is_file() or asset.suffix.lower() not in allowed_suffixes:
                    continue
                try:
                    rel = str(asset.relative_to(file_ops.root)).replace("\\", "/")
                    if any(part in {"node_modules", ".git", ".harness"} for part in Path(rel).parts):
                        continue
                    size = asset.stat().st_size
                    if size > 7_000_000:
                        continue
                    candidates.append({"path": rel, "bytes": size, "format": asset.suffix.lower().lstrip(".")})
                    if len(candidates) >= limit:
                        return candidates
                except OSError:
                    continue
        return candidates
