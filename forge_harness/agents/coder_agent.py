from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .base import BaseAgent, AgentOutputError, PatchParseError, extract_json, extract_json_candidates
from ..core.state import Task
from ..tools.file_ops import FileOperationError, FileOps

CODER_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "files_to_read": {"type": "array", "items": {"type": "string"}},
        "operations": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["create_file", "replace_text", "overwrite_file"]},
                    "path": {"type": "string", "minLength": 1},
                    "target": {"type": "string"},
                    "reason": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "content_outline": {"type": "string"},
                },
                "required": ["type", "path"],
                "additionalProperties": False,
            },
        },
        "validation_plan": {"type": "array", "items": {"type": "string"}},
        "registry_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "string"},
                    "purpose": {"type": "string"},
                    "props": {"type": "object"},
                },
                "required": ["name", "path", "purpose", "props"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "risk", "files_to_read", "operations", "validation_plan", "registry_changes"],
    "additionalProperties": False,
}

CODER_PATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "operations": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["create_file", "replace_text", "overwrite_file"]},
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["type", "path"],
                "additionalProperties": False,
            },
        },
        "registry_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "string"},
                    "purpose": {"type": "string"},
                    "props": {"type": "object"},
                },
                "required": ["name", "path", "purpose", "props"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "operations", "registry_changes"],
    "additionalProperties": False,
}


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
        # Use native constrained JSON output when supported by the selected provider.
        # Keep the same schema on the repair call below, not just the initial request.
        structured_profile = self._structured_profile_override(
            profile_override, schema_name="forge_coder_change_plan", schema=CODER_PLAN_SCHEMA,
        )
        parsed, result = self.ask_json(
            user_text,
            profile_override=structured_profile,
            required_nonempty_array="operations",
            schema_name="forge_coder_change_plan",
        )
        response_attempts = [result.content]
        # JSON can parse successfully while still violating the plan contract (for
        # example, an empty operations array). Repair that schema failure locally
        # instead of burning a complete task retry and repeating the same planning
        # work. The retry remains bounded and still passes through the same validator.
        max_plan_repairs = max(0, int(self.config.get("llm", {}).get("structured_repair_attempts", 1)))
        for repair_index in range(max_plan_repairs + 1):
            try:
                self._normalize_plan(parsed, file_ops)
                self._normalize_stale_repair_anchors(task, parsed, file_ops, diagnostics)
                self._ensure_screen_mount_operation(task, parsed, file_ops, diagnostics)
                self._validate_plan(parsed, file_ops)
                self._validate_diagnostic_coverage(parsed, diagnostics)
                return parsed
            except AgentOutputError as exc:
                if repair_index >= max_plan_repairs:
                    raise PatchParseError(
                        f"Coder change plan remained invalid after {len(response_attempts)} response(s): {exc}",
                        raw_content=result.content,
                        raw_attempts=response_attempts,
                    ) from exc
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
                result = self.runtime.complete("coder", repair_messages, profile_override=structured_profile)
                response_attempts.append(result.content)
                try:
                    parsed = self._extract_contract_candidates(result.content, "operations")[0]
                except AgentOutputError as parse_exc:
                    if repair_index + 1 >= max_plan_repairs + 1:
                        raise PatchParseError(
                            f"Coder change plan could not be parsed after {len(response_attempts)} response(s): {parse_exc}",
                            raw_content=result.content, raw_attempts=response_attempts,
                        ) from parse_exc
                    parsed = {}
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
            create_count = sum(1 for op in planned_operations if op.get("type") in {"create_file", "overwrite_file"})
            replace_count = sum(1 for op in planned_operations if op.get("type") == "replace_text")
            estimated_budget = 1500 + (4000 * create_count) + (1250 * replace_count)
            profile_override["max_output_tokens"] = min(hard_cap, max(2500, estimated_budget))
        profile_override = self._structured_profile_override(
            profile_override, schema_name="forge_coder_patch", schema=CODER_PATCH_SCHEMA,
        )
        response_attempts: list[str] = []
        last_error: AgentOutputError | None = None

        for repair_index in range(max_repairs + 1):
            result = self.runtime.complete("coder", messages, profile_override=profile_override)
            response_attempts.append(result.content)
            if self._was_truncated(result):
                # The model hit max_tokens mid-JSON. Re-asking with the same cap (and the cut-off
                # reply in the history) just truncates again, so grow the cap and drop the partial reply.
                current_cap = int(profile_override.get("max_output_tokens") or 4000)
                ceiling = max(current_cap, int(patch_cfg.get("truncation_ceiling_tokens", 32000)))
                last_error = AgentOutputError(
                    f"Coder patch response was truncated at the output limit (max_output_tokens={current_cap}, "
                    f"response_chars={len(result.content)})"
                )
                self.runtime._emit(
                    "output_truncated", agent="coder", stage="generating_patch",
                    max_output_tokens=current_cap, response_chars=len(result.content),
                    finish_reason=getattr(result, "finish_reason", None),
                )
                if repair_index >= max_repairs or current_cap >= ceiling:
                    raise PatchParseError(
                        f"{last_error}. Raise llm.patch_generation.max_output_tokens or split the task.",
                        raw_content=result.content, raw_attempts=response_attempts,
                    ) from last_error
                profile_override["max_output_tokens"] = min(ceiling, current_cap * 2)
                messages = base_messages + [{
                    "role": "user",
                    "content": (
                        "Your previous response was cut off by the output limit before the JSON finished. "
                        "Return the complete JSON object again. Keep files compact (no explanatory comments, "
                        "no unused styles) so the whole object fits."
                    ),
                }]
                continue
            try:
                parsed_candidates = self._extract_contract_candidates(result.content, "operations")
                json_candidates = extract_json_candidates(result.content)
                self.runtime._emit(
                    "structured_response_candidates",
                    agent="coder", stage="generating_patch", response_chars=len(result.content),
                    json_candidate_count=len(json_candidates),
                    candidate_keys=[sorted(item.keys())[:16] for item in json_candidates[:5]],
                    candidate_operation_counts=[
                        len(item.get("operations", [])) if isinstance(item.get("operations"), list) else None
                        for item in parsed_candidates[:5]
                    ],
                )
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
    def _was_truncated(result: Any) -> bool:
        reason = str(getattr(result, "finish_reason", "") or "").lower()
        return reason in {"length", "max_tokens", "max_output_tokens"}

    @staticmethod
    def _extract_contract_candidates(text: str, required_array: str) -> list[dict[str, Any]]:
        """Return schema-shaped JSON candidates, prioritizing non-empty arrays.

        Do not assume the first decodable object is the final answer. Keep all
        candidates with a non-empty operations array so the caller can test each
        one against the approved operation plan before selecting it.
        """
        candidates = extract_json_candidates(text)
        if not candidates:
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

    @staticmethod
    def _normalize_plan(plan: dict[str, Any], file_ops: FileOps) -> None:
        """Repair harmless planning mistakes deterministically instead of burning an LLM round.

        - drop replace_text operations whose new_text equals old_text (model padding)
        - create_file on an existing file -> overwrite_file; overwrite_file on a missing file -> create_file
        - remap create_file/overwrite_file paths when the same file exists at a different
          normalized location (e.g. src/App.tsx vs App.tsx at project root)
        """
        operations = plan.get("operations")
        if not isinstance(operations, list):
            return
        kept: list[dict[str, Any]] = []
        for op in operations:
            if not isinstance(op, dict):
                continue
            kind = op.get("type")
            if kind == "replace_text" and op.get("new_text") == op.get("old_text"):
                continue
            path = op.get("path")
            if path and kind in {"create_file", "overwrite_file"}:
                try:
                    target = file_ops.resolve(path, for_write=True)
                    exists = target.is_file()
                except FileOperationError:
                    exists = None
                if exists is True and kind == "create_file":
                    op["type"] = "overwrite_file"
                elif exists is False and kind == "overwrite_file":
                    op["type"] = "create_file"
                # If the planned path doesn't exist but an equivalent normalized path does,
                # remap to the real on-disk path so validation and patch matching align.
                if not exists:
                    normalized = CoderAgent._normalize_path(path)
                    for candidate in (path, normalized):
                        try:
                            cand_target = file_ops.resolve(candidate, for_write=True)
                        except FileOperationError:
                            continue
                        if cand_target.is_file() and candidate != path:
                            op["path"] = candidate
                            break
            kept.append(op)
        plan["operations"] = kept

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Normalize a project-relative path for cross-plan/patch comparison.

        Strips leading ./ and src/ prefixes and normalizes separators so that
        src/App.tsx, ./App.tsx, and App.tsx all compare equal.
        """
        if not isinstance(path, str):
            return ""
        p = path.replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        # strip a leading src/ when the same file exists at project root
        if p.startswith("src/") and len(p) > 4:
            root = p[4:]
            return root
        return p

    @staticmethod
    def _write_kind(op_type: str) -> str:
        return "write_file" if op_type in {"create_file", "overwrite_file"} else op_type

    @staticmethod
    def _types_compatible(plan_type: str, patch_type: str) -> bool:
        """Patch operation type can satisfy a planned operation type.

        overwrite_file and create_file are equivalent (both write full file
        content). A replace_text plan can be satisfied by an overwrite_file or
        create_file patch since writing the whole file subsumes any text
        replacement. A replace_text patch can also satisfy a replace_text plan.
        """
        write_kinds = {"create_file", "overwrite_file"}
        if plan_type in write_kinds and patch_type in write_kinds:
            return True
        if plan_type == "replace_text" and patch_type in write_kinds:
            return True
        if plan_type in write_kinds and patch_type == "replace_text":
            return True
        return plan_type == patch_type

    @staticmethod
    def _diagnostic_notes(diagnostics: dict[str, Any] | None) -> str:
        if not isinstance(diagnostics, dict) or not diagnostics:
            return ""
        parts: list[str] = []
        strategy = diagnostics.get("repair_strategy")
        if strategy:
            parts.append(str(strategy))
        next_steps = diagnostics.get("next_steps") or []
        parts.extend(str(step) for step in next_steps if step)
        for cause in diagnostics.get("root_causes") or []:
            if isinstance(cause, dict):
                parts.append(str(cause.get("cause", "")))
                parts.append(str(cause.get("evidence", "")))
        text = " ".join(parts)
        selectors = []
        for match in re.finditer(
            r"(?:testID|data-testid)\s*=\s*\{?\s*\\*[\"'`]([A-Za-z][A-Za-z0-9_-]*)\\*[\"'`]",
            json.dumps(diagnostics, ensure_ascii=False),
            flags=re.IGNORECASE,
        ):
            selectors.append(match.group(1))
        selectors = list(dict.fromkeys(selectors))
        if selectors:
            text += " Required UI/test selectors: " + ", ".join(selectors) + ". Render and wire these exact elements; do not make unrelated changes."
        return text[:6000]

    def _normalize_stale_repair_anchors(
        self,
        task: Task,
        plan: dict[str, Any],
        file_ops: FileOps,
        diagnostics: dict[str, Any] | None,
    ) -> None:
        """Promote stale text edits to bounded whole-file repairs after a failed validation.

        A stale exact anchor on a repair attempt means the previous patch or another repair
        already changed the source. Re-asking the model to reconstruct the same anchor wastes
        the task retry budget. For a diagnosed repair, rewrite only the already-approved target
        file and carry the observed failure requirements into its content outline.
        """
        if not isinstance(diagnostics, dict) or not diagnostics:
            return
        notes = self._diagnostic_notes(diagnostics)
        acceptance = "; ".join(str(item) for item in (task.acceptance_criteria or []) if item)
        for operation in plan.get("operations", []) or []:
            if not isinstance(operation, dict) or operation.get("type") != "replace_text":
                continue
            path = str(operation.get("path") or "")
            anchor = operation.get("old_text")
            try:
                target = file_ops.resolve(path, for_write=True)
                source = target.read_text(encoding="utf-8") if target.is_file() else ""
            except Exception:
                continue
            if isinstance(anchor, str) and anchor and source.count(anchor) == 1:
                continue
            # Preserve the plan's stated intent, but use a full-file contract because the
            # exact replacement anchor is no longer valid in the current source.
            intent = " ".join(str(operation.get(key, "")) for key in ("target", "reason", "new_text", "content_outline") if operation.get(key))
            operation["type"] = "overwrite_file"
            operation["target"] = operation.get("target") or f"Repair the existing {Path(path).name} against current source and failed validation evidence"
            operation["reason"] = operation.get("reason") or f"The previous exact anchor no longer matches current source for {path}; avoid repeating a stale text replacement."
            operation["content_outline"] = (
                f"Rewrite the complete existing file {path} using the current source excerpt and actual exported APIs. "
                f"Preserve unrelated working behavior. Task: {task.title}. Acceptance criteria: {acceptance}. "
                f"Planned intent: {intent}. Diagnostic evidence and required repair: {notes}"
            )[:9000]
            operation.pop("old_text", None)
            operation.pop("new_text", None)
            if str(plan.get("risk", "low")).lower() == "low":
                plan["risk"] = "medium"
            logging.getLogger("forge.coder").warning(
                "Promoted stale replace_text plan to overwrite_file for diagnosed repair: %s", path
            )

    def _ensure_screen_mount_operation(
        self,
        task: Task,
        plan: dict[str, Any],
        file_ops: FileOps,
        diagnostics: dict[str, Any] | None,
    ) -> None:
        """Ensure a component-based screen task includes the Expo root entrypoint when needed."""
        task_type = str(getattr(task, "type", "") or "").lower()
        if task_type not in {"screen", "integration"}:
            return
        operations = plan.get("operations")
        if not isinstance(operations, list):
            return
        # Expo Router owns routes under app/; do not rewrite its entry point.
        try:
            package = json.loads((file_ops.root / "package.json").read_text(encoding="utf-8"))
            main = str(package.get("main") or "")
            if "expo-router/entry" in main:
                return
        except Exception:
            pass
        root_app = file_ops.root / "App.tsx"
        index_candidates = [file_ops.root / name for name in ("index.ts", "index.tsx", "index.js", "index.jsx")]
        explicitly_registers_root_app = any(
            candidate.is_file() and re.search(r"from\s*['\"](?:\./)?App(?:\.[jt]sx?)?['\"]", candidate.read_text(encoding="utf-8", errors="replace"))
            for candidate in index_candidates
        )
        if not root_app.is_file():
            return
        # For a vanilla Expo project with App.tsx, that is the default root even if an
        # explicit index.ts is absent. If an index exists and clearly registers App, it
        # is also unambiguous that App.tsx is the mounted entrypoint.
        if any(candidate.is_file() for candidate in index_candidates) and not explicitly_registers_root_app:
            return
        screen_operations = []
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            path = str(operation.get("path") or "").replace("\\", "/")
            if path.lower().endswith(".tsx") and ("/screens/" in f"/{path.lower()}" or path.lower().startswith("screens/")):
                screen_operations.append(operation)
        if not screen_operations and isinstance(diagnostics, dict):
            # If the diagnosis names the missing screen file but the new plan omits it,
            # reintroduce that approved diagnostic target as a complete-file repair.
            # This stops an entrypoint-only or unrelated fix from satisfying the plan.
            for candidate in diagnostics.get("target_files", []) or []:
                candidate_path = str(candidate).replace("\\", "/")
                if not candidate_path.lower().endswith(".tsx") or not ("/screens/" in f"/{candidate_path.lower()}" or candidate_path.lower().startswith("screens/")):
                    continue
                try:
                    exists = file_ops.resolve(candidate_path, for_write=True).is_file()
                except Exception:
                    continue
                if exists:
                    # The screen file already exists; a missing-selector failure is a
                    # wiring/test-plan issue, not a reason to rewrite an existing file.
                    continue
                repair_operation = {
                    "type": "create_file",
                    "path": candidate_path,
                    "target": f"Implement diagnosed screen component {Path(candidate_path).stem}",
                    "reason": "Prior functional evidence shows the rendered screen is missing required elements; this diagnosed screen file must be created, not merely described.",
                    "content_outline": (
                        f"Create a complete React Native screen file at {candidate_path}. Task: {task.title}. "
                        f"Acceptance criteria: {'; '.join(str(item) for item in (task.acceptance_criteria or []) if item)}. "
                        f"Use existing component/type/theme APIs exactly. {self._diagnostic_notes(diagnostics)}"
                    )[:9000],
                }
                operations.append(repair_operation)
                screen_operations.append(repair_operation)
                logging.getLogger("forge.coder").warning("Added diagnosed screen file to repair plan: %s", candidate_path)
                break
        if not screen_operations:
            return
        route = str(getattr(task, "route", "") or "/").strip("/")
        screen_operation = screen_operations[-1]
        relative_screen = str(screen_operation.get("path")).replace("\\", "/")
        component_name = Path(relative_screen).stem
        if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", component_name):
            return
        if route:
            # Non-default routes (for example /profile) are activated by the planner's
            # test plan, typically a tab click. Do not force the screen onto the
            # App.tsx home/default branch; that breaks tabbed shells and contradicts
            # the route/test-plan activation contract.
            return
        try:
            app_source = root_app.read_text(encoding="utf-8", errors="replace")
        except OSError:
            app_source = ""
        imported = False
        # Inspect the import clause rather than the whole line: a module path containing
        # "HomeScreen" must not be mistaken for an import of the HomeScreen symbol.
        for match in re.finditer(r"import\s+([\s\S]*?)\s+from\s*['\"][^'\"]+['\"]", app_source):
            if re.search(rf"\b{re.escape(component_name)}\b", match.group(1)):
                imported = True
                break
        mounted = bool(re.search(rf"<\s*{re.escape(component_name)}(?:\s|/|>)", app_source))
        if imported and mounted:
            # Only short-circuit if the imported screen file actually exists on disk.
            # An inline definition or a missing imported file still needs a real screen file.
            import_path = None
            for match in re.finditer(rf"from\s*['\"]([^'\"]+)['\"]", app_source):
                if re.search(rf"\b{re.escape(component_name)}\b", match.group(0)):
                    import_path = match.group(1)
                    break
            if import_path:
                try:
                    resolved = (root_app.parent / import_path).resolve()
                    if resolved.is_file() or resolved.with_suffix(".tsx").is_file():
                        return
                except Exception:
                    pass
        # A stale/nonfunctional App.tsx operation should be promoted to a whole-file edit.
        app_op = next((op for op in operations if str(op.get("path") or "").replace("\\", "/") in {"App.tsx", "./App.tsx"}), None)
        relative_import = "./" + str(Path(relative_screen).with_suffix("")).replace("\\", "/").lstrip("./")
        notes = self._diagnostic_notes(diagnostics)
        acceptance = "; ".join(str(item) for item in (task.acceptance_criteria or []) if item)
        outline = (
            f"Rewrite the complete root App.tsx entrypoint to import the default {component_name} component from '{relative_import}' "
            f"and actually render <{component_name} /> on the home/default tab or route. Preserve existing profile tab/navigation behavior, "
            f"other working views, StatusBar, and current theme imports. Do not merely add an unused import. "
            f"Task: {task.title}. Acceptance criteria: {acceptance}. "
            f"The current App.tsx does not both import and render {component_name}. {notes}"
        )[:9000]
        if app_op is None:
            operations.append({
                "type": "overwrite_file",
                "path": "App.tsx",
                "target": f"Mount {component_name} from the root Expo entrypoint",
                "reason": f"The screen component in {relative_screen} must be rendered by App.tsx; the current root entrypoint does not import and mount it.",
                "content_outline": outline,
            })
            if str(plan.get("risk", "low")).lower() == "low":
                plan["risk"] = "medium"
            logging.getLogger("forge.coder").warning("Added required root entrypoint operation to screen plan: App.tsx -> %s", component_name)
        else:
            app_op["type"] = "overwrite_file"
            app_op["target"] = app_op.get("target") or f"Mount {component_name} from the root Expo entrypoint"
            app_op["reason"] = app_op.get("reason") or f"App.tsx does not currently render {component_name}."
            app_op["content_outline"] = outline
            app_op.pop("old_text", None)
            app_op.pop("new_text", None)
            if str(plan.get("risk", "low")).lower() == "low":
                plan["risk"] = "medium"
            logging.getLogger("forge.coder").warning("Promoted root entrypoint change to complete-file write: App.tsx -> %s", component_name)

    def _validate_plan(self, plan: dict[str, Any], file_ops: FileOps) -> None:
        operations = plan.get("operations")
        if not isinstance(operations, list) or not operations:
            raise AgentOutputError("Change plan must contain a non-empty operations array")
        if str(plan.get("risk", "low")).lower() not in {"low", "medium", "high"}:
            raise AgentOutputError("Change plan risk must be low, medium, or high")
        for operation in operations:
            operation_type = operation.get("type")
            if operation_type not in {"create_file", "replace_text", "overwrite_file"}:
                raise AgentOutputError(f"Unsupported planned operation: {operation_type}")
            path = operation.get("path")
            if not path:
                raise AgentOutputError("Every planned operation requires a path")
            try:
                target = file_ops.resolve(path, for_write=True)
            except FileOperationError as exc:
                raise AgentOutputError(str(exc)) from exc
            normalized_path = str(path).replace("\\", "/")
            if normalized_path in {"tsconfig.json", "package.json"} or normalized_path.startswith("tsconfig.json"):
                raise AgentOutputError(f"Direct changes to {path} are not permitted by the coder; update harness configuration instead.")
            if operation_type == "overwrite_file" and target.exists() and not target.is_file():
                raise AgentOutputError(f"overwrite_file target is not a file: {path}")
            if operation_type == "create_file" and target.exists():
                raise AgentOutputError(f"Plan attempts to create an existing file: {path}")
            if operation_type == "replace_text":
                proposed_new_text = operation.get("new_text")
                if not isinstance(proposed_new_text, str):
                    raise AgentOutputError(f"replace_text needs a string new_text replacement: {path}")
                if proposed_new_text == operation.get("old_text"):
                    raise AgentOutputError(f"No-op replace_text is not allowed; operation must change file content: {path}")
                if not target.is_file():
                    # Keep the approved plan immutable. A replacement against a missing file
                    # is a planning error, not permission to silently change replace_text into
                    # create_file (which changes the safety contract for patch generation).
                    raise AgentOutputError(
                        f"replace_text target does not exist: {path}; plan a create_file operation instead"
                    )
                anchor = operation.get("old_text")
                if not isinstance(anchor, str) or not anchor:
                    raise AgentOutputError(f"replace_text needs an exact old_text anchor: {path}")
                content = target.read_text(encoding="utf-8")
                count = content.count(anchor)
                if count != 1:
                    raise AgentOutputError(f"Planned anchor for {path} matched {count} times; require exactly one")

    @staticmethod
    def _validate_diagnostic_coverage(plan: dict[str, Any], diagnostics: dict[str, Any] | None) -> None:
        """Reject repair plans that ignore explicit failing testIDs in diagnostics.

        This is deliberately narrow and deterministic: when the diagnostic names stable
        testID/data-testid selectors, the planned changes must account for every selector
        in their implementation description or replacement content. It prevents a model
        from proposing an unrelated cleanup while claiming a failed screen already works.
        """
        if not isinstance(diagnostics, dict) or not diagnostics:
            return
        diagnostic_text = json.dumps({
            "root_causes": diagnostics.get("root_causes", []),
            "next_steps": diagnostics.get("next_steps", []),
            "repair_strategy": diagnostics.get("repair_strategy", ""),
        }, ensure_ascii=False)
        # Only explicit assignments count: testID="x", data-testid=\\"x\\", testID={'x'}.
        # Prose such as "testID: string" or "testID requirement" is never an identifier.
        found: list[str] = []
        for match in re.finditer(
            r"(?:testID|data-testid)\s*=\s*\{?\s*\\*[\"'`]([A-Za-z][A-Za-z0-9_-]*)\\*[\"'`]",
            diagnostic_text,
            flags=re.IGNORECASE,
        ):
            found.append(match.group(1))
        identifiers = list(dict.fromkeys(found))
        if not identifiers:
            return
        descriptive_parts = [str(plan.get("summary", ""))]
        for operation in plan.get("operations", []) or []:
            if isinstance(operation, dict):
                descriptive_parts.extend(str(operation.get(key, "")) for key in (
                    "target", "reason", "content_outline", "new_text"
                ))
        planned_text = "\n".join(descriptive_parts).lower()
        planned_paths = {
            CoderAgent._normalize_path(str(operation.get("path", "")))
            for operation in (plan.get("operations", []) or []) if isinstance(operation, dict)
        }
        diagnosed_screen_paths = [
            CoderAgent._normalize_path(str(path))
            for path in (diagnostics.get("target_files", []) or [])
            if str(path).lower().endswith(".tsx")
            and ("/screens/" in f"/{str(path).lower()}" or str(path).lower().startswith("screens/"))
        ]
        missing_screen_ops = [path for path in diagnosed_screen_paths if path not in planned_paths]
        if missing_screen_ops:
            raise AgentOutputError(
                "Repair plan must include a real file operation for diagnosed screen target(s): "
                + ", ".join(missing_screen_ops)
                + ". Do not fix only imports or describe a screen repair without editing the screen file."
            )
        missing = [identifier for identifier in identifiers if identifier.lower() not in planned_text]
        if missing:
            raise AgentOutputError(
                "Repair plan ignores diagnostic testID requirement(s): "
                + ", ".join(missing)
                + ". Include each identifier in the relevant operation's target/reason/content_outline/new_text "
                "and implement the corresponding missing UI or behavior; do not plan unrelated cleanup."
            )

    @staticmethod
    def _project_uses_react_native(file_ops: FileOps) -> bool:
        try:
            package = json.loads((file_ops.root / "package.json").read_text(encoding="utf-8"))
            deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
            return "react-native" in deps or "expo" in deps
        except Exception:
            return False

    @staticmethod
    def _normalize_react_native_test_ids(operations: list[dict[str, Any]], file_ops: FileOps) -> None:
        """Keep generated React Native JSX on the native ``testID`` prop.

        React Native Web renders ``testID`` as the DOM attribute ``data-testid``.
        Diagnostics sometimes tell the coder to "add data-testid" to a component,
        which is silently dropped by React Native. Normalize that mistake in patch
        content before validation so a repair cannot regress a working selector.
        """
        if not CoderAgent._project_uses_react_native(file_ops):
            return
        attr = re.compile(r"(?<![\w\-\[\"'])data-testid(?=\s*=)")
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            path = str(operation.get("path") or "").replace("\\", "/").lower()
            if not path.endswith((".tsx", ".jsx")):
                continue
            for key in ("content", "new_text"):
                value = operation.get(key)
                if not isinstance(value, str) or "data-testid" not in value:
                    continue
                normalized = attr.sub("testID", value)
                if normalized != value:
                    operation[key] = normalized
                    logging.getLogger("forge.coder").warning(
                        "Normalized data-testid JSX attribute to React Native testID in %s", operation.get("path")
                    )

    def _validate_patch_against_plan(self, operations: list[dict[str, Any]], plan: dict[str, Any], file_ops: FileOps) -> None:
        self._normalize_react_native_test_ids(operations, file_ops)
        # Match operations one-to-one. A simple (type, path) dictionary is unsafe here:
        # multiple distinct anchor replacements in one file would overwrite each other.
        planned = list(plan.get("operations", []))
        used: set[int] = set()
        accepted: list[dict[str, Any]] = []
        dropped: list[str] = []
        for operation in operations:
            op_type = str(operation.get("type"))
            op_path = str(operation.get("path"))
            op_old_text = operation.get("old_text")
            candidates = [
                (index, item) for index, item in enumerate(planned)
                if index not in used
                and self._types_compatible(str(item.get("type")), op_type)
                and self._normalize_path(str(item.get("path"))) == self._normalize_path(op_path)
                and (op_type != "replace_text" or str(item.get("type")) != "replace_text" or self._anchors_match(op_old_text, item.get("old_text")))
            ]
            if not candidates:
                # An unplanned extra operation is dropped (and reported) rather than failing the
                # whole patch. The approved plan stays the authority for what may change; if the
                # dropped edit was truly needed, type-check/functional gates will surface it.
                dropped.append(f"{op_type}:{op_path}")
                continue
            plan_index, approved = candidates[0]
            used.add(plan_index)
            if op_type == "replace_text":
                if not isinstance(operation.get("new_text"), str):
                    raise AgentOutputError(f"replace_text requires string new_text: {op_path}")
                if operation.get("new_text") == op_old_text:
                    raise AgentOutputError(f"No-op replace_text is not allowed; operation must change file content: {op_path}")
                target = file_ops.resolve(op_path, for_write=True)
                plan_old_text = approved.get("old_text")
                if not isinstance(plan_old_text, str) or not target.is_file() or target.read_text(encoding="utf-8").count(plan_old_text) != 1:
                    raise AgentOutputError(f"Patch anchor is no longer unique in {op_path}")
                operation["old_text"] = plan_old_text
                if operation.get("new_text") == plan_old_text:
                    raise AgentOutputError(f"No-op replace_text is not allowed: {op_path}")
            else:
                if not isinstance(operation.get("content"), str) or not operation["content"].strip():
                    raise AgentOutputError(f"New/rewritten file has empty content: {op_path}")
                # Use the true state of the disk, not the model's guess.
                exists = file_ops.resolve(op_path, for_write=True).is_file()
                operation["type"] = "overwrite_file" if exists else "create_file"
            accepted.append(operation)
        if dropped:
            logging.getLogger("forge.coder").warning("Dropped unplanned patch operation(s): %s", dropped)
        operations[:] = accepted
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
        parts = []
        for path in self._ordered_source_files(root):
            try:
                relative = str(path.relative_to(root))
                if path.stat().st_size <= 120_000:
                    # Do NOT use FileOps.cat_file here: it adds line-number prefixes that
                    # the model may copy into old_text, making exact anchors impossible.
                    source = path.read_text(encoding="utf-8", errors="replace")
                    parts.append(f"--- {relative} (raw source; no line numbers) ---\n{source[:9000]}")
                else:
                    parts.append(f"--- {relative} (large file; omitted) ---")
            except Exception:
                continue
        return "\n".join(parts)[:40000] or "No existing source files found. New app files may be created."

    def _sources_for_plan(self, plan: dict[str, Any], file_ops: FileOps) -> str:
        excerpts = []
        seen: set[str] = set()
        for operation in plan.get("operations", []):
            if operation.get("type") not in {"replace_text", "overwrite_file"}:
                continue
            relative = str(operation.get("path"))
            if relative in seen:
                continue
            seen.add(relative)
            try:
                path = file_ops.resolve(relative)
                # Include raw, unnumbered source so exact anchors are copied literally.
                content = path.read_text(encoding="utf-8", errors="replace")
                excerpts.append(f"--- {relative} (raw source; no line numbers) ---\n{content[:12000]}")
            except Exception as exc:
                excerpts.append(f"--- {relative}: unable to read ---\n{exc}")
        targets = "\n".join(excerpts)[:26000] or "No existing-file patch targets; only new files are planned."
        # New files must be written against the REAL interfaces of existing code (component
        # props, exported names, theme tokens, data shapes). Without this the patch author
        # guesses (default vs named imports, prop names, token names) and tsc fails.
        context = self._project_context(file_ops, exclude=seen)
        return targets + ("\n\nEXISTING PROJECT FILES (import from these exactly as exported; do not guess names):\n" + context if context else "")

    _PRIORITY = ("theme", "types", "constants", "data", "utils", "hooks", "components", "screens")

    def _ordered_source_files(self, root: Path) -> list[Path]:
        found: list[Path] = []
        for base_name in ("App.tsx", "app", "src", "components", "screens"):
            base = root / base_name
            if base.is_file():
                found.append(base)
            elif base.is_dir():
                found.extend(
                    p for p in base.rglob("*")
                    if p.is_file() and p.suffix.lower() in {".tsx", ".ts", ".jsx", ".js", ".json"}
                    and "node_modules" not in p.parts and ".git" not in p.parts
                )
        def rank(path: Path) -> tuple[int, str]:
            rel = str(path.relative_to(root)).lower()
            for i, key in enumerate(self._PRIORITY):
                if key in rel:
                    return (i, rel)
            return (len(self._PRIORITY) + (1 if path.name == "App.tsx" else 0), rel)
        return sorted(dict.fromkeys(found), key=rank)[:100]

    def _project_context(self, file_ops: FileOps, exclude: set[str], per_file: int = 6000, total: int = 24000) -> str:
        root = file_ops.root
        parts: list[str] = []
        for path in self._ordered_source_files(root):
            relative = str(path.relative_to(root))
            if relative in exclude:
                continue
            try:
                if path.stat().st_size > 120_000:
                    continue
                parts.append(f"--- {relative} (raw source) ---\n{path.read_text(encoding='utf-8', errors='replace')[:per_file]}")
            except Exception:
                continue
        return "\n".join(parts)[:total]
