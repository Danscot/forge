from __future__ import annotations

import base64
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import pytest
import yaml
from rich.console import Console

from forge_harness.agents.base import PatchParseError
from forge_harness.config import initialize_project, load_config
from forge_harness.core.runner import HarnessRunner
from forge_harness.utils.renderer import PlaywrightRenderer
from forge_harness.utils.server import ExpoServer


class ConfigurableAgentHandler(BaseHTTPRequestHandler):
    """Mock LLM provider whose responses are driven by a shared config dict."""

    config = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        request = json.loads(body)
        system = request["messages"][0]["content"] if request.get("messages") else ""
        mode = self.config.get("mode", "success")
        if mode == "retry_repair":
            self.config.setdefault("requests", []).append(request)

        # Simulate a provider/API exception during a specific agent's call.
        if mode == "llm_error" and "patch author" in system:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"upstream timeout"}}')
            return

        if "Master Planner" in system:
            content = {
                "app_summary": "Test app",
                "tasks": [{
                    "id": "home_screen", "type": "screen", "title": "Build Home Screen",
                    "description": "Create a home screen", "screen": "Home", "route": "/",
                    "mockup_paths": [], "dependencies": [],
                    "acceptance_criteria": ["The home screen renders"],
                    "test_plan": [{"action": "assert_visible", "selector": "body", "description": "Verify the screen content is visible"}],
                }],
            }
        elif "change planner" in system:
            if mode == "empty_plan":
                content = {"summary": "Nothing to do", "risk": "low", "files_to_read": [], "operations": [], "validation_plan": [], "registry_changes": []}
            elif mode == "retry_repair":
                user_message = request["messages"][-1]["content"]
                source_is_present = "--- src/screens/HomeScreen.tsx" in user_message and "<Text>Home</Text>" in user_message
                self.config.setdefault("retry_plan_saw_source", []).append(source_is_present)
                if source_is_present:
                    content = {
                        "summary": "Repair the existing home screen in place",
                        "risk": "low", "files_to_read": ["src/screens/HomeScreen.tsx"],
                        "operations": [{"type": "replace_text", "path": "src/screens/HomeScreen.tsx", "target": "home heading", "reason": "Repair the existing failed attempt", "old_text": "<Text>Home</Text>", "new_text": "<Text>Home repaired</Text>"}],
                        "validation_plan": ["TypeScript compiles"], "registry_changes": [],
                    }
                else:
                    content = {
                        "summary": "Create initial home screen", "risk": "low", "files_to_read": [],
                        "operations": [{"type": "create_file", "path": "src/screens/HomeScreen.tsx", "target": "home screen", "reason": "Create task screen", "content_outline": "A typed React Native screen"}],
                        "validation_plan": ["TypeScript compiles"], "registry_changes": [],
                    }
            else:
                content = {
                    "summary": "Create the initial home screen",
                    "risk": "low",
                    "files_to_read": [],
                    "operations": [{"type": "create_file", "path": "src/screens/HomeScreen.tsx", "target": "home screen", "reason": "Create task screen", "content_outline": "A typed React Native screen"}],
                    "validation_plan": ["TypeScript compiles", "Screen renders"],
                    "registry_changes": [],
                }
        elif "patch author" in system:
            if mode == "retry_repair":
                user_message = request["messages"][-1]["content"]
                if '"type": "replace_text"' in user_message:
                    content = {
                        "summary": "Repair the existing home screen",
                        "operations": [{"type": "replace_text", "path": "src/screens/HomeScreen.tsx", "old_text": "<Text>Home</Text>", "new_text": "<Text>Home repaired</Text>"}],
                        "registry_changes": [],
                    }
                else:
                    content = {
                        "summary": "Created a basic home screen",
                        "operations": [
                            {"type": "create_file", "path": "src/screens/HomeScreen.tsx", "content": "import React from 'react';\nimport { View, Text } from 'react-native';\nexport default function HomeScreen() { return <View><Text>Home</Text></View>; }\n"},
                            {"type": "overwrite_file", "path": "App.tsx", "content": "import React from 'react';\nimport HomeScreen from './src/screens/HomeScreen';\nexport default function App() { return <HomeScreen />; }\n"},
                        ],
                        "registry_changes": [],
                    }
            elif mode == "malformed_patch":
                content = "This is not JSON at all, just conversational text."
            elif mode == "empty_patch":
                content = {"summary": "No ops", "operations": []}
            elif mode == "missing_ops":
                content = {"summary": "No ops key", "registry_changes": []}
            else:
                content = {
                    "summary": "Created a basic home screen",
                    "operations": [
                        {"type": "create_file", "path": "src/screens/HomeScreen.tsx", "content": "import React from 'react';\nimport { View, Text } from 'react-native';\nexport default function HomeScreen() { return <View><Text>Home</Text></View>; }\n"},
                        {"type": "overwrite_file", "path": "App.tsx", "content": "import React from 'react';\nimport HomeScreen from './src/screens/HomeScreen';\nexport default function App() { return <HomeScreen />; }\n"},
                    ],
                    "registry_changes": [],
                }
        elif "rigorous QA critic" in system:
            content = {"status": "PASS", "functional_score": 95, "visual_score": 90, "quality_score": 93, "critical_failure": False, "issues": [], "passed_criteria": [], "failed_criteria": [], "summary": "Looks valid."}
        elif "diagnostic interpreter" in system:
            content = {"root_causes": [], "repair_strategy": "No repair needed", "repair_scope": "page", "target_files": [], "do_not_change": [], "next_steps": []}
        else:
            content = {"layout": "simple", "regions": [], "design_tokens": {}, "functional_notes": [], "ambiguities": []}

        response = {"choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(content)}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
        body_bytes = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def log_message(self, *args):
        return


class FakeExpoServer:
    def __init__(self, project_root, config, log_path, logger=None):
        self.url = config["project"]["navigation_url"]

    def start(self):
        return self.url

    def stop(self):
        return None


class FakeRenderer:
    def __init__(self, project_root, config, logger=None):
        self.project_root = project_root

    def capture(self, base_url, route, screenshot_path, test_plan=None):
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/r3sAAAAASUVORK5CYII="))
        return {
            "url": base_url, "screenshot_path": str(screenshot_path), "duration_s": 0.02,
            "http_status": 200, "title": "Test app", "body_preview": "Home",
            "element_counts": {"buttons": 0, "links": 0, "inputs": 0, "images": 0},
            "page_errors": [], "console_errors": [], "failed_requests": [],
            "test_results": [{"action": step.get("action"), "description": step.get("description", ""), "passed": True} for step in (test_plan or [])],
            "functional_pass": True,
        }


def _make_config(project, server_port, mode="success", typecheck_command=None):
    config_path = project / ".harness" / "config.yaml"
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    cfg["project"]["typecheck_command"] = typecheck_command or ["python3", "-c", "pass"]
    cfg["project"]["navigation_url"] = f"http://127.0.0.1:{server_port}/"
    cfg["llm"]["providers"] = {
        "test": {
            "base_url": f"http://127.0.0.1:{server_port}/v1",
            "api_key_env": "TEST_FORGE_KEY",
            "parameter_map": {"max_output_tokens": "max_tokens"},
            "reasoning_fields": [],
        }
    }
    cfg["llm"]["default_provider"] = "test"
    cfg["llm"]["streaming"] = False
    cfg["llm"]["display_reasoning_stream"] = False
    cfg["llm"]["max_retries"] = 0
    cfg["llm"]["initial_backoff_s"] = 0
    cfg["llm"]["log_payloads"] = False
    for agent in cfg["llm"]["agents"].values():
        agent["model"] = "test:mock-model"
        agent["streaming"] = False
        agent["thinking_enabled"] = None
        agent["thinking_budget"] = None
        agent["request_params"] = {}
    cfg["execution"]["default_mode"] = "autonomous"
    cfg["execution"]["max_task_retries"] = 1
    cfg["validation"]["require_visual_critic_when_mockup_exists"] = True
    cfg["validation"]["require_functional_checks"] = False
    ConfigurableAgentHandler.config = {"mode": mode}
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return load_config(project)


def _run_harness(project, cfg, spec="Build one simple home screen.", monkeypatch=None):
    console = Console(file=io.StringIO(), width=130, force_terminal=False)
    return HarnessRunner(project, cfg, mode="autonomous", console=console).execute(spec=spec)


def test_successful_planning_patch_generation_and_application(monkeypatch, tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ConfigurableAgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TEST_FORGE_KEY", "test-key")
    monkeypatch.setattr("forge_harness.core.runner.ExpoServer", FakeExpoServer)
    monkeypatch.setattr("forge_harness.core.runner.PlaywrightRenderer", FakeRenderer)
    try:
        project = tmp_path / "expo-app"
        project.mkdir()
        (project / "package.json").write_text(json.dumps({"name": "expo-app", "dependencies": {"expo": "1.0.0"}}), encoding="utf-8")
        (project / "App.tsx").write_text("export default function App() { return null; }\n", encoding="utf-8")
        initialize_project(project)
        cfg = _make_config(project, server.server_port, mode="success")
        result = _run_harness(project, cfg, monkeypatch=monkeypatch)
        assert result["status"] == "completed", result
        assert result["passed_tasks"] == 1
        assert (project / "src/screens/HomeScreen.tsx").is_file()
        patch_artifacts = list((project / ".harness" / "runs" / result["run_id"] / "tasks").rglob("patch_attempt_*.json"))
        assert patch_artifacts, "No patch artifact was persisted"
        # Stage transitions should be recorded in the event log.
        events = [e["event_type"] for e in __import__("forge_harness.core.store", fromlist=["StateStore"]).StateStore(project / ".harness" / "state.db").recent_events(result["run_id"])]
        assert "stage" in events, f"No stage events recorded: {events}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_retry_repairs_previous_attempt_without_restoring_task_baseline(monkeypatch, tmp_path: Path):
    """A targeted repair must see files created by the prior failed validation attempt."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), ConfigurableAgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TEST_FORGE_KEY", "test-key")
    monkeypatch.setattr("forge_harness.core.runner.ExpoServer", FakeExpoServer)
    monkeypatch.setattr("forge_harness.core.runner.PlaywrightRenderer", FakeRenderer)
    checks = {"count": 0}

    def typecheck_fails_once(self):
        checks["count"] += 1
        if checks["count"] == 1:
            return {"passed": False, "exit_code": 2, "stdout": "intentional first-attempt type error", "stderr": "", "command": ["mock-typecheck"], "duration_s": 0.01, "timed_out": False}
        return {"passed": True, "exit_code": 0, "stdout": "", "stderr": "", "command": ["mock-typecheck"], "duration_s": 0.01, "timed_out": False}

    monkeypatch.setattr(HarnessRunner, "_run_typecheck", typecheck_fails_once)
    try:
        project = tmp_path / "expo-app"
        project.mkdir()
        (project / "package.json").write_text(json.dumps({"name": "expo-app", "dependencies": {"expo": "1.0.0"}}), encoding="utf-8")
        (project / "App.tsx").write_text("export default function App() { return null; }\n", encoding="utf-8")
        initialize_project(project)
        cfg = _make_config(project, server.server_port, mode="retry_repair")
        result = _run_harness(project, cfg, monkeypatch=monkeypatch)
        assert result["status"] == "completed", result
        assert checks["count"] == 2
        assert ConfigurableAgentHandler.config.get("retry_plan_saw_source") == [False, True], ConfigurableAgentHandler.config
        repaired = (project / "src/screens/HomeScreen.tsx").read_text(encoding="utf-8")
        assert "Home repaired" in repaired
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_malformed_patch_response_persists_raw_and_fails(monkeypatch, tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ConfigurableAgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TEST_FORGE_KEY", "test-key")
    monkeypatch.setattr("forge_harness.core.runner.ExpoServer", FakeExpoServer)
    monkeypatch.setattr("forge_harness.core.runner.PlaywrightRenderer", FakeRenderer)
    try:
        project = tmp_path / "expo-app"
        project.mkdir()
        (project / "package.json").write_text(json.dumps({"name": "expo-app", "dependencies": {"expo": "1.0.0"}}), encoding="utf-8")
        (project / "App.tsx").write_text("export default function App() { return null; }\n", encoding="utf-8")
        initialize_project(project)
        cfg = _make_config(project, server.server_port, mode="malformed_patch")
        result = _run_harness(project, cfg, monkeypatch=monkeypatch)
        assert result["status"] in {"blocked", "failed"}, result
        run_dir = project / ".harness" / "runs" / result["run_id"]
        raw_artifacts = list(run_dir.rglob("raw_response_attempt_*.txt"))
        assert raw_artifacts, "Raw model response was not persisted for diagnosis"
        raw = raw_artifacts[0].read_text(encoding="utf-8")
        assert "This is not JSON" in raw
        # No patch artifact should exist since parsing failed before artifact write.
        patch_artifacts = list(run_dir.rglob("patch_attempt_*.json"))
        assert not patch_artifacts, "Patch artifact should not be written on parse failure"
        # Pre-existing files must be preserved.
        assert (project / "App.tsx").read_text(encoding="utf-8") == "export default function App() { return null; }\n"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_empty_patch_response_rejected(monkeypatch, tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ConfigurableAgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TEST_FORGE_KEY", "test-key")
    monkeypatch.setattr("forge_harness.core.runner.ExpoServer", FakeExpoServer)
    monkeypatch.setattr("forge_harness.core.runner.PlaywrightRenderer", FakeRenderer)
    try:
        project = tmp_path / "expo-app"
        project.mkdir()
        (project / "package.json").write_text(json.dumps({"name": "expo-app", "dependencies": {"expo": "1.0.0"}}), encoding="utf-8")
        (project / "App.tsx").write_text("export default function App() { return null; }\n", encoding="utf-8")
        initialize_project(project)
        cfg = _make_config(project, server.server_port, mode="empty_patch")
        result = _run_harness(project, cfg, monkeypatch=monkeypatch)
        assert result["status"] in {"blocked", "failed"}, result
        assert not (project / "src/screens" / "HomeScreen.tsx").exists(), "Empty patch must not create files"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_llm_exception_during_patch_generation(monkeypatch, tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ConfigurableAgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TEST_FORGE_KEY", "test-key")
    monkeypatch.setattr("forge_harness.core.runner.ExpoServer", FakeExpoServer)
    monkeypatch.setattr("forge_harness.core.runner.PlaywrightRenderer", FakeRenderer)
    try:
        project = tmp_path / "expo-app"
        project.mkdir()
        (project / "package.json").write_text(json.dumps({"name": "expo-app", "dependencies": {"expo": "1.0.0"}}), encoding="utf-8")
        (project / "App.tsx").write_text("export default function App() { return null; }\n", encoding="utf-8")
        initialize_project(project)
        cfg = _make_config(project, server.server_port, mode="llm_error")
        result = _run_harness(project, cfg, monkeypatch=monkeypatch)
        assert result["status"] in {"blocked", "failed"}, result
        # The exception type should be recorded in the last report / event log.
        store = __import__("forge_harness.core.store", fromlist=["StateStore"]).StateStore(project / ".harness" / "state.db")
        run = store.get_run(result["run_id"])
        task = run["tasks"][0]
        report = task.get("last_report") or {}
        assert report.get("stage") in {"generating_patch", "exception"} or "LLMError" in str(report.get("evidence", {})), report
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_typecheck_failure_then_rollback(monkeypatch, tmp_path: Path):
    # typecheck_command that fails -> validation fails -> rollback restores pre-task state.
    server = ThreadingHTTPServer(("127.0.0.1", 0), ConfigurableAgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TEST_FORGE_KEY", "test-key")
    monkeypatch.setattr("forge_harness.core.runner.ExpoServer", FakeExpoServer)
    monkeypatch.setattr("forge_harness.core.runner.PlaywrightRenderer", FakeRenderer)
    try:
        project = tmp_path / "expo-app"
        project.mkdir()
        (project / "package.json").write_text(json.dumps({"name": "expo-app", "dependencies": {"expo": "1.0.0"}}), encoding="utf-8")
        original_app = "export default function App() { return null; }\n"
        (project / "App.tsx").write_text(original_app, encoding="utf-8")
        initialize_project(project)
        cfg = _make_config(project, server.server_port, mode="success", typecheck_command=["python3", "-c", "import sys; sys.exit(1)"])
        result = _run_harness(project, cfg, monkeypatch=monkeypatch)
        assert result["status"] in {"blocked", "failed"}, result
        # Pre-existing App.tsx must be restored to its original content.
        assert (project / "App.tsx").read_text(encoding="utf-8") == original_app
        # No new files should survive rollback.
        assert not (project / "src/screens" / "HomeScreen.tsx").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_interactive_approval_rejection_no_patch(monkeypatch, tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ConfigurableAgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TEST_FORGE_KEY", "test-key")
    monkeypatch.setattr("forge_harness.core.runner.ExpoServer", FakeExpoServer)
    monkeypatch.setattr("forge_harness.core.runner.PlaywrightRenderer", FakeRenderer)
    # Reject the approval prompt.
    monkeypatch.setattr("forge_harness.core.runner.Confirm.ask", lambda *a, **k: False)
    try:
        project = tmp_path / "expo-app"
        project.mkdir()
        (project / "package.json").write_text(json.dumps({"name": "expo-app", "dependencies": {"expo": "1.0.0"}}), encoding="utf-8")
        (project / "App.tsx").write_text("export default function App() { return null; }\n", encoding="utf-8")
        initialize_project(project)
        config_path = project / ".harness" / "config.yaml"
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        cfg["project"]["typecheck_command"] = ["python3", "-c", "pass"]
        cfg["project"]["navigation_url"] = f"http://127.0.0.1:{server.server_port}/"
        cfg["llm"]["providers"] = {"test": {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "api_key_env": "TEST_FORGE_KEY", "parameter_map": {"max_output_tokens": "max_tokens"}, "reasoning_fields": []}}
        cfg["llm"]["default_provider"] = "test"
        cfg["llm"]["streaming"] = False
        cfg["llm"]["max_retries"] = 0
        cfg["execution"]["default_mode"] = "interactive"
        cfg["execution"]["max_task_retries"] = 1
        cfg["validation"]["require_visual_critic_when_mockup_exists"] = True
        config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        final_cfg = load_config(project)
        console = Console(file=io.StringIO(), width=130, force_terminal=False)
        result = HarnessRunner(project, final_cfg, mode="interactive", console=console).execute(spec="Build one simple home screen.")
        assert result["status"] in {"cancelled", "blocked"}, result
        assert not (project / "src/screens" / "HomeScreen.tsx").exists(), "Rejected patch must not create files"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_stage_transitions_logged(monkeypatch, tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ConfigurableAgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TEST_FORGE_KEY", "test-key")
    monkeypatch.setattr("forge_harness.core.runner.ExpoServer", FakeExpoServer)
    monkeypatch.setattr("forge_harness.core.runner.PlaywrightRenderer", FakeRenderer)
    try:
        project = tmp_path / "expo-app"
        project.mkdir()
        (project / "package.json").write_text(json.dumps({"name": "expo-app", "dependencies": {"expo": "1.0.0"}}), encoding="utf-8")
        (project / "App.tsx").write_text("export default function App() { return null; }\n", encoding="utf-8")
        initialize_project(project)
        cfg = _make_config(project, server.server_port, mode="success")
        result = _run_harness(project, cfg, monkeypatch=monkeypatch)
        store = __import__("forge_harness.core.store", fromlist=["StateStore"]).StateStore(project / ".harness" / "state.db")
        stages = [e["payload"].get("stage") for e in store.recent_events(result["run_id"]) if e["event_type"] == "stage"]
        expected = {"planning_changes", "applying_patch", "running_type_checks", "completed"}
        assert expected.issubset(set(stages)), f"Missing stages: {set(stages) - expected}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_patch_parse_error_is_patch_parse_error(monkeypatch, tmp_path: Path):
    # Unit: PatchParseError carries raw content and is an AgentOutputError.
    from forge_harness.agents.base import AgentOutputError
    exc = PatchParseError("bad json", raw_content="{not closed")
    assert isinstance(exc, AgentOutputError)
    assert exc.raw_content == "{not closed"


def test_apply_patch_no_operations_raises(tmp_path: Path):
    from forge_harness.agents.coder_agent import CoderAgent
    from forge_harness.tools.file_ops import FileOperationError, FileOps
    validator = CoderAgent.__new__(CoderAgent)
    with pytest.raises(FileOperationError, match="no operations"):
        validator.apply_patch({"operations": []}, FileOps(tmp_path))


def test_dashboard_pause_resume_toggle():
    from forge_harness.utils.dashboard import Dashboard
    console = Console(file=io.StringIO(), width=130, force_terminal=False)
    d = Dashboard("run1", "interactive", {}, console=console)
    d.start()
    assert d._live is not None
    d.pause()
    assert d._live is None
    d.resume()
    assert d._live is not None
    d.stop("completed")
    assert d._live is None


def test_dashboard_thinking_and_code_streams():
    from forge_harness.utils.dashboard import Dashboard
    console = Console(file=io.StringIO(), width=130, force_terminal=False)
    d = Dashboard("run1", "interactive", {"llm": {"display_reasoning_stream": True}}, console=console)
    d.start()
    # Streamed chunks are buffered into a single message instead of being split
    # into dozens of unrelated transcript entries.
    d.on_llm_event("llm_request_start", {"agent": "coder", "provider": "test", "model": "mock", "attempt": 1, "max_attempts": 1})
    d.on_llm_event("llm_reasoning_delta", {"agent": "coder", "text": "Let me plan the patch structure..."})
    d.on_llm_event("llm_reasoning_delta", {"agent": "coder", "text": " I need to create HomeScreen.tsx"})
    assert d.last_thinking_tokens > 0
    assert d.thinking_tokens > 0
    assert not [m for m in d.messages if m.kind == "thinking"]  # deferred until request completion

    d.on_llm_event("llm_text_delta", {"agent": "coder", "text": '{ "operations": ['})
    d.on_llm_event("llm_text_delta", {"agent": "coder", "text": ' { "type": "create_file" } }'})
    assert not [m for m in d.messages if m.kind == "assistant_output"]

    d.on_llm_event("llm_request_done", {
        "agent": "coder",
        "duration_s": 1.5,
        "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300, "thinking_tokens": 50},
        "finish_reason": "stop",
    })
    assert d.last_thinking_tokens == 50
    assert d.thinking_tokens > 50  # accumulated from deltas + usage
    assert d.total_tokens == 300
    result_msgs = [m for m in d.messages if m.kind == "result"]
    assert len(result_msgs) == 1
    assert "300 tokens" in result_msgs[0].content
    thinking_msgs = [m for m in d.messages if m.kind == "thinking"]
    assert len(thinking_msgs) == 1
    assert "Let me plan" in thinking_msgs[0].content
    outputs = [m for m in d.messages if m.kind == "assistant_output"]
    assert len(outputs) == 1
    assert outputs[0].content == '{ "operations": [ { "type": "create_file" } }'
    d.stop("completed")


def test_dashboard_command_visibility():
    from forge_harness.utils.dashboard import Dashboard
    console = Console(file=io.StringIO(), width=130, force_terminal=False)
    d = Dashboard("run1", "interactive", {}, console=console)
    d.start()
    d.on_command_start(["npx", "tsc", "--noEmit"], agent="coder")
    cmd_msgs = [m for m in d.messages if m.kind == "command"]
    assert len(cmd_msgs) == 1
    assert "$ npx tsc --noEmit" in cmd_msgs[0].content

    d.on_command_done({"exit_code": 0, "duration_s": 2.5, "command": ["npx", "tsc", "--noEmit"], "timed_out": False}, agent="coder")
    result_msgs = [m for m in d.messages if m.kind == "result"]
    assert len(result_msgs) == 1
    assert "exit 0" in result_msgs[0].content
    assert "(2.5s)" in result_msgs[0].content

    # Failed command
    d.on_command_done({"exit_code": 2, "duration_s": 1.0, "command": ["npx", "tsc", "--noEmit"], "timed_out": False}, agent="coder")
    result_msgs = [m for m in d.messages if m.kind == "result"]
    assert len(result_msgs) == 2
    assert "exit 2" in result_msgs[1].content
    d.stop("completed")


def test_dashboard_plan_and_patch():
    from forge_harness.utils.dashboard import Dashboard
    console = Console(file=io.StringIO(), width=130, force_terminal=False)
    d = Dashboard("run1", "interactive", {}, console=console)
    d.start()
    d.on_plan({
        "summary": "Create HomeScreen and update App.tsx",
        "risk": "low",
        "operations": [
            {"type": "create_file", "path": "src/screens/HomeScreen.tsx", "reason": "New screen"},
            {"type": "replace_text", "path": "App.tsx", "reason": "Bootstrap screen"},
        ],
    }, agent="coder")
    plan_msgs = [m for m in d.messages if m.kind == "plan"]
    assert len(plan_msgs) == 1
    assert "Create HomeScreen" in plan_msgs[0].content
    assert "Operations: 2" in plan_msgs[0].content

    d.on_patch_applied([
        {"type": "create_file", "path": "src/screens/HomeScreen.tsx"},
        {"type": "replace_text", "path": "App.tsx"},
    ], agent="coder")
    patch_msgs = [m for m in d.messages if m.kind == "code"]
    assert len(patch_msgs) == 1
    assert "Applied 2 surgical file operation(s)" in patch_msgs[0].content
    d.stop("completed")
