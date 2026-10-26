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

    def __init__(self, message: str, raw_content: str = ""):
        super().__init__(message)
        self.raw_content = raw_content


def extract_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s*```$", "", candidate)
    try:
        result = json.loads(candidate)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    # Recover the outermost JSON object from conversational model output.
    start = candidate.find("{")
    if start >= 0:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(candidate)):
            char = candidate[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        result = json.loads(candidate[start:index + 1])
                        if isinstance(result, dict):
                            return result
                    except json.JSONDecodeError:
                        break
    raise AgentOutputError("Model response did not contain a valid JSON object")


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

    def ask_json(self, user_text: str, *, image_paths: list[str | Path] | None = None) -> tuple[dict[str, Any], LLMResult]:
        messages = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": user_text},
        ]
        result = self.runtime.complete(self.name, messages, image_paths=image_paths)
        attempts = 0
        max_repairs = max(0, int(self.config.get("llm", {}).get("structured_repair_attempts", 1)))
        while True:
            try:
                parsed = extract_json(result.content)
                return parsed, result
            except AgentOutputError as exc:
                if attempts >= max_repairs:
                    raise AgentOutputError(f"{self.name} returned invalid structured output after {attempts + 1} response(s): {exc}") from exc
                attempts += 1
                self.logger.warning("Invalid agent JSON; requesting structured repair", extra={"agent": self.name, "repair_attempt": attempts})
                self.runtime._emit("structured_output_repair", agent=self.name, attempt=attempts, error=str(exc))
                repair_messages = messages + [
                    {"role": "assistant", "content": result.content},
                    {"role": "user", "content": "Your previous response was not valid JSON. Return the same intended result as one valid JSON object only. Do not use markdown fences or comments. Escape newlines inside string values."},
                ]
                result = self.runtime.complete(self.name, repair_messages, image_paths=image_paths)
