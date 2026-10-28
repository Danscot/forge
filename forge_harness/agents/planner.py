from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .base import BaseAgent, AgentOutputError
from ..core.state import Task
from ..tools.file_ops import FileOps

ALLOWED_TASK_TYPES = {"foundation", "design_tokens", "shared_component", "screen", "integration"}

TASK_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "app_summary": {"type": "string"},
        "architecture_notes": {"type": "array", "items": {"type": "string"}},
        "tasks": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "type": {"type": "string", "enum": ["foundation", "design_tokens", "shared_component", "screen", "integration"]},
                    "title": {"type": "string", "minLength": 1},
                    "description": {"type": "string"},
                    "screen": {"type": ["string", "null"]},
                    "route": {"type": "string"},
                    "mockup_paths": {"type": "array", "items": {"type": "string"}},
                    "dependencies": {"type": "array", "items": {"type": "string"}},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "test_plan": {
                        "type": "array", "maxItems": 5,
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["assert_visible", "click", "fill", "assert_text", "navigate"]},
                                "selector": {"type": "string"},
                                "value": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["action", "description"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "type", "title", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["app_summary", "architecture_notes", "tasks"],
    "additionalProperties": False,
}


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
        # Scale planning effort to the amount of context. Small specs without mockups
        # get a fast pass; large specs or many mockups get a larger reasoning allowance.
        effort_override: dict[str, Any] | None = None
        context_size = len(target_spec) + len(files) + len(mockup_summary)
        mockup_count = len(input_manifest.get("mockups", []))
        effort_cfg = self.config.get("llm", {}).get("effort_policy", {}) or {}
        thresholds = effort_cfg.get("planner_thresholds", {}) if isinstance(effort_cfg, dict) else {}
        simple_chars = max(1000, int(thresholds.get("simple_context_chars", 12000)))
        complex_chars = max(simple_chars + 1, int(thresholds.get("complex_context_chars", 30000)))
        if context_size <= simple_chars and mockup_count <= 1:
            effort_override = {"thinking_enabled": True, "thinking_budget": 1024, "max_output_tokens": 6500}
        elif context_size >= complex_chars or mockup_count >= 5:
            effort_override = {"thinking_enabled": True, "thinking_budget": 3072, "max_output_tokens": 8192}
        parsed, _result = self.ask_json(
            user_text,
            image_paths=images,
            profile_override=effort_override,
            required_nonempty_array="tasks",
            json_schema=TASK_PLAN_SCHEMA,
            schema_name="forge_application_task_plan",
        )
        raw_tasks = parsed.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise AgentOutputError("Planner response must contain a non-empty tasks array")
        tasks: list[Task] = []
        seen: set[str] = set()
        known: set[str] = set()
        root = file_ops.root
        known_mockups = {str(item.get("path")) for item in input_manifest.get("mockups", [])}

        # A mockup is the acceptance target for a screen, not for its foundation
        # task. Models sometimes attach the same whole-page reference to both the
        # foundation and screen tasks. Move that assignment to the screen task when
        # the screen task otherwise has no explicit mockup, so vision analysis and
        # browser/visual gates run at the correct point in the task queue.
        screen_task_indexes = [
            i for i, item in enumerate(raw_tasks)
            if isinstance(item, dict) and str(item.get("type", "screen")) in {"screen", "integration"}
        ]
        explicitly_screen_assigned = {
            str(path)
            for i in screen_task_indexes
            for path in (raw_tasks[i].get("mockup_paths", []) or [])
            if str(path) in known_mockups
        }
        foundation_assigned = {
            str(path)
            for item in raw_tasks
            if isinstance(item, dict) and str(item.get("type", "screen")) in {"foundation", "design_tokens"}
            for path in (item.get("mockup_paths", []) or [])
            if str(path) in known_mockups
        }
        unassigned_foundation_mockups = sorted(foundation_assigned - explicitly_screen_assigned)
        if screen_task_indexes and unassigned_foundation_mockups:
            # A single screen is unambiguous. For multi-screen plans, add these
            # references to the first screen lacking an explicit mapping instead of
            # letting a foundation task consume them.
            destination = next(
                (i for i in screen_task_indexes if not (raw_tasks[i].get("mockup_paths") or [])),
                screen_task_indexes[0] if len(screen_task_indexes) == 1 else None,
            )
            if destination is not None:
                current = list(raw_tasks[destination].get("mockup_paths", []) or [])
                raw_tasks[destination]["mockup_paths"] = list(dict.fromkeys(current + unassigned_foundation_mockups))

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
            if task_type in {"foundation", "design_tokens"}:
                # Keep page screenshots out of foundation prompts. Otherwise the
                # coder tends to implement the entire screen inside App.tsx during
                # the shell task, and the final-screen checks can run too early.
                mockup_paths = []
            dependencies = [str(dependency) for dependency in raw.get("dependencies", []) if str(dependency) in known]
            # Screen behavior is verified only on screen/integration tasks. A mockup
            # assigned to a shared_component can still drive focused component review,
            # but foundation/design-token tasks never inherit those screen selectors.
            raw_test_plan = [step for step in raw.get("test_plan", []) if isinstance(step, dict)]
            route = str(raw.get("route") or "/")
            test_plan = raw_test_plan if (task_type in {"screen", "integration"} or (task_type == "shared_component" and mockup_paths)) else []
            if task_type in {"screen", "integration"} or (task_type == "shared_component" and mockup_paths):
                test_plan = self._ensure_route_activation(route, test_plan)
            task = Task(
                id=task_id,
                type=task_type,
                title=title,
                description=str(raw.get("description", title)),
                screen=str(raw.get("screen")) if raw.get("screen") is not None else None,
                route=route,
                mockup_paths=mockup_paths,
                dependencies=dependencies,
                acceptance_criteria=[str(v) for v in raw.get("acceptance_criteria", []) if v],
                test_plan=test_plan,
                expected_files=[str(p) for p in raw.get("expected_files", [])],
            )
            tasks.append(task)
            known.add(task_id)
        return parsed, tasks

    @staticmethod
    def _ensure_route_activation(route: str, test_plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Prepend a tab-activation step when a non-default route cannot be reached by navigation alone.

        Single-page Expo tab shells ignore the browser route; a Profile task routed to
        ``/profile`` still renders Home until the Profile tab is clicked. Without an
        activation step, ``assert_visible`` checks for route-specific selectors time out.
        """
        normalized = str(route or "").strip("/")
        if not normalized:
            return test_plan
        segment = normalized.split("/", 1)[0]
        if not segment:
            return test_plan
        has_activation = any(
            isinstance(step, dict) and str(step.get("action", "")).strip() in {"click", "navigate"}
            for step in test_plan
        )
        if has_activation:
            return test_plan
        activation_step = {
            "action": "click",
            "selector": f'[data-testid="tab-{segment}"]',
            "description": f"Activate the {segment} tab for route {route}",
        }
        return [activation_step] + list(test_plan)

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
