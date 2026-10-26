from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .base import BaseAgent, AgentOutputError
from ..core.state import Task
from ..tools.file_ops import FileOps

ALLOWED_TASK_TYPES = {"foundation", "design_tokens", "shared_component", "screen", "integration"}


class PlannerAgent(BaseAgent):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__("planner", "planner.md", *args, **kwargs)

    def plan(self, target_spec: str, input_manifest: dict[str, Any], file_ops: FileOps) -> tuple[dict[str, Any], list[Task]]:
        files = self._project_inventory(file_ops)
        mockup_summary = json.dumps(input_manifest.get("mockups", []), ensure_ascii=False, indent=2)
        user_text = (
            "APPLICATION SPECIFICATION:\n" + target_spec[:20000] +
            "\n\nINPUT MANIFEST (use only paths listed here):\n" + json.dumps(input_manifest, ensure_ascii=False, indent=2)[:18000] +
            "\n\nPROJECT INVENTORY:\n" + files +
            "\n\nMOCKUP MANIFEST:\n" + mockup_summary +
            "\n\nPlan the implementation. Existing screens/components should be preserved and reused where sensible."
        )
        images = [str(file_ops.resolve(item["path"])) for item in input_manifest.get("mockups", []) if item.get("path")]
        parsed, _result = self.ask_json(user_text, image_paths=images)
        raw_tasks = parsed.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise AgentOutputError("Planner response must contain a non-empty tasks array")
        tasks: list[Task] = []
        seen: set[str] = set()
        known: set[str] = set()
        root = file_ops.root
        known_mockups = {str(item.get("path")) for item in input_manifest.get("mockups", [])}
        for index, raw in enumerate(raw_tasks):
            if not isinstance(raw, dict):
                raise AgentOutputError(f"Planner task {index + 1} is not an object")
            title = str(raw.get("title") or f"Task {index + 1}").strip()
            task_id = re.sub(r"[^a-z0-9_]+", "_", str(raw.get("id") or title.lower())).strip("_") or f"task_{index + 1}"
            if task_id in seen:
                task_id = f"{task_id}_{index + 1}"
            seen.add(task_id)
            task_type = str(raw.get("type", "screen"))
            if task_type not in ALLOWED_TASK_TYPES:
                task_type = "screen"
            mockup_paths = [str(path) for path in raw.get("mockup_paths", []) if str(path) in known_mockups]
            if not mockup_paths and raw.get("screen"):
                needle = str(raw["screen"]).lower().replace(" ", "_")
                mockup_paths = [str(item["path"]) for item in input_manifest.get("mockups", []) if needle in Path(str(item.get("path", ""))).stem.lower()]
            dependencies = [str(dependency) for dependency in raw.get("dependencies", []) if str(dependency) in known]
            task = Task(
                id=task_id,
                type=task_type,
                title=title,
                description=str(raw.get("description", title)),
                screen=str(raw.get("screen")) if raw.get("screen") is not None else None,
                route=str(raw.get("route") or "/"),
                mockup_paths=mockup_paths,
                dependencies=dependencies,
                acceptance_criteria=[str(v) for v in raw.get("acceptance_criteria", []) if v],
                test_plan=[step for step in raw.get("test_plan", []) if isinstance(step, dict)],
                expected_files=[str(p) for p in raw.get("expected_files", [])],
            )
            tasks.append(task)
            known.add(task_id)
        return parsed, tasks

    @staticmethod
    def _project_inventory(file_ops: FileOps) -> str:
        paths = []
        for relative in ("package.json", "App.tsx", "app.json", "tsconfig.json", "src", "app", "components", "screens", "src/components", "src/screens", "src/navigation", "src/theme"):
            path = file_ops.root / relative
            if path.exists():
                if path.is_file():
                    paths.append(relative)
                else:
                    for child in sorted(path.rglob("*")):
                        if child.is_file() and child.suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".json"} and "node_modules" not in child.parts and ".git" not in child.parts:
                            paths.append(str(child.relative_to(file_ops.root)))
                            if len(paths) >= 180:
                                break
        paths = list(dict.fromkeys(paths))[:180]
        inventory = []
        for relative in paths:
            try:
                path = file_ops.resolve(relative)
                if path.is_file() and path.stat().st_size <= 150_000:
                    inventory.append(f"--- {relative} ---\n{file_ops.cat_file(relative, 1, 80)[:2500]}")
                else:
                    inventory.append(f"{relative}/")
            except Exception:
                continue
        return "\n".join(inventory)[:20000] or "No recognizable project source files were found."
