from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..llm.runtime import LLMError, LLMResult, LLMRuntime


class AgentOutputError(RuntimeError):
    pass


class PatchParseError(AgentOutputError):
    """Raised when an agent's response cannot be parsed as the expected patch/plan JSON.

    Carries the raw model response so it can be persisted for diagnosis.
    """

    def __init__(self, message: str, raw_content: str = "", raw_attempts: list[str] | None = None):
        super().__init__(message)
        self.raw_content = raw_content
        self.raw_attempts = list(raw_attempts or ([raw_content] if raw_content else []))


def extract_json_candidates(text: str) -> list[dict[str, Any]]:
    """Return JSON object candidates from a model response, outermost first.

    Some OpenAI-compatible models/providers prepend a valid but incomplete JSON
    object or emit an intermediate object before the final structured payload.
    The old first-object parser silently selected that object even when a later
    object contained the required operations. We keep every decodable candidate
    so a schema-aware caller can select the one that satisfies its contract.
    """
    candidate = text.strip().lstrip("\ufeff")
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```\s*$", "", candidate)

    decoder = json.JSONDecoder()
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for start, char in enumerate(candidate):
        if char != "{":
            continue
        try:
            result, _ = decoder.raw_decode(candidate[start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(result, dict):
            continue
        # Remove duplicate values caused by looking at nested braces and then
        # encountering the same decoded object at a later candidate position.
        try:
            fingerprint = json.dumps(result, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            fingerprint = repr(result)
        if fingerprint not in seen:
            seen.add(fingerprint)
            results.append(result)
    return results


def extract_json(text: str) -> dict[str, Any]:
    """Extract the first valid JSON object from a model response.

    General-purpose callers retain first-object semantics. Schema-aware agents
    should select a candidate matching the required response contract.
    """
    candidates = extract_json_candidates(text)
    if candidates:
        return candidates[0]
    raise AgentOutputError("Model response did not contain a valid JSON object")


def extract_json_with_nonempty_array(text: str, required_array: str) -> dict[str, Any]:
    """Prefer a JSON object whose required field is a non-empty array.

    If a model response contains an intermediate/stub JSON object followed by
    the actual structured output, first-object extraction can silently select
    the stub. This helper scans every decodable candidate and prefers one that
    satisfies the minimum shape expected by an agent. Common provider wrappers
    (``patch``, ``plan``, ``result`` and ``data``) are normalized conservatively;
    downstream operation/path/plan validation is still mandatory.
    """
    candidates = extract_json_candidates(text)
    if not candidates:
        raise AgentOutputError("Model response did not contain a valid JSON object")

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
        value = item.get(required_array)
        if isinstance(value, list) and value:
            return item
    # Preserve the first candidate so the caller's regular schema validator can
    # produce a precise failure and request a bounded repair.
    return normalized[0]


class BaseAgent:
    def __init__(self, name: str, prompt_name: str, runtime: LLMRuntime, project_root: Path, config: dict[str, Any], logger: logging.Logger | None = None):
        self.name = name
        self.prompt_name = prompt_name
        self.runtime = runtime
        self.project_root = project_root
        self.config = config
        self.logger = logger or logging.getLogger(f"forge.agents.{name}")

    def system_prompt(self) -> str:
        project_harness = self.project_root / str(self.config.get("project", {}).get("harness_dir", ".harness"))
        paths = [project_harness / "prompts" / self.prompt_name, Path(__file__).resolve().parents[1] / "prompts" / self.prompt_name]
        for path in paths:
            try:
                if path.is_file():
                    return path.read_text(encoding="utf-8")
            except OSError:
                continue
        raise AgentOutputError(f"Prompt file is missing: {self.prompt_name}. Run `forge init` to install default prompts.")

    def _structured_profile_override(
        self,
        profile_override: dict[str, Any] | None,
        *,
        schema_name: str,
        schema: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Attach a provider-neutral structured-output contract to a call.

        The runtime translates this abstract schema into the selected endpoint's
        native response_format for each candidate model, including fallbacks. NVIDIA
        supports response_format.json_schema; other providers must opt in explicitly
        if their endpoint/model supports constrained JSON output.
        """
        if not schema:
            return profile_override
        merged = dict(profile_override or {})
        merged["structured_output_schema"] = {"name": schema_name, "schema": schema}
        return merged

    def ask_json(
        self,
        user_text: str,
        *,
        image_paths: list[str | Path] | None = None,
        profile_override: dict[str, Any] | None = None,
        required_nonempty_array: str | None = None,
        json_schema: dict[str, Any] | None = None,
        schema_name: str | None = None,
    ) -> tuple[dict[str, Any], LLMResult]:
        messages = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": user_text},
        ]
        effective_profile = self._structured_profile_override(
            profile_override, schema_name=schema_name or f"forge_{self.name}_response", schema=json_schema,
        )
        response_attempts: list[str] = []
        stage_hint = {
            "forge_coder_change_plan": "planning_changes",
            "forge_coder_patch": "generating_patch",
            "forge_application_task_plan": "planning_tasks",
        }.get(schema_name or f"forge_{self.name}_response")
        result = self.runtime.complete(self.name, messages, image_paths=image_paths, profile_override=effective_profile)
        response_attempts.append(result.content)
        attempts = 0
        max_repairs = max(0, int(self.config.get("llm", {}).get("structured_repair_attempts", 1)))
        while True:
            try:
                parsed = (
                    extract_json_with_nonempty_array(result.content, required_nonempty_array)
                    if required_nonempty_array else extract_json(result.content)
                )
                if required_nonempty_array:
                    required_value = parsed.get(required_nonempty_array)
                    if not isinstance(required_value, list) or not required_value:
                        raise AgentOutputError(
                            f"JSON response must contain a non-empty '{required_nonempty_array}' array"
                        )
                self.runtime._emit(
                    "structured_output_parsed", agent=self.name, schema_name=schema_name or f"forge_{self.name}_response",
                    response_chars=len(result.content), responses_seen=len(response_attempts),
                )
                return parsed, result
            except AgentOutputError as exc:
                truncated = str(getattr(result, "finish_reason", "") or "").lower() in {"length", "max_tokens", "max_output_tokens"}
                if truncated:
                    exc = AgentOutputError(f"response truncated at the output limit ({exc})")
                if attempts >= max_repairs:
                    raise PatchParseError(
                        f"{self.name} returned invalid structured output after {attempts + 1} response(s): {exc}",
                        raw_content=result.content,
                        raw_attempts=response_attempts,
                    ) from exc
                attempts += 1
                self.logger.warning("Invalid agent JSON; requesting structured repair", extra={"agent": self.name, "repair_attempt": attempts})
                self.runtime._emit(
                    "structured_output_repair", agent=self.name, attempt=attempts, stage=stage_hint,
                    error=str(exc), response_chars=len(result.content),
                )
                repair_instruction = (
                    "Your previous response was not valid JSON. Return the same intended result as one valid JSON object only. "
                    "Do not use markdown fences or comments. Escape newlines inside string values."
                )
                if required_nonempty_array:
                    repair_instruction = (
                        "Your previous response did not satisfy the structured-output contract: "
                        f"the top-level '{required_nonempty_array}' field must be a non-empty {required_nonempty_array} array. "
                        "Return one corrected JSON object with real, actionable entries. Do not add a placeholder or no-op "
                        "merely to satisfy the schema. Keep paths project-relative and preserve the requested contract."
                    )
                if truncated:
                    # Don't feed the cut-off reply back; give the model room to finish instead.
                    effective_profile = dict(effective_profile or {})
                    current_cap = int(effective_profile.get("max_output_tokens") or 4096)
                    effective_profile["max_output_tokens"] = min(32000, int(current_cap * 1.75))
                    repair_messages = messages + [{"role": "user", "content": (
                        "Your previous response was cut off by the output limit. Return the complete JSON object again, "
                        "more concisely (shorter descriptions, fewer words in every string)."
                    )}]
                else:
                    repair_messages = messages + [
                        {"role": "assistant", "content": result.content},
                        {"role": "user", "content": repair_instruction},
                    ]
                result = self.runtime.complete(self.name, repair_messages, image_paths=image_paths, profile_override=effective_profile)
                response_attempts.append(result.content)
