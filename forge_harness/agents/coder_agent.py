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
        # This is a surgical operation plan under a task that has already been
        # decomposed by PlannerAgent. Keep it direct and bounded; implementation code
        # is generated in the separate patch stage.
        plan_effort = (self.config.get("llm", {}).get("effort_policy", {}) or {}).get("coder_plan", {})
        profile_override = {
            "thinking_enabled": bool(plan_effort.get("thinking_enabled", False)),
            "max_output_tokens": max(1200, int(plan_effort.get("max_output_tokens", 3500))),
        }
        if plan_effort.get("thinking_budget") is not None:
            profile_override["thinking_budget"] = max(0, int(plan_effort["thinking_budget"]))
        parsed, result = self.ask_json(
            user_text,
            profile_override=profile_override,
            required_nonempty_array="operations",
        )
        # JSON can parse successfully while still violating the plan contract (for
        # example, an empty operations array). Repair that schema failure locally
        # instead of burning a complete task retry and repeating the same planning
        # work. The retry remains bounded and still passes through the same validator.
        max_plan_repairs = max(0, int(self.config.get("llm", {}).get("structured_repair_attempts", 1)))
        for repair_index in range(max_plan_repairs + 1):
            try:
                self._validate_plan(parsed, file_ops)
                return parsed
            except AgentOutputError as exc:
                if repair_index >= max_plan_repairs:
                    raise
                self.runtime._emit(
                    "structured_output_repair",
                    agent="coder",
                    stage="planning_changes",
                    attempt=repair_index + 1,
                    error=str(exc),
                )
                repair_messages = [
                    {"role": "system", "content": self._prompt_for(self.prompt_name)},
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": result.content},
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was valid JSON but failed the plan contract: "
                            f"{exc}. Return a corrected JSON object with a non-empty operations array "
                            "that addresses the task and any confirmed prior diagnostics. Every operation "
                            "must make a meaningful content change; do not invent a touch operation, a "
                            "same-text replacement, or another no-op just to satisfy the schema. If the "
                            "task needs more work, identify the smallest real change that satisfies its "
                            "acceptance criteria. Preserve the required schema, use only project-relative "
                            "paths, and do not include code."
                        ),
                    },
                ]
                result = self.runtime.complete("coder", repair_messages, profile_override=profile_override)
                parsed = self._extract_contract_candidates(result.content, "operations")[0]
        return parsed  # pragma: no cover - the bounded loop returns or raises

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
        base_messages = [
            {"role": "system", "content": self._prompt_for(self.patch_prompt_name)},
            {"role": "user", "content": user_text},
        ]
        messages = list(base_messages)
        llm_cfg = self.config.get("llm", {})
        patch_cfg = llm_cfg.get("patch_generation", {}) or {}
        max_repairs = max(0, int(patch_cfg.get("structured_repair_attempts", llm_cfg.get("structured_repair_attempts", 1))))
        # Patch output is tightly constrained by the approved plan. Reasoning tokens
        # on this stage add latency and can bury the JSON payload, so use the provider's
        # configured thinking switch off by default. Set llm.patch_generation.thinking_enabled
        # to true to opt back in for models that benefit from it.
        profile_override: dict[str, Any] = {
            "thinking_enabled": bool(patch_cfg.get("thinking_enabled", False)),
        }
        if patch_cfg.get("thinking_budget") is not None:
            profile_override["thinking_budget"] = max(0, int(patch_cfg["thinking_budget"]))
        if patch_cfg.get("max_output_tokens") is not None:
            # An explicit patch cap is a user-requested hard limit.
            profile_override["max_output_tokens"] = max(1024, int(patch_cfg["max_output_tokens"]))
        else:
            # Estimate output allowance from approved operations rather than giving a
            # one-file patch the same budget as a foundation task with many components.
            # This scales output capacity without raising reasoning effort.
            configured_cap = (
                (llm_cfg.get("effort_policy", {}) or {}).get("agents", {}).get("coder", {}).get("max_output_tokens")
                or llm_cfg.get("agents", {}).get("coder", {}).get("max_output_tokens")
                or 12000
            )
            hard_cap = max(2500, int(configured_cap))
            planned_operations = approved_plan.get("operations", []) or []
            create_count = sum(1 for op in planned_operations if op.get("type") == "create_file")
            replace_count = sum(1 for op in planned_operations if op.get("type") == "replace_text")
            estimated_budget = 1000 + (1850 * create_count) + (1250 * replace_count)
            profile_override["max_output_tokens"] = min(hard_cap, max(2500, estimated_budget))
        response_attempts: list[str] = []
        last_error: AgentOutputError | None = None

        for repair_index in range(max_repairs + 1):
            result = self.runtime.complete("coder", messages, profile_override=profile_override)
            response_attempts.append(result.content)
            try:
                from .base import PatchParseError, extract_json_candidates
                parsed_candidates = self._extract_contract_candidates(result.content, "operations")
                if not parsed_candidates:
                    json_candidates = extract_json_candidates(result.content)
                    first_keys = sorted(json_candidates[0].keys()) if json_candidates else []
                    raise AgentOutputError(
                        "Coder patch response did not contain a non-empty operations array "
                        f"(JSON candidates={len(json_candidates)}, first candidate keys={first_keys}, "
                        f"response_chars={len(result.content)})"
                    )

                candidate_errors: list[AgentOutputError] = []
                for parsed in parsed_candidates:
                    operations = parsed.get("operations")
                    if not isinstance(operations, list) or not operations:
                        continue
                    try:
                        self._validate_patch_against_plan(operations, approved_plan, file_ops)
                    except (AgentOutputError, FileOperationError) as candidate_exc:
                        candidate_errors.append(
                            candidate_exc if isinstance(candidate_exc, AgentOutputError)
                            else AgentOutputError(str(candidate_exc))
                        )
                        continue
                    # Select a candidate only after it both has operations and passes
                    # the exact approved-plan/path/anchor contract.
                    return parsed

                if candidate_errors:
                    # A later JSON candidate may be a valid implementation while an
                    # earlier one is a stub or embedded example. Keep checking all
                    # candidates; if none match the plan, repair using the last precise
                    # validation error rather than incorrectly reporting an empty array.
                    raise candidate_errors[-1]
                raise AgentOutputError("Coder patch response has no non-empty operations array")
            except (AgentOutputError, FileOperationError) as exc:
                # Schema/plan/path validation failures are still patch-authoring failures;
                # attempt a constrained correction before restarting the entire task.
                last_error = exc if isinstance(exc, AgentOutputError) else AgentOutputError(str(exc))
                if repair_index >= max_repairs:
                    raise PatchParseError(
                        f"Coder patch response remained invalid after {repair_index + 1} response(s): {exc}",
                        raw_content=result.content,
                        raw_attempts=response_attempts,
                    ) from exc

                # Repair the patch response in-place rather than burning a complete
                # task retry (which repeats planning, diagnosis, and full code generation).
                # Keep the original approved plan and give the model the deterministic
                # validation error so it can return a corrected JSON patch only.
                self.runtime._emit(
                    "structured_output_repair",
                    agent="coder",
                    attempt=repair_index + 1,
                    error=str(exc),
                )
                messages = base_messages + [
                    {"role": "assistant", "content": result.content},
                    {
                        "role": "user",
                        "content": (
                            "Your previous patch response was rejected. Correct it and return one valid JSON object only. "
                            "Do not change the approved plan, add unplanned paths, or omit any approved operation. "
                            "Preserve the required patch schema. Validation error: " + str(exc)
                        ),
                    },
                ]

        # The loop always returns or raises; this is a defensive invariant.
        raise PatchParseError(
            f"Coder patch generation failed: {last_error or 'unknown error'}",
            raw_content=response_attempts[-1] if response_attempts else "",
            raw_attempts=response_attempts,
        )

    @staticmethod
    def _extract_contract_candidates(text: str, required_array: str) -> list[dict[str, Any]]:
        """Return schema-shaped JSON candidates, prioritizing non-empty arrays.

        Do not assume the first decodable object is the final answer. Keep all
        candidates with a non-empty operations array so the caller can test each
        one against the approved operation plan before selecting it.
        """
        from .base import extract_json_candidates

        candidates = extract_json_candidates(text)
        if not candidates:
            from .base import extract_json
            return [extract_json(text)]

        normalized: list[dict[str, Any]] = []
        for candidate in candidates:
            item = dict(candidate)
            value = item.get(required_array)
            if not isinstance(value, list) or not value:
                for wrapper_key in ("patch", "plan", "result", "data"):
                    wrapped = item.get(wrapper_key)
                    if not isinstance(wrapped, dict):
                        continue
                    wrapped_value = wrapped.get(required_array)
                    if isinstance(wrapped_value, list) and wrapped_value:
                        item[required_array] = wrapped_value
                        for metadata_key in ("summary", "registry_changes", "files_to_read", "validation_plan", "risk"):
                            if metadata_key not in item and metadata_key in wrapped:
                                item[metadata_key] = wrapped[metadata_key]
                        break
            normalized.append(item)

        matching = [
            item for item in normalized
            if isinstance(item.get(required_array), list) and bool(item[required_array])
        ]
        # If no candidates satisfy the minimum schema, return the first candidate
        # so the caller can issue a bounded repair with a precise contract error.
        return matching or normalized[:1]

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
                proposed_new_text = operation.get("new_text")
                if not isinstance(proposed_new_text, str):
                    raise AgentOutputError(f"replace_text needs a string new_text replacement: {path}")
                if proposed_new_text == operation.get("old_text"):
                    raise AgentOutputError(f"No-op replace_text is not allowed; operation must change file content: {path}")
                if not target.is_file():
                    # After a snapshot restore, files created in earlier attempts no longer exist.
                    # Rather than hard-failing the plan, auto-upgrade replace_text → create_file so
                    # the pipeline can proceed deterministically. The patch step will provide content.
                    operation["type"] = "create_file"
                    operation.pop("old_text", None)
                    continue
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
                if operation.get("new_text") == op_old_text:
                    raise AgentOutputError(f"No-op replace_text is not allowed; operation must change file content: {op_path}")
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
