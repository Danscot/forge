from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .base import BaseAgent, AgentOutputError
from ..core.state import Task
from ..tools.file_ops import FileOperationError, FileOps


class CoderAgent(BaseAgent):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__("coder", "coder_plan.md", *args, **kwargs)
        self.patch_prompt_name = "coder_patch.md"

    def _prompt_for(self, name: str) -> str:
        project_harness = self.project_root / str(self.config.get("project", {}).get("harness_dir", ".harness"))
        paths = [project_harness / "prompts" / name, Path(__file__).resolve().parents[1] / "prompts" / name]
        for path in paths:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        raise AgentOutputError(f"Prompt file missing: {name}")

    def plan_changes(
        self,
        task: Task,
        target_spec: str,
        vision_spec: dict[str, Any] | None,
        registry: dict[str, Any],
        diagnostics: dict[str, Any] | None,
        file_ops: FileOps,
    ) -> dict[str, Any]:
        inventory = self._relevant_inventory(task, file_ops)
        user_text = (
            f"TASK:\n{json.dumps(task.to_dict(), ensure_ascii=False, indent=2)}\n\n"
            f"APPLICATION SPECIFICATION:\n{target_spec[:12000]}\n\n"
            f"VISION SPECIFICATION:\n{json.dumps(vision_spec or {}, ensure_ascii=False, indent=2)[:12000]}\n\n"
            f"COMPONENT REGISTRY:\n{json.dumps(registry, ensure_ascii=False, indent=2)[:12000]}\n\n"
            f"RELEVANT FILE INVENTORY / SOURCE:\n{inventory}\n\n"
            f"PRIOR DIAGNOSTICS:\n{json.dumps(diagnostics or {}, ensure_ascii=False, indent=2)[:9000]}\n\n"
            "Plan the smallest sufficient set of changes. Do not author implementation code in this response."
        )
        parsed, _result = self.ask_json(user_text)
        self._validate_plan(parsed, file_ops)
        return parsed

    def generate_patch(
        self,
        task: Task,
        approved_plan: dict[str, Any],
        vision_spec: dict[str, Any] | None,
        registry: dict[str, Any],
        diagnostics: dict[str, Any] | None,
        file_ops: FileOps,
    ) -> dict[str, Any]:
        source_map = self._sources_for_plan(approved_plan, file_ops)
        user_text = (
            f"TASK:\n{json.dumps(task.to_dict(), ensure_ascii=False, indent=2)}\n\n"
            f"APPROVED CHANGE PLAN (follow exactly):\n{json.dumps(approved_plan, ensure_ascii=False, indent=2)[:16000]}\n\n"
            f"CURRENT SOURCE EXCERPTS FOR TARGET FILES:\n{source_map}\n\n"
            f"VISION SPECIFICATION:\n{json.dumps(vision_spec or {}, ensure_ascii=False, indent=2)[:10000]}\n\n"
            f"COMPONENT REGISTRY:\n{json.dumps(registry, ensure_ascii=False, indent=2)[:8000]}\n\n"
            f"LATEST DIAGNOSTICS:\n{json.dumps(diagnostics or {}, ensure_ascii=False, indent=2)[:9000]}\n\n"
            "Generate the code patch operations now. You must stay within the approved paths and operation types."
        )
        messages = [{"role": "system", "content": self._prompt_for(self.patch_prompt_name)}, {"role": "user", "content": user_text}]
        result = self.runtime.complete("coder", messages)
        try:
            from .base import extract_json, PatchParseError
            parsed = extract_json(result.content)
        except AgentOutputError as exc:
            raise PatchParseError(f"Coder did not return a patch JSON object: {exc}", raw_content=result.content) from exc
        operations = parsed.get("operations")
        if not isinstance(operations, list) or not operations:
            raise AgentOutputError("Coder patch response has no operations")
        self._validate_patch_against_plan(operations, approved_plan, file_ops)
        return parsed

    def apply_patch(self, patch: dict[str, Any], file_ops: FileOps) -> list[dict[str, Any]]:
        return file_ops.apply_operations(patch.get("operations", []))

    def _validate_plan(self, plan: dict[str, Any], file_ops: FileOps) -> None:
        operations = plan.get("operations")
        if not isinstance(operations, list) or not operations:
            raise AgentOutputError("Change plan must contain a non-empty operations array")
        if str(plan.get("risk", "low")).lower() not in {"low", "medium", "high"}:
            raise AgentOutputError("Change plan risk must be low, medium, or high")
        for operation in operations:
            operation_type = operation.get("type")
            if operation_type not in {"create_file", "replace_text"}:
                raise AgentOutputError(f"Unsupported planned operation: {operation_type}")
            path = operation.get("path")
            if not path:
                raise AgentOutputError("Every planned operation requires a path")
            try:
                target = file_ops.resolve(path, for_write=True)
            except FileOperationError as exc:
                raise AgentOutputError(str(exc)) from exc
            if operation_type == "create_file" and target.exists():
                raise AgentOutputError(f"Plan attempts to create an existing file: {path}")
            if operation_type == "replace_text":
                if not target.is_file():
                    raise AgentOutputError(f"replace_text target does not exist: {path}")
                anchor = operation.get("old_text")
                if not isinstance(anchor, str) or not anchor:
                    raise AgentOutputError(f"replace_text needs an exact old_text anchor: {path}")
                content = target.read_text(encoding="utf-8")
                count = content.count(anchor)
                if count != 1:
                    raise AgentOutputError(f"Planned anchor for {path} matched {count} times; require exactly one")

    def _validate_patch_against_plan(self, operations: list[dict[str, Any]], plan: dict[str, Any], file_ops: FileOps) -> None:
        # Match operations one-to-one. A simple (type, path) dictionary is unsafe here:
        # multiple distinct anchor replacements in one file would overwrite each other.
        planned = list(plan.get("operations", []))
        used: set[int] = set()
        for operation in operations:
            op_type = str(operation.get("type"))
            op_path = str(operation.get("path"))
            op_old_text = operation.get("old_text")
            candidates = [
                (index, item) for index, item in enumerate(planned)
                if index not in used
                and str(item.get("type")) == op_type
                and str(item.get("path")) == op_path
                and (op_type != "replace_text" or self._anchors_match(op_old_text, item.get("old_text")))
            ]
            if not candidates:
                raise AgentOutputError(
                    f"Patch operation ({op_type}, {op_path}) was not present as an unused operation in the approved plan"
                )
            plan_index, approved = candidates[0]
            used.add(plan_index)
            if op_type == "replace_text":
                if not isinstance(operation.get("new_text"), str):
                    raise AgentOutputError(f"replace_text requires string new_text: {op_path}")
                target = file_ops.resolve(op_path, for_write=True)
                # Use the plan's anchor for the uniqueness check: _validate_plan already
                # confirmed it exists exactly once in the current file. The patch's anchor
                # may differ by whitespace but matches after normalization (_anchors_match).
                plan_old_text = approved.get("old_text")
                if not target.is_file() or target.read_text(encoding="utf-8").count(plan_old_text) != 1:
                    raise AgentOutputError(f"Patch anchor is no longer unique in {op_path}")
            elif op_type == "create_file":
                if not isinstance(operation.get("content"), str) or not operation["content"].strip():
                    raise AgentOutputError(f"New file has empty content: {op_path}")
        if len(used) != len(planned):
            missing = [
                {"type": item.get("type"), "path": item.get("path")}
                for index, item in enumerate(planned) if index not in used
            ]
            raise AgentOutputError(f"Coder patch omitted approved operation(s): {missing}")

    @staticmethod
    def _anchors_match(patch_anchor: Any, plan_anchor: Any) -> bool:
        """Compare two replace_text anchors, tolerating LLM whitespace drift between plan and patch calls.

        LLMs often emit different trailing whitespace or line endings across separate
        plan_changes and generate_patch calls. Normalizing per-line trailing whitespace
        and line endings keeps the safety guarantee (anchor must still be unique in-file,
        checked separately) while avoiding false rejections of otherwise-valid repairs.
        """
        if not isinstance(patch_anchor, str) or not isinstance(plan_anchor, str):
            return patch_anchor == plan_anchor

        def normalize(text: str) -> str:
            return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))

        return normalize(patch_anchor) == normalize(plan_anchor)

    def _relevant_inventory(self, task: Task, file_ops: FileOps) -> str:
        root = file_ops.root
        source_files: list[Path] = []
        for base_name in ("App.tsx", "app", "src", "components", "screens"):
            base = root / base_name
            if base.is_file():
                source_files.append(base)
            elif base.is_dir():
                source_files.extend(p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in {".tsx", ".ts", ".jsx", ".js", ".json"} and "node_modules" not in p.parts and ".git" not in p.parts)
            if len(source_files) > 100:
                break
        parts = []
        for path in dict.fromkeys(source_files):
            try:
                relative = str(path.relative_to(root))
                if path.stat().st_size <= 120_000:
                    parts.append(f"--- {relative} ---\n{file_ops.cat_file(relative, 1, 140)[:2500]}")
                else:
                    parts.append(f"--- {relative} (large file; omitted) ---")
            except Exception:
                continue
        return "\n".join(parts)[:22000] or "No existing source files found. New app files may be created."

    def _sources_for_plan(self, plan: dict[str, Any], file_ops: FileOps) -> str:
        excerpts = []
        for operation in plan.get("operations", []):
            if operation.get("type") != "replace_text":
                continue
            relative = str(operation.get("path"))
            try:
                excerpts.append(f"--- {relative} ---\n{file_ops.cat_file(relative, 1, 280)[:10000]}")
            except Exception as exc:
                excerpts.append(f"--- {relative}: unable to read ---\n{exc}")
        return "\n".join(excerpts)[:26000] or "No existing-file patch targets; only new files are planned."
