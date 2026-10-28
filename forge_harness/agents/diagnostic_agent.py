from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .base import BaseAgent, AgentOutputError
from ..core.state import Task
from ..tools.file_ops import FileOps


class DiagnosticAgent(BaseAgent):
    def __init__(self, *args: Any, file_ops: FileOps | None = None, **kwargs: Any):
        super().__init__("diagnostic", "diagnostic.md", *args, **kwargs)
        self.file_ops = file_ops

    def _get_installed_packages(self) -> set[str]:
        """Read package.json dependencies to know which modules can be imported."""
        if not self.file_ops:
            return set()
        try:
            pkg = self.file_ops.root / "package.json"
            if not pkg.is_file():
                return set()
            data = json.loads(pkg.read_text(encoding="utf-8"))
            deps: set[str] = set()
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                for name in (data.get(section) or {}):
                    deps.add(name.lower())
                    # Also add scoped package short form (e.g. @react-navigation/native -> react-navigation)
                    if "/" in name:
                        deps.add(name.split("/")[-1].lower())
            return deps
        except Exception:
            return set()

    def _find_unresolvable_imports(self, typecheck: dict[str, Any], installed: set[str], allow_install: bool) -> list[str]:
        """Parse TypeScript resolution errors and return modules/names not installed and not installable.

        Handles TS2307 (module not found), TS2304 (name not found, for example a usage of
        ``react-test-renderer`` or Jest globals without installed types), and TS2688 (type
        definition file not found). With dependency installation disabled the only safe
        repair is to remove the unresolvable import/usage, not to edit tsconfig or install
        packages.
        """
        if allow_install:
            return []
        stderr = str(typecheck.get("stderr", "") or "")
        stdout = str(typecheck.get("stdout", "") or "")
        combined = stderr + "\n" + stdout
        missing: list[str] = []
        patterns = [
            r"Cannot find module ['\"]([^'\"]+)['\"]",
            r"Cannot find name ['\"]([^'\"]+)['\"]",
            r"Cannot find type definition file for ['\"]([^'\"]+)['\"]",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, combined, flags=re.IGNORECASE):
                mod = match.group(1)
                # Skip relative imports (./, ../) and node builtins
                if mod.startswith(".") or mod in {"node:fs", "node:path", "node:util"}:
                    continue
                # Normalize: strip @scope/ prefix for comparison
                check = mod.lower()
                if "/" in check:
                    check = check.split("/")[-1]
                if check not in installed:
                    missing.append(mod)
        return list(dict.fromkeys(missing))

    def _add_remove_import_steps(self, diagnosis: dict[str, Any], missing_modules: list[str], affected_files: list[str] | None = None) -> dict[str, Any]:
        """Add next_steps to remove unresolvable imports from affected files."""
        if not missing_modules:
            return diagnosis
        next_steps = list(diagnosis.get("next_steps") or [])
        modules_str = ", ".join(f"'{m}'" for m in missing_modules)
        step = f"Remove imports of unresolvable modules ({modules_str}) from affected source files; these packages are not installed and package.json changes are disabled."
        if step not in next_steps:
            next_steps.append(step)
        diagnosis["next_steps"] = next_steps
        root_causes = list(diagnosis.get("root_causes") or [])
        cause = f"TypeScript cannot resolve modules {modules_str}; package is not installed and dependency installation is disabled"
        existing_causes = [str(c.get("cause", "")) for c in root_causes if isinstance(c, dict)]
        if cause not in existing_causes:
            root_causes.insert(0, {"cause": cause, "evidence": "TS2307 module resolution errors", "confidence": "high"})
        diagnosis["root_causes"] = root_causes
        return diagnosis

    def _verify_target_files_exist(self, diagnosis: dict[str, Any], file_ops: FileOps | None) -> dict[str, Any]:
        """Reject diagnostic claims that a screen file already exists when it does not.

        The LLM diagnostic frequently hallucinates that a screen component is already
        present on disk. Verify each target_files entry; if a claimed-existing screen
        file is missing, mark it as missing and add a next_step to create it so the
        coder cannot skip creating the screen file.
        """
        if not file_ops:
            return diagnosis
        target_files = diagnosis.get("target_files") or []
        missing: list[str] = []
        for target in target_files:
            if not isinstance(target, str):
                continue
            try:
                exists = file_ops.resolve(target, for_write=True).is_file()
            except Exception:
                exists = False
            if not exists:
                missing.append(target)
        if missing:
            diagnosis["missing_files"] = missing
            next_steps = list(diagnosis.get("next_steps") or [])
            for missing_path in missing:
                step = f"Create the missing screen file {missing_path}; it does not exist on disk despite prior claims."
                if step not in next_steps:
                    next_steps.append(step)
            diagnosis["next_steps"] = next_steps
            root_causes = list(diagnosis.get("root_causes") or [])
            cause = f"Diagnostic claimed screen file(s) already exist but they are missing on disk: {', '.join(missing)}"
            existing_causes = [str(c.get("cause", "")) for c in root_causes if isinstance(c, dict)]
            if cause not in existing_causes:
                root_causes.insert(0, {"cause": cause, "evidence": "File existence check failed", "confidence": "high"})
            diagnosis["root_causes"] = root_causes
        return diagnosis

    def _strip_false_existence_claims(self, diagnosis: dict[str, Any], file_ops: FileOps | None) -> dict[str, Any]:
        """Remove diagnostic text that claims a file exists when it does not.

        Scans next_steps and root_causes for phrases asserting a specific screen
        file already contains required UI. If that file is missing on disk, the
        claim is dropped so the coder is not misled into thinking no work is needed.
        """
        if not file_ops:
            return diagnosis
        text_fields = []
        for key in ("next_steps",):
            for item in diagnosis.get(key, []) or []:
                if isinstance(item, str):
                    text_fields.append(item)
        for cause in diagnosis.get("root_causes", []) or []:
            if isinstance(cause, dict):
                for val in (cause.get("cause", ""), cause.get("evidence", "")):
                    if isinstance(val, str):
                        text_fields.append(val)
        referenced = set()
        for match in re.finditer(r"(?:src/)?screens/([A-Za-z_][A-Za-z0-9_]*)\.tsx", " ".join(text_fields), flags=re.IGNORECASE):
            referenced.add(f"src/screens/{match.group(1)}.tsx")
        for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\.tsx\b", " ".join(text_fields)):
            candidate = f"src/screens/{match.group(1)}.tsx"
            referenced.add(candidate)
        for path in referenced:
            try:
                exists = file_ops.resolve(path, for_write=True).is_file()
            except Exception:
                exists = False
            if not exists:
                cleaned: list[str] = []
                for step in diagnosis.get("next_steps", []) or []:
                    if isinstance(step, str) and path in step:
                        lower = step.lower()
                        if any(phrase in lower for phrase in ("already", "contains", "exists", "present", "in place")):
                            continue
                    cleaned.append(step)
                diagnosis["next_steps"] = cleaned
        return diagnosis

    def diagnose(self, task: Task, evidence: dict[str, Any], approved_plan: dict[str, Any] | None = None, file_ops: FileOps | None = None) -> dict[str, Any]:
        text = (
            f"TASK:\n{json.dumps(task.to_dict(), ensure_ascii=False, indent=2)}\n\n"
            f"APPROVED CHANGE PLAN:\n{json.dumps(approved_plan or {}, ensure_ascii=False, indent=2)[:10000]}\n\n"
            f"EVIDENCE:\n{json.dumps(evidence, ensure_ascii=False, indent=2)[:22000]}\n\n"
            "Provide a targeted repair diagnosis, separating confirmed evidence from hypotheses."
        )
        llm_cfg = self.config.get("llm", {}) or {}
        effort_cfg = llm_cfg.get("effort_policy", {}) or {}
        escalation = effort_cfg.get("diagnostic_escalation", {}) if isinstance(effort_cfg, dict) else {}
        threshold = max(1, int(escalation.get("after_task_retries", 2)))
        profile_override = None
        if task.retry_count >= threshold:
            profile_override = {
                "thinking_enabled": True,
                "thinking_budget": max(1024, int(escalation.get("thinking_budget", 2048))),
                "max_output_tokens": max(4000, int(escalation.get("max_output_tokens", 5000))),
            }
        parsed, _result = self.ask_json(text, profile_override=profile_override)
        if not isinstance(parsed.get("next_steps", []), list):
            raise AgentOutputError("Diagnostic response must include next_steps array")
        active_file_ops = file_ops or self.file_ops
        parsed = self._verify_target_files_exist(parsed, active_file_ops)
        parsed = self._strip_false_existence_claims(parsed, active_file_ops)

        # Add a deterministic root cause when a functional assertion failed because the
        # test plan never activated the requested route/tab. Single-page tab shells ignore
        # the browser route, so an assert_visible check for a profile-specific selector
        # times out while the default screen is still rendered.
        render = evidence.get("render") or {}
        test_results = render.get("test_results") or []
        failed_visible = [
            step for step in test_results
            if isinstance(step, dict) and not step.get("passed") and str(step.get("action", "")).strip() == "assert_visible"
        ]
        if failed_visible and str(task.route or "/").strip("/"):
            step = failed_visible[0]
            test_plan = task.test_plan or []
            has_activation = any(
                isinstance(item, dict) and str(item.get("action", "")).strip() in {"click", "navigate"}
                for item in test_plan
            )
            if not has_activation:
                root_causes = list(parsed.get("root_causes") or [])
                cause = (
                    f"Functional test targeted [{step.get('selector')}] on route {task.route} but the test plan did not "
                    f"activate that route/tab; the rendered screenshot showed the default screen instead."
                )
                existing_causes = [str(c.get("cause", "")) for c in root_causes if isinstance(c, dict)]
                if cause not in existing_causes:
                    root_causes.insert(0, {"cause": cause, "evidence": json.dumps(step, ensure_ascii=False), "confidence": "high"})
                    parsed["root_causes"] = root_causes
                next_steps = list(parsed.get("next_steps") or [])
                repair_step = f"Add a click/navigate step before the failing assert_visible step in the test plan for route {task.route}."
                if repair_step not in next_steps:
                    next_steps.append(repair_step)
                    parsed["next_steps"] = next_steps

        # Add deterministic TS2307 repair steps when modules are unresolvable and
        # cannot be installed, so the coder removes the imports instead of leaving
        # broken files that fail type-check on every retry.
        typecheck = evidence.get("typecheck") or {}
        if not typecheck.get("passed"):
            allow_install = bool((self.config.get("execution", {}) or {}).get("allow_dependency_install", False))
            installed = self._get_installed_packages()
            missing = self._find_unresolvable_imports(typecheck, installed, allow_install)
            if missing:
                affected = None
                apply_results = evidence.get("apply_results") or []
                if isinstance(apply_results, list):
                    affected = [str(r.get("path", "")) for r in apply_results if isinstance(r, dict)]
                parsed = self._add_remove_import_steps(parsed, missing, affected)

        return parsed
