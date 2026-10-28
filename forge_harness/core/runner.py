from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from ..agents.base import AgentOutputError, PatchParseError
from ..agents.coder_agent import CoderAgent
from ..agents.critic_agent import CriticAgent
from ..agents.diagnostic_agent import DiagnosticAgent
from ..agents.planner import PlannerAgent
from ..agents.vision_spec_agent import VisionSpecAgent
from ..config import resolve_project_path
from ..llm.runtime import LLMError, LLMRuntime
from ..logging_setup import set_log_context, setup_logging
from ..tools.file_ops import FileOperationError, FileOps
from ..tools.shell import ShellExecutionError, run_shell
from ..utils.dashboard import Dashboard
from ..utils.input_discovery import discover_inputs
from ..utils.renderer import PlaywrightRenderer, RenderError
from ..utils.server import ExpoServer, ExpoServerError
from .registry import ComponentRegistry
from .router import RefinementRouter
from .state import Task
from .store import StateStore


class HarnessRunError(RuntimeError):
    pass


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + os.urandom(3).hex()


class ExecutionStage:
    """Meaningful, linearly-ordered execution stages for a single task attempt.

    Every pipeline transition is announced through one of these stages so the
    operator can always tell what FORGE is doing (or waiting for).
    """

    PLANNING_CHANGES = "planning_changes"
    AWAITING_APPROVAL = "awaiting_approval"
    PREPARING_SNAPSHOT = "preparing_rollback_snapshot"
    GENERATING_PATCH = "generating_patch"
    PARSING_AND_VALIDATING_PATCH = "parsing_and_validating_patch"
    SAVING_PATCH_ARTIFACT = "saving_patch_artifact"
    APPLYING_PATCH = "applying_patch"
    RUNNING_TYPE_CHECKS = "running_type_checks"
    RUNNING_FUNCTIONAL_VALIDATION = "running_functional_validation"
    RUNNING_VISUAL_VALIDATION = "running_visual_validation"
    RETRYING = "retrying"
    RESTORING_FILES = "restoring_files"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HarnessRunner:
    def __init__(self, project_root: Path, config: dict[str, Any], mode: str | None = None, console: Console | None = None):
        self.project_root = project_root.expanduser().resolve()
        self.config = config
        self.mode = mode or config.get("execution", {}).get("default_mode", "interactive")
        if self.mode not in {"autonomous", "approval", "interactive"}:
            raise HarnessRunError(f"Unknown execution mode: {self.mode}")
        self.console = console or Console()
        harness_relative = str(config.get("project", {}).get("harness_dir", ".harness"))
        self.harness_dir = (self.project_root / harness_relative).resolve()
        self.store = StateStore(self.harness_dir / "state.db")
        self.file_ops = FileOps(self.project_root, config)
        self.run_id: str | None = None
        self.logger = logging.getLogger("forge.runner")
        self.dashboard: Dashboard | None = None
        self.runtime: LLMRuntime | None = None
        self.registry: ComponentRegistry | None = None
        self.current_task_id: str | None = None
        self.current_stage: str | None = None
        self.current_attempt: int = 0
        self.active_agent: str = "orchestrator"
        self._server: ExpoServer | None = None
        self._events_file: Path | None = None
        self._task_artifacts: Path | None = None

    def execute(self, spec: str | None = None, resume_id: str | None = None) -> dict[str, Any]:
        self.harness_dir.mkdir(parents=True, exist_ok=True)
        resume_record = self.store.get_run(resume_id) if resume_id else None
        if resume_id and not resume_record:
            raise HarnessRunError(f"No stored run with ID {resume_id}")
        self.run_id = resume_id or _new_run_id()
        self._events_file = setup_logging(self.project_root, self.run_id, self.config)
        set_log_context(self.run_id, None)
        self.logger = logging.getLogger("forge.runner")
        self.registry = ComponentRegistry(self.harness_dir / "components.json", self.project_root)
        self.dashboard = Dashboard(self.run_id, self.mode, self.config, console=self.console)
        self.dashboard.set_project(self.project_root.name)
        self.dashboard.start()

        if resume_record:
            target_spec = str(resume_record["target_spec"])
            manifest = (resume_record.get("metadata") or {}).get("manifest") or discover_inputs(self.project_root, self.config)
            tasks = [Task.from_dict(item) for item in resume_record.get("tasks", [])]
            for task_index, task in enumerate(tasks):
                if task.status in {"failed", "blocked", "running"}:
                    # A process may have died after a patch reached disk but before its
                    # validation/checkpoint was committed. Restore the pre-task snapshot
                    # before planning again; otherwise a create_file retry can collide
                    # with a half-applied file and a replace_text retry can use stale code.
                    if task.status == "running":
                        self.current_task_id = task.id
                        set_log_context(self.run_id, task.id)
                        snapshot_path = self.harness_dir / "runs" / self.run_id / "tasks" / task.id / "snapshot.json"
                        snapshot_data = self._load_snapshot(snapshot_path)
                        if snapshot_data.get("paths"):
                            self.logger.warning("Recovering interrupted task from its pre-task snapshot", extra={"task_id": task.id})
                            self._restore_snapshot(snapshot_data)
                            self.store.event(self.run_id, "interrupted_task_rolled_back", {"task_id": task.id}, task.id)
                    task.status = "pending"
                    task.retry_count = 0
            self.current_task_id = None
            set_log_context(self.run_id, None)
            self.store.update_run(self.run_id, "running")
            self.store.save_tasks(self.run_id, [task.to_dict() for task in tasks])
            self._log_event("run_resumed", {"run_id": self.run_id, "from_status": resume_record.get("status")})
        else:
            manifest = discover_inputs(self.project_root, self.config)
            target_spec = spec.strip() if isinstance(spec, str) and spec.strip() else str(manifest.get("spec_text", "")).strip()
            if not target_spec:
                self.dashboard.stop("failed")
                raise HarnessRunError("No application specification provided. Pass --spec or fill input/app_spec.md.")
            if not spec and "Describe the application, target users" in target_spec and not manifest.get("mockups"):
                self.dashboard.stop("failed")
                raise HarnessRunError("input/app_spec.md still contains the starter placeholder. Describe your app or pass --spec; add screenshots under input/mockups/.")
            metadata = {"manifest": manifest, "plan": None, "run_schema": 1}
            self.store.create_run(self.run_id, str(self.project_root), target_spec, self.mode, metadata)
            self._log_event("run_created", {"run_id": self.run_id, "mode": self.mode, "project_root": str(self.project_root)})
            self.dashboard.activity_message("INFO", f"Discovered {len(manifest.get('mockups', []))} mockup(s) and {len(manifest.get('assets', []))} asset(s)")
            planner = PlannerAgent(self._runtime(), self.project_root, self.config)
            try:
                plan, tasks = planner.plan(target_spec, manifest, self.file_ops)
                self.dashboard.activity_message("PASS", f"Planner produced {len(tasks)} ordered tasks")
            except Exception as exc:
                self.logger.exception("Master planner failed; using deterministic fallback plan")
                self.dashboard.activity_message("WARN", f"Planner unavailable; fallback queue generated ({exc})")
                plan, tasks = self._fallback_plan(target_spec, manifest)
            metadata["plan"] = plan
            self.store.update_run(self.run_id, "running", current_index=0, metadata=metadata)
            self.store.save_tasks(self.run_id, [task.to_dict() for task in tasks])
            if self.mode == "approval" and tasks:
                self._show_plan(tasks)
                if not Confirm.ask("Approve this task queue and start implementation?", default=False, console=self.console):
                    self.store.update_run(self.run_id, "cancelled")
                    self.dashboard.stop("cancelled")
                    return {"run_id": self.run_id, "status": "cancelled", "tasks": [task.to_dict() for task in tasks]}

        self.dashboard.set_tasks([task.to_dict() for task in tasks])
        self.runtime = self._runtime()
        planner_agent = PlannerAgent(self.runtime, self.project_root, self.config)
        vision_agent = VisionSpecAgent(self.runtime, self.project_root, self.config)
        coder_agent = CoderAgent(self.runtime, self.project_root, self.config)
        critic_agent = CriticAgent(self.runtime, self.project_root, self.config)
        diagnostic_agent = DiagnosticAgent(self.runtime, self.project_root, self.config)
        renderer = PlaywrightRenderer(self.project_root, self.config)
        router = RefinementRouter()
        max_retries = max(0, int(self.config.get("execution", {}).get("max_task_retries", 3)))
        root = self.project_root
        run_dir = self.harness_dir / "runs" / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        self._task_artifacts = run_dir
        overall_status = "completed"

        try:
            for index, task in enumerate(tasks):
                self.current_task_id = task.id
                set_log_context(self.run_id, task.id)
                if task.status == "passed":
                    self.dashboard.activity_message("PASS", f"Skipping validated task {task.title} during resume")
                    continue
                if not router.dependencies_satisfied(task, tasks):
                    task.status = "blocked"
                    task.last_report = {"stage": "dependency", "accepted": False, "error": "One or more dependencies have not passed"}
                    self.store.update_task(self.run_id, task.to_dict(), index)
                    overall_status = "blocked"
                    self.dashboard.activity_message("ERROR", f"{task.title} blocked by an unpassed dependency")
                    break
                self.dashboard.set_task(index, task.title, "vision" if task.mockup_paths else "coder")
                task.status = "running"
                task_dir = run_dir / "tasks" / task.id
                task_dir.mkdir(parents=True, exist_ok=True)
                snapshot_file = task_dir / "snapshot.json"
                snapshot_data = self._load_snapshot(snapshot_file)
                created_paths = set((task.last_report or {}).get("created_paths", []))
                vision_spec = (task.last_report or {}).get("vision_spec")
                last_evidence: dict[str, Any] = {}
                task_passed = False
                blocked_for_model = False

                for attempt_index in range(max_retries + 1):
                    self.dashboard.set_task(index, task.title, "vision" if task.mockup_paths and not vision_spec else "coder")
                    task.retry_count = attempt_index
                    self.current_attempt = attempt_index + 1
                    self.store.update_task(self.run_id, task.to_dict(), index)
                    try:
                        if task.mockup_paths and vision_spec is None:
                            self.dashboard.activity_message("INFO", f"Analyzing {len(task.mockup_paths)} mockup reference(s)")
                            mockup_registry = self.registry.refresh()
                            vision_spec = vision_agent.specify(task, target_spec, self.file_ops, mockup_registry)
                            task.last_report["vision_spec"] = vision_spec
                            self.store.update_task(self.run_id, task.to_dict(), index)
                        diagnostics_in = (task.last_report or {}).get("diagnostic", {})
                        registry_snapshot = self.registry.refresh()
                        self.dashboard.active_agent = "coder"
                        self.active_agent = "coder"
                        # Keep the previous attempt's working tree for targeted repair.
                        # The diagnosis and repair plan are produced from the failed
                        # implementation; restoring the task-start snapshot here would
                        # delete those files and make the next plan stale (for example,
                        # App.tsx imports components that the retry silently removed).
                        # _extend_snapshot preserves the original pre-task contents for
                        # every newly touched path, and the original snapshot is restored
                        # only after the task exhausts retries or is interrupted/resumed.
                        if attempt_index > 0:
                            self.dashboard.activity_message(
                                "INFO",
                                "Continuing from the previous attempt for targeted repair; original snapshot retained for final rollback",
                            )
                        self._set_stage(ExecutionStage.PLANNING_CHANGES)
                        self.dashboard.activity_message("INFO", f"Planning a surgical patch · attempt {attempt_index + 1}/{max_retries + 1}")
                        approved_plan = coder_agent.plan_changes(task, target_spec, vision_spec, registry_snapshot, diagnostics_in, self.file_ops)
                        self._display_change_plan(approved_plan)
                        if self._needs_approval(approved_plan):
                            self._set_stage(ExecutionStage.AWAITING_APPROVAL)
                            self._log_event("awaiting_approval", {
                                "task_id": task.id,
                                "task_title": task.title,
                                "operation_count": len(approved_plan.get("operations", [])),
                                "risk": approved_plan.get("risk", "low"),
                                "attempt": self.current_attempt,
                            })
                            # Stop the live dashboard so the approval prompt is not obscured.
                            if self.dashboard:
                                self.dashboard.pause()
                            try:
                                self.console.print()
                                self.console.print(Panel.fit(
                                    f"[bold yellow]FORGE IS AWAITING YOUR APPROVAL[/bold yellow]\n\n"
                                    f"Task: {task.title}\n"
                                    f"Planned operations: {len(approved_plan.get('operations', []))}\n"
                                    f"Risk: {approved_plan.get('risk', 'low')}\n\n"
                                    f"Type [green]y[/green] then Enter to apply the patch.\n"
                                    f"Type [red]n[/red] then Enter to reject.\n"
                                    f"(Pressing Enter alone counts as rejection.)",
                                    title="APPROVAL REQUIRED", border_style="bold yellow",
                                ))
                                self.console.print()
                                approved = Confirm.ask(
                                    f"Apply {len(approved_plan.get('operations', []))} planned operation(s) to {task.title}?",
                                    default=False, console=self.console,
                                )
                            finally:
                                if self.dashboard:
                                    self.dashboard.resume()
                            if not approved:
                                task.status = "cancelled"
                                task.last_report = {"stage": "approval", "accepted": False, "error": "Patch rejected by user", "vision_spec": vision_spec}
                                self.store.update_task(self.run_id, task.to_dict(), index)
                                overall_status = "cancelled"
                                blocked_for_model = True
                                break
                        self._set_stage(ExecutionStage.PREPARING_SNAPSHOT)
                        self._extend_snapshot(approved_plan, snapshot_data, snapshot_file, created_paths)
                        self._set_stage(ExecutionStage.GENERATING_PATCH)
                        patch = coder_agent.generate_patch(task, approved_plan, vision_spec, registry_snapshot, diagnostics_in, self.file_ops)
                        self._set_stage(ExecutionStage.SAVING_PATCH_ARTIFACT)
                        patch_artifact = task_dir / f"patch_attempt_{attempt_index + 1}.json"
                        patch_artifact.write_text(json.dumps({"plan": approved_plan, "patch": patch}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                        self._set_stage(ExecutionStage.APPLYING_PATCH)
                        apply_results = coder_agent.apply_patch(patch, self.file_ops)
                        if not apply_results:
                            raise AgentOutputError("Patch application reported success but applied no file operations")
                        self.dashboard.on_patch_applied(apply_results, agent=self.active_agent or "coder")
                        if any(Path(str(result.get("path", ""))).name == "package.json" for result in apply_results):
                            install_result = self._run_command(["npm", "install"], timeout=300)
                            install_result["passed"] = install_result.get("exit_code") == 0 and not install_result.get("timed_out")
                            if not install_result["passed"]:
                                raise ShellExecutionError(f"npm install failed after an approved package.json patch: {install_result.get('stderr', '')[-2000:]}")
                            self.dashboard.activity_message("PASS", "Dependencies synchronized with npm install")
                        for result in apply_results:
                            if result.get("type") == "create_file":
                                created_paths.add(str(result.get("path")))
                        self.dashboard.activity_message("PASS", f"Applied {len(apply_results)} surgical file operation(s)")
                        registry_snapshot = self.registry.refresh(patch.get("registry_changes", []))
                        self._save_patch_summary(task_dir, attempt_index, apply_results, approved_plan, patch)

                        self._set_stage(ExecutionStage.RUNNING_TYPE_CHECKS)
                        typecheck = self._run_typecheck()
                        self.dashboard.set_validation("TypeScript", "PASS" if typecheck.get("passed") else "FAIL")
                        evidence: dict[str, Any] = {
                            "typecheck": typecheck,
                            "apply_results": apply_results,
                            "approved_plan": approved_plan,
                            "patch_summary": patch.get("summary", ""),
                            "registry_count": len(registry_snapshot.get("components", {})),
                        }
                        render_result: dict[str, Any] | None = None
                        custom_functional: dict[str, Any] | None = None
                        render_error: str | None = None
                        if typecheck.get("passed"):
                            self._set_stage(ExecutionStage.RUNNING_FUNCTIONAL_VALIDATION)
                            self._set_stage(ExecutionStage.RUNNING_VISUAL_VALIDATION)
                            try:
                                base_url = self._ensure_server()
                                route = task.route or "/"
                                screenshot = task_dir / "screenshots" / f"attempt_{attempt_index + 1}.png"
                                mockup_abs = [str(self.file_ops.resolve(path)) for path in task.mockup_paths]
                                effective_test_plan = self._effective_test_plan(task)
                                if task.test_plan and not effective_test_plan:
                                    self.dashboard.activity_message(
                                        "INFO",
                                        f"Skipped {len(task.test_plan)} screen-level browser check(s) for '{task.type}' task; "
                                        "these checks belong to a screen/integration task.",
                                    )
                                render_result = renderer.capture(base_url, route, screenshot, effective_test_plan)
                                task.last_report["last_screenshot_path"] = str(screenshot)
                                self.dashboard.set_validation("Runtime", "PASS" if not render_result.get("page_errors") else "FAIL")
                                self.dashboard.set_validation("Functional", "PASS" if render_result.get("functional_pass") else "FAIL")
                                failed_steps = [step for step in render_result.get("test_results", []) if not step.get("passed")]
                                if failed_steps:
                                    compact_failures = [
                                        {"action": step.get("action"), "description": step.get("description"), "error": step.get("error")}
                                        for step in failed_steps[:5]
                                    ]
                                    self.logger.warning("Functional browser checks failed: %s", json.dumps(compact_failures, ensure_ascii=False))
                                    self.dashboard.activity_message(
                                        "ERROR",
                                        "Browser checks failed: " + "; ".join(
                                            f"{item.get('description') or item.get('action')}: {item.get('error', 'failed')}"
                                            for item in compact_failures
                                        )[:1200],
                                    )
                                self.dashboard.activity_message("PASS", f"Rendered screenshot: {screenshot.relative_to(root)}")
                                custom_cmd = self.config.get("project", {}).get("functional_check_command", []) or []
                                if custom_cmd:
                                    custom_functional = self._run_command(custom_cmd, timeout=120)
                                    custom_functional["passed"] = custom_functional.get("exit_code") == 0 and not custom_functional.get("timed_out")
                                evidence["render"] = render_result
                                evidence["custom_functional"] = custom_functional
                                if custom_functional:
                                    self.dashboard.activity_message("CHECK", f"Configured functional command exit code: {custom_functional['exit_code']}")
                            except (RenderError, ExpoServerError, ShellExecutionError, OSError) as exc:
                                render_error = str(exc)
                                evidence["render_error"] = render_error
                                self.dashboard.set_validation("Runtime", "FAIL")
                                self.dashboard.set_validation("Functional", "FAIL")
                                self.dashboard.activity_message("ERROR", f"Browser validation failed: {render_error}")
                                if self._server:
                                    try:
                                        self._server.stop()
                                    except Exception:
                                        pass
                                    self._server = None
                        else:
                            evidence["render_skipped"] = "TypeScript gate failed; visual rendering skipped"
                            evidence["render_error"] = "TypeScript check failed"
                            self.dashboard.set_validation("Runtime", "SKIPPED")
                            self.dashboard.set_validation("Functional", "FAIL")

                        critic_report = None
                        visual_review_required = self._requires_visual_review(task)
                        if render_result and task.last_report.get("last_screenshot_path") and visual_review_required:
                            try:
                                mockup_abs = [str(self.file_ops.resolve(path)) for path in task.mockup_paths]
                                critic_report = critic_agent.inspect(
                                    task, target_spec, vision_spec, evidence, mockup_abs,
                                    task.last_report.get("last_screenshot_path"),
                                )
                                self.dashboard.set_validation("Visual critic", str(critic_report.get("status", "FAIL")))
                                self.dashboard.set_score(float(critic_report.get("quality_score", 0)))
                                self.dashboard.activity_message("PASS" if critic_report.get("status") == "PASS" else "WARN", f"Critic verdict: {critic_report.get('status')} · model-reported quality {critic_report.get('reported_quality_score', critic_report.get('quality_score', 0)):.1f}/100")
                            except LLMError as exc:
                                # Don't turn a provider outage into an invented visual verdict.
                                evidence["critic_error"] = str(exc)
                                self.dashboard.set_validation("Visual critic", "UNAVAILABLE")
                                self.dashboard.activity_message("ERROR", "Visual critic unavailable after retries; task will remain resumable")
                                task.last_report = {
                                    "stage": "critic_unavailable", "accepted": False, "evidence": evidence,
                                    "vision_spec": vision_spec, "diagnostic": {"root_causes": [{"cause": "Critic provider unavailable", "evidence": str(exc)}]},
                                    "created_paths": sorted(created_paths), "last_screenshot_path": task.last_report.get("last_screenshot_path"),
                                }
                                self.store.update_task(self.run_id, task.to_dict(), index)
                                overall_status = "blocked"
                                blocked_for_model = True
                                break
                            except Exception as exc:
                                self.logger.exception("Critic failed")
                                evidence["critic_error"] = f"{exc.__class__.__name__}: {exc}"
                                self.dashboard.set_validation("Visual critic", "ERROR")
                        else:
                            self.dashboard.set_validation("Visual critic", "SKIPPED")
                            if render_result and not visual_review_required:
                                evidence["critic_skipped_reason"] = f"Task type '{task.type}' has no visual acceptance scope; visual review is deferred to screen/integration tasks."
                                self.dashboard.activity_message("INFO", f"Visual critic skipped for {task.type} task; technical and functional gates remain mandatory")

                        report = self._evaluate_gates(
                            typecheck, render_result, custom_functional, critic_report, evidence,
                            bool(task.mockup_paths), task.type,
                        )
                        self.dashboard.set_score(float(report.get("quality_score", 0)))
                        self.dashboard.activity_message(
                            "INFO",
                            f"Effective validation score {report.get('quality_score', 0):.1f}/100 "
                            f"(functional {report.get('functional_score', 0):.1f}, visual {report.get('visual_score', 0):.1f}; "
                            f"visual gate {'required' if report.get('visual_gate_required') else 'advisory'})",
                        )
                        report.update({"evidence": evidence, "critic": critic_report, "vision_spec": vision_spec, "created_paths": sorted(created_paths)})
                        if not report.get("accepted"):
                            reasons = report.get("rejection_reasons", [])
                            reason_text = "; ".join(reasons) if reasons else "No rejection reason was recorded"
                            self.dashboard.activity_message(
                                "WARN",
                                f"Acceptance rejected · effective quality {report.get('quality_score', 0):.1f}/100 · {reason_text}",
                            )
                        task.last_report = report
                        decision = router.after_validation(task, attempt_index, report, max_retries)
                        # Router's retry_count is not the same as this loop's index; persist the exact attempt index.
                        task.retry_count = attempt_index
                        self.store.update_task(self.run_id, task.to_dict(), index)
                        if decision.action == "advance":
                            task.status = "passed"
                            task.retry_count = attempt_index
                            task.last_report["accepted"] = True
                            self.store.update_task(self.run_id, task.to_dict(), index)
                            self.dashboard.activity_message("PASS", f"{task.title} passed all mandatory gates")
                            task_passed = True
                            break
                        else:
                            self.dashboard.activity_message("WARN", f"{task.title} failed validation; preparing a targeted repair")
                            try:
                                diagnosis = diagnostic_agent.diagnose(task, evidence, approved_plan)
                            except Exception as diagnostic_exc:
                                diagnosis = self._fallback_diagnosis(evidence, str(diagnostic_exc))
                                self.dashboard.activity_message("WARN", "Diagnostic agent unavailable; using deterministic compiler/runtime summary")
                            task.last_report["diagnostic"] = diagnosis
                            task.last_report["created_paths"] = sorted(created_paths)
                            self.store.update_task(self.run_id, task.to_dict(), index)
                    except KeyboardInterrupt:
                        # User cancellation is not an execution failure; distinguish it clearly.
                        self._set_stage(ExecutionStage.CANCELLED)
                        self.logger.warning("Task interrupted by user", extra={"task_id": task.id, "attempt": self.current_attempt, "stage": self.current_stage})
                        task.last_report = {
                            "stage": "cancelled", "accepted": False,
                            "evidence": {"cancelled_by_user": True, "stage": self.current_stage, "attempt": self.current_attempt},
                            "vision_spec": vision_spec, "created_paths": sorted(created_paths),
                        }
                        self.store.update_task(self.run_id, task.to_dict(), index)
                        self.dashboard.activity_message("WARN", f"Task {task.title} cancelled by user")
                        blocked_for_model = True
                        overall_status = "cancelled"
                        break
                    except (LLMError, AgentOutputError, FileOperationError, ShellExecutionError, Exception) as exc:
                        # Persist the raw model response when patch parsing failed so it can be inspected.
                        raw_persisted = None
                        if isinstance(exc, PatchParseError):
                            raw_attempts = getattr(exc, "raw_attempts", []) or []
                            final_raw = getattr(exc, "raw_content", "") or (raw_attempts[-1] if raw_attempts else "")
                            if final_raw or raw_attempts:
                                try:
                                    # Preserve earlier structured-repair responses too. This
                                    # allows comparison of the invalid first output with the
                                    # final response without rerunning an expensive model call.
                                    for response_index, raw_response in enumerate(raw_attempts[:-1], start=1):
                                        repair_path = task_dir / (
                                            f"raw_response_attempt_{self.current_attempt}_repair_{response_index}.txt"
                                        )
                                        repair_path.write_text(str(raw_response), encoding="utf-8")
                                    if final_raw:
                                        raw_path = task_dir / f"raw_response_attempt_{self.current_attempt}.txt"
                                        raw_path.write_text(str(final_raw), encoding="utf-8")
                                        raw_persisted = str(raw_path)
                                except OSError:
                                    self.logger.exception("Could not persist raw model response")
                        stage = self.current_stage or "unknown"
                        evidence = {
                            "stage_error": f"{exc.__class__.__name__}: {exc}",
                            "stage": stage,
                            "attempt": self.current_attempt,
                            "traceback": traceback.format_exc()[-10000:],
                            "raw_response_persisted": raw_persisted,
                            "files_modified": sorted(created_paths),
                            "rollback_will_occur": True,
                            "retry_will_occur": bool(attempt_index < max_retries and not blocked_for_model),
                        }
                        self.logger.exception("Task attempt failed", extra={"stage": stage, "attempt": self.current_attempt})
                        task.last_report = {
                            "stage": stage, "accepted": False, "evidence": evidence,
                            "vision_spec": vision_spec, "created_paths": sorted(created_paths),
                            "diagnostic": {"root_causes": [{"cause": exc.__class__.__name__, "evidence": str(exc)}], "next_steps": ["Retry the operation after inspecting the exact error."]},
                        }
                        self.store.update_task(self.run_id, task.to_dict(), index)
                        self._log_event("task_attempt_failed", {
                            "task_id": task.id, "stage": stage, "attempt": self.current_attempt,
                            "exception": exc.__class__.__name__, "error_message": str(exc),
                        }, task.id)
                        self.dashboard.activity_message("ERROR", f"Attempt failed at [{stage}] attempt {self.current_attempt}: {exc.__class__.__name__}: {exc}")
                        if isinstance(exc, LLMError) and (exc.fatal or attempt_index >= max_retries):
                            blocked_for_model = True
                            overall_status = "blocked"
                            break
                    if attempt_index < max_retries and not task_passed and not blocked_for_model:
                        self._set_stage(ExecutionStage.RETRYING)
                        self.dashboard.activity_message("INFO", f"Retrying task ({attempt_index + 2}/{max_retries + 1})")
                        time.sleep(0.25)

                if not task_passed:
                    self._set_stage(ExecutionStage.RESTORING_FILES)
                    rollback_ok = True
                    try:
                        self._restore_snapshot(snapshot_data)
                    except Exception as rollback_exc:
                        rollback_ok = False
                        self.logger.exception("Snapshot rollback failed", extra={"task_id": task.id})
                        self.dashboard.activity_message("ERROR", f"Rollback failed: {rollback_exc}")
                    if not blocked_for_model:
                        task.status = "failed"
                        task.last_report["accepted"] = False
                        task.last_report["rollback_ok"] = rollback_ok
                        self.dashboard.activity_message("ERROR", f"Task exhausted its retry budget ({max_retries} retries); restored pre-task files")
                        overall_status = "blocked"
                    else:
                        task.status = "cancelled" if overall_status == "cancelled" else "blocked"
                        task.last_report["accepted"] = False
                        task.last_report["rollback_ok"] = rollback_ok
                    self.store.update_task(self.run_id, task.to_dict(), index)
                    self.store.update_run(self.run_id, overall_status, current_index=index)
                    break
                self.store.update_run(self.run_id, "running", current_index=index + 1)
                self.store.event(self.run_id, "task_passed", {"task_id": task.id, "title": task.title, "attempts": task.retry_count + 1}, task.id)

            if all(task.status == "passed" for task in tasks):
                overall_status = "completed"
            elif overall_status == "completed":
                overall_status = "blocked"
            self.store.update_run(self.run_id, overall_status, current_index=len([t for t in tasks if t.status == "passed"]))
            self._set_stage({"completed": ExecutionStage.COMPLETED, "blocked": ExecutionStage.FAILED, "cancelled": ExecutionStage.CANCELLED}[overall_status])
            self.dashboard.update_runtime_totals(self.runtime)
            self.dashboard.stop(overall_status)
            final = {
                "run_id": self.run_id,
                "status": overall_status,
                "project_root": str(self.project_root),
                "passed_tasks": sum(1 for task in tasks if task.status == "passed"),
                "total_tasks": len(tasks),
                "tasks": [task.to_dict() for task in tasks],
                "log_file": str(self._events_file) if self._events_file else None,
                "source_only": True,
            }
            self.console.print(f"\n[bold]FORGE run {overall_status.upper()}[/bold] · {final['passed_tasks']}/{final['total_tasks']} tasks passed")
            self.console.print(f"[dim]Run ID: {self.run_id} · Logs: {final['log_file']}[/dim]")
            return final
        finally:
            if self._server:
                try:
                    self._server.stop()
                except Exception:
                    self.logger.exception("Could not stop Expo server cleanly")
                self._server = None

    def _runtime(self) -> LLMRuntime:
        if self.runtime:
            return self.runtime
        def callback(kind: str, payload: dict[str, Any]) -> None:
            if self.dashboard:
                self.dashboard.on_llm_event(kind, payload)
            # Avoid storing every streamed token as a database event; streaming is visible in TUI.
            if kind not in {"llm_text_delta", "llm_reasoning_delta"} and self.run_id:
                self.store.event(self.run_id, kind, payload, self.current_task_id)
            if kind not in {"llm_text_delta", "llm_reasoning_delta"}:
                self.logger.info("LLM runtime event", extra={"event_type": kind, **{k: v for k, v in payload.items() if k not in {"text"}}})
        self.runtime = LLMRuntime(self.config, logger=logging.getLogger("forge.llm"), event_callback=callback)
        return self.runtime

    def _log_event(self, event_type: str, payload: dict[str, Any], task_id: str | None = None) -> None:
        try:
            self.store.event(self.run_id, event_type, payload, task_id)
        except Exception:
            self.logger.exception("Could not persist state event")
        self.logger.info(event_type, extra={"event_type": event_type, **payload})

    def _set_stage(self, stage: str) -> None:
        self.current_stage = stage
        self._log_event("stage", {"stage": stage, "attempt": self.current_attempt}, self.current_task_id)
        if self.dashboard:
            self.dashboard.activity_message("STAGE", f"{stage} · attempt {self.current_attempt}")

    def _fallback_plan(self, spec: str, manifest: dict[str, Any]) -> tuple[dict[str, Any], list[Task]]:
        mockups = manifest.get("mockups", [])
        tasks: list[Task] = []
        if mockups:
            for index, item in enumerate(mockups):
                base = re.sub(r"[^a-z0-9_]+", "_", Path(str(item.get("path", "screen"))).stem.lower()).strip("_") or f"screen_{index + 1}"
                tasks.append(Task(
                    id=f"screen_{base}", type="screen", title=f"Implement {item.get('inferred_screen') or base}",
                    description=f"Implement a screen based on mockup {item.get('path')}. Application spec: {spec[:800]}",
                    screen=str(item.get("inferred_screen") or base), route="/" if index == 0 else f"/{base}",
                    mockup_paths=[str(item["path"])],
                    acceptance_criteria=["Render the screen without runtime errors", "Preserve accessible interactions"],
                    test_plan=[{"action": "assert_visible", "selector": "body", "description": "Verify the rendered screen has visible content"}],
                ))
        else:
            tasks.append(Task(
                id="main_screen", type="screen", title="Implement the initial application screen",
                description=f"Implement the smallest useful first screen described by the specification: {spec[:1200]}",
                screen="Main", route="/", acceptance_criteria=["The application renders", "Primary controls have accessible labels"],
                test_plan=[{"action": "assert_visible", "selector": "body", "description": "Verify the rendered screen has visible content"}],
            ))
        return {"app_summary": "Deterministic fallback plan used because the Planner agent was unavailable", "tasks": [task.to_dict() for task in tasks]}, tasks

    def _show_plan(self, tasks: list[Task]) -> None:
        table = Table(title="Proposed implementation queue", expand=True)
        table.add_column("#", style="cyan", width=3)
        table.add_column("Task")
        table.add_column("Type", style="magenta")
        table.add_column("Dependencies", style="dim")
        for idx, task in enumerate(tasks, 1):
            table.add_row(str(idx), task.title, task.type, ", ".join(task.dependencies) or "—")
        self.console.print(table)

    def _display_change_plan(self, plan: dict[str, Any]) -> None:
        risk = str(plan.get("risk", "low")).upper()
        style = {"LOW": "green", "MEDIUM": "yellow", "HIGH": "bold red"}.get(risk, "white")
        self.dashboard.activity_message("PLAN", f"Change plan ({risk} risk): {plan.get('summary', 'No summary')}")
        self.dashboard.on_plan(plan, agent=self.active_agent or "coder")
        table = Table(title=f"Surgical change plan · {risk} risk", expand=True)
        table.add_column("Operation", style="cyan")
        table.add_column("File")
        table.add_column("Purpose")
        for operation in plan.get("operations", []):
            table.add_row(str(operation.get("type")), str(operation.get("path")), str(operation.get("reason") or operation.get("target") or "—")[:100])
        self.console.print(f"[bold {style}]Risk: {risk}[/bold {style}]")
        self.console.print(table)

    def _needs_approval(self, plan: dict[str, Any]) -> bool:
        execution = self.config.get("execution", {})
        if self.mode == "autonomous":
            return bool(execution.get("approve_high_risk_in_autonomous", False)) and str(plan.get("risk", "low")).lower() == "high"
        if self.mode == "approval":
            return True
        return bool(execution.get("approve_each_patch", True)) or str(plan.get("risk", "low")).lower() == "high"

    def _run_typecheck(self) -> dict[str, Any]:
        command = self.config.get("project", {}).get("typecheck_command", ["npx", "tsc", "--noEmit"])
        if not command:
            return {"passed": True, "skipped": True, "exit_code": 0, "stdout": "Typecheck command disabled by config", "stderr": ""}
        result = self._run_command(command, timeout=120)
        result["passed"] = result.get("exit_code") == 0 and not result.get("timed_out")
        return result

    def _run_command(self, command: list[str] | str, timeout: int = 60) -> dict[str, Any]:
        if self.dashboard:
            self.dashboard.on_command_start(command, agent=self.active_agent or "orchestrator")
        result = run_shell(command, cwd=self.project_root, timeout=timeout, config=self.config, logger=logging.getLogger("forge.shell"))
        if self.dashboard:
            self.dashboard.on_command_done(result, agent=self.active_agent or "orchestrator")
        return result

    def _ensure_server(self) -> str:
        if self._server:
            return self._server.url
        run_dir = self._task_artifacts or (self.harness_dir / "runs" / str(self.run_id))
        self._server = ExpoServer(
            self.project_root, self.config, run_dir / "logs" / "expo.log", logger=logging.getLogger("forge.expo")
        )
        return self._server.start()

    @staticmethod
    def _requires_visual_review(task: Task) -> bool:
        """Visual acceptance belongs to screen/integration tasks or an explicit mockup."""
        task_type = str(task.type or "").strip().lower()
        return bool(task.mockup_paths) or task_type in {"screen", "integration"}

    @staticmethod
    def _requires_functional_review(task: Task) -> bool:
        """Browser interaction assertions are scoped to a screen, integration, or mockup.

        Foundation/design-token/shared-component tasks may smoke-render the existing
        application, but must not be forced to satisfy screen-level selectors before a
        later task mounts those components.
        """
        task_type = str(task.type or "").strip().lower()
        return bool(task.mockup_paths) or task_type in {"screen", "integration"}

    @classmethod
    def _effective_test_plan(cls, task: Task) -> list[dict[str, Any]]:
        """Return only browser checks appropriate to this task's acceptance scope."""
        return list(task.test_plan) if cls._requires_functional_review(task) else []

    def _evaluate_gates(
        self,
        typecheck: dict[str, Any],
        render: dict[str, Any] | None,
        custom_functional: dict[str, Any] | None,
        critic: dict[str, Any] | None,
        evidence: dict[str, Any],
        has_mockup: bool,
        task_type: str = "screen",
    ) -> dict[str, Any]:
        cfg = self.config.get("validation", {})
        typecheck_pass = bool(typecheck.get("passed")) if cfg.get("require_typecheck", True) else True
        render_pass = bool(render) and (not render.get("page_errors") if cfg.get("fail_on_page_errors", True) else True)
        if cfg.get("require_runtime_render", True) and not render:
            render_pass = False
        task_type = str(task_type or "").strip().lower()
        functional_gate_required = bool(task_type in {"screen", "integration"} or has_mockup)
        task_tests = (render or {}).get("test_results", [])
        task_tests_pass = all(item.get("passed", False) for item in task_tests)
        custom_pass = custom_functional is None or bool(custom_functional.get("passed"))
        if functional_gate_required:
            functional_pass = bool(render and render.get("functional_pass") and task_tests_pass and custom_pass)
            if cfg.get("require_functional_checks", False) and not (task_tests or custom_functional):
                functional_pass = False
        else:
            # For foundation/design-token/shared-component tasks, arbitrary test_plan
            # entries are out of scope. Keep a deterministic render/runtime smoke gate
            # and any explicitly configured functional command, without forcing a later
            # screen's controls/selectors to exist yet.
            functional_pass = bool(render) and (not render.get("page_errors") if cfg.get("fail_on_page_errors", True) else True) and custom_pass
        if cfg.get("fail_on_console_errors", False) and render and render.get("console_errors"):
            functional_pass = False

        # Visual scoring is a blocking acceptance gate only when this task is expected
        # to produce a screen/integration or explicitly references a mockup. Otherwise,
        # a partial screenshot (for example, a foundation task before HomeScreen exists)
        # is not valid evidence that the shared-component task failed.
        visual_gate_required = bool(
            task_type in {"screen", "integration"}
            or (has_mockup and cfg.get("require_visual_critic_when_mockup_exists", True))
        )
        weights = cfg.get("functional_weight", 0.6), cfg.get("visual_weight", 0.4)
        functional_score = float((critic or {}).get("functional_score", 100 if functional_pass else 0))
        visual_score = float((critic or {}).get("visual_score", 0))
        total_weight = max(0.0001, float(weights[0]) + float(weights[1]))
        if visual_gate_required:
            quality_score = (functional_score * float(weights[0]) + visual_score * float(weights[1])) / total_weight
            critic_pass = bool(critic and str(critic.get("status", "FAIL")).upper() == "PASS" and not critic.get("critical_failure", False))
            high_issues = [issue for issue in (critic or {}).get("issues", []) if str(issue.get("severity", "")).lower() in {"critical", "high"}]
        else:
            # Do not let the critic's view of an intentionally incomplete screen
            # overrule the deterministic checks for a non-visual task.
            quality_score = 100.0 if typecheck_pass and render_pass and functional_pass else 0.0
            critic_pass = True
            high_issues = []
        score_pass = quality_score >= float(cfg.get("minimum_quality_score", 75))
        accepted = typecheck_pass and render_pass and functional_pass and critic_pass and score_pass and not high_issues
        if visual_gate_required and critic is None:
            accepted = False

        rejection_reasons: list[str] = []
        if not typecheck_pass:
            rejection_reasons.append("TypeScript/type-check gate failed")
        if not render_pass:
            rejection_reasons.append("Runtime render gate failed")
        if not functional_pass:
            rejection_reasons.append("Functional checks failed or required functional evidence is missing")
        if visual_gate_required and not critic_pass:
            rejection_reasons.append("Visual critic did not pass or reported a critical failure")
        if not score_pass:
            rejection_reasons.append(
                f"Effective quality score {quality_score:.1f} is below minimum {float(cfg.get('minimum_quality_score', 75)):.1f}"
            )
        if high_issues:
            rejection_reasons.append(f"Visual critic reported {len(high_issues)} high/critical issue(s)")
        if visual_gate_required and critic is None:
            rejection_reasons.append("Visual review is required for this task but no critic report was produced")
        return {
            "accepted": accepted,
            "typecheck_pass": typecheck_pass,
            "runtime_pass": render_pass,
            "functional_pass": functional_pass,
            "functional_gate_required": functional_gate_required,
            "visual_gate_required": visual_gate_required,
            "critic_pass": critic_pass,
            "quality_score": round(quality_score, 2),
            "functional_score": round(functional_score, 2),
            "visual_score": round(visual_score, 2),
            "score_pass": score_pass,
            "high_severity_issues": high_issues,
            "rejection_reasons": rejection_reasons,
            "gate_summary": {
                "typecheck": typecheck_pass, "runtime": render_pass, "functional": functional_pass,
                "functional_scope_required": functional_gate_required,
                "critic": critic_pass, "quality_threshold": score_pass,
            },
        }

    def _fallback_diagnosis(self, evidence: dict[str, Any], error: str) -> dict[str, Any]:
        tc = evidence.get("typecheck", {})
        render = evidence.get("render", {})
        causes = []
        if not tc.get("passed"):
            causes.append({"cause": "TypeScript validation failed", "evidence": (tc.get("stderr", "") + "\n" + tc.get("stdout", ""))[-3000:], "confidence": "high"})
        if evidence.get("render_error"):
            causes.append({"cause": "Browser render failed", "evidence": evidence["render_error"], "confidence": "high"})
        for page_error in render.get("page_errors", [])[:5]:
            causes.append({"cause": "Browser runtime error", "evidence": page_error, "confidence": "high"})
        for failed in render.get("test_results", []):
            if not failed.get("passed"):
                causes.append({"cause": "Functional test failed", "evidence": json.dumps(failed, ensure_ascii=False), "confidence": "high"})
        if not causes:
            causes.append({"cause": "Critic reported quality defects", "evidence": json.dumps(evidence.get("critic", {}), ensure_ascii=False)[:3000] or error, "confidence": "medium"})
        return {"root_causes": causes, "repair_strategy": "Apply the smallest patch directly related to the confirmed evidence.", "repair_scope": "page", "target_files": [], "do_not_change": ["Unrelated screens and shared behavior"], "next_steps": ["Inspect the exact diagnostics", "Patch only the responsible file", "Re-run required checks"]}

    def _load_snapshot(self, path: Path) -> dict[str, Any]:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("paths", {})
                    data.setdefault("created_paths", [])
                    return data
            except Exception:
                self.logger.exception("Snapshot metadata could not be read")
        return {"paths": {}, "created_paths": []}

    def _extend_snapshot(self, plan: dict[str, Any], snapshot: dict[str, Any], snapshot_path: Path, created_paths: set[str]) -> None:
        existing_snap = snapshot.setdefault("paths", {})
        relatives = [str(operation.get("path", "")) for operation in plan.get("operations", [])]
        if self.config.get("execution", {}).get("allow_dependency_install", False) and any(Path(path).name == "package.json" for path in relatives):
            relatives.extend(["package-lock.json", "yarn.lock", "pnpm-lock.yaml"])
        for relative in relatives:
            if not relative or relative in existing_snap:
                continue
            try:
                full = self.file_ops.resolve(relative)
            except FileOperationError:
                continue
            if relative in created_paths:
                existing_snap[relative] = {"existed": False}
            elif full.is_file():
                original = full.read_text(encoding="utf-8", errors="replace")
                existing_snap[relative] = {"existed": True, "content_b64": base64.b64encode(original.encode("utf-8")).decode("ascii")}
            else:
                existing_snap[relative] = {"existed": False}
        snapshot["created_paths"] = sorted(created_paths)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _restore_snapshot(self, snapshot: dict[str, Any]) -> None:
        paths = snapshot.get("paths", {}) or {}
        self.dashboard.activity_message("WARN", f"Restoring {len(paths)} touched file(s) to their pre-task state")
        for relative, record in paths.items():
            try:
                if Path(relative).name in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}:
                    path = (self.project_root / relative).resolve()
                    path.relative_to(self.project_root.resolve())
                else:
                    path = self.file_ops.resolve(relative, for_write=True)
                if not record.get("existed"):
                    path.unlink(missing_ok=True)
                    continue
                content = base64.b64decode(record.get("content_b64", "")).decode("utf-8")
                self.file_ops._atomic_write(path, content)
            except Exception:
                self.logger.exception("Snapshot rollback failed for %s", relative)

    def _save_patch_summary(self, task_dir: Path, attempt: int, apply_results: list[dict[str, Any]], plan: dict[str, Any], patch: dict[str, Any]) -> None:
        summary = {
            "attempt": attempt + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "plan_summary": plan.get("summary", ""),
            "risk": plan.get("risk", "unknown"),
            "apply_results": apply_results,
            "patch_summary": patch.get("summary", ""),
            "registry_changes": patch.get("registry_changes", []),
        }
        directory = task_dir / "patches"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"attempt_{attempt + 1}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
