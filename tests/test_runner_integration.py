from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml
from rich.console import Console

from forge_harness.config import initialize_project, load_config
from forge_harness.core.runner import HarnessRunner
from forge_harness.utils.renderer import PlaywrightRenderer
from forge_harness.utils.server import ExpoServer


class AgentHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        system = request["messages"][0]["content"]
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
            content = {
                "summary": "Create the initial home screen",
                "risk": "low",
                "files_to_read": [],
                "operations": [{"type": "create_file", "path": "src/screens/HomeScreen.tsx", "target": "home screen", "reason": "Create task screen", "content_outline": "A typed React Native screen"}],
                "validation_plan": ["TypeScript compiles", "Screen renders"],
                "registry_changes": [],
            }
        elif "patch author" in system:
            content = {
                "summary": "Created a basic home screen",
                "operations": [
                    {"type": "create_file", "path": "src/screens/HomeScreen.tsx", "content": "import React from 'react';\nimport { View, Text } from 'react-native';\nexport default function HomeScreen() { return <View><Text>Home</Text></View>; }\n"},
                    {"type": "overwrite_file", "path": "App.tsx", "content": "import React from 'react';\nimport HomeScreen from './src/screens/HomeScreen';\nexport default function App() { return <HomeScreen />; }\n"},
                ],
                "registry_changes": [],
            }
        elif "rigorous QA critic" in system:
            content = {
                "status": "PASS", "functional_score": 95, "visual_score": 90,
                "quality_score": 93, "critical_failure": False, "issues": [],
                "passed_criteria": ["The home screen renders"], "failed_criteria": [], "summary": "Looks valid.",
            }
        elif "diagnostic interpreter" in system:
            content = {"root_causes": [], "repair_strategy": "No repair needed", "repair_scope": "page", "target_files": [], "do_not_change": [], "next_steps": []}
        else:
            content = {"layout": "simple", "regions": [], "design_tokens": {}, "functional_notes": [], "ambiguities": []}
        response = {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(content)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }
        body = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        # A tiny valid one-pixel PNG; critic request is intercepted by the local mock server.
        import base64
        screenshot_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/r3sAAAAASUVORK5CYII="))
        return {
            "url": base_url, "screenshot_path": str(screenshot_path), "duration_s": 0.02,
            "http_status": 200, "title": "Test app", "body_preview": "Home",
            "element_counts": {"buttons": 0, "links": 0, "inputs": 0, "images": 0},
            "page_errors": [], "console_errors": [], "failed_requests": [],
            "test_results": [
                {"action": step.get("action"), "description": step.get("description", ""), "passed": True}
                for step in (test_plan or [])
            ], "functional_pass": True,
        }


def test_runner_completes_simple_screen_with_mock_provider(monkeypatch, tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), AgentHandler)
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
        config_path = project / ".harness" / "config.yaml"
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        cfg["project"]["typecheck_command"] = ["python", "-c", "pass"]
        cfg["project"]["navigation_url"] = f"http://127.0.0.1:{server.server_port}/"
        cfg["llm"]["providers"] = {
            "test": {
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
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
        for agent in cfg["llm"]["agents"].values():
            agent["model"] = "test:mock-model"
            agent["streaming"] = False
            agent["thinking_enabled"] = None
            agent["thinking_budget"] = None
            agent["request_params"] = {}
        cfg["execution"]["default_mode"] = "autonomous"
        cfg["execution"]["max_task_retries"] = 1
        cfg["validation"]["require_visual_critic_when_mockup_exists"] = True
        config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        final_cfg = load_config(project)
        result = HarnessRunner(project, final_cfg, mode="autonomous", console=Console(file=io.StringIO(), width=130, force_terminal=False)).execute(
            spec="Build one simple home screen."
        )
        assert result["status"] == "completed"
        assert result["passed_tasks"] == 1
        assert (project / "src/screens/HomeScreen.tsx").is_file()
        assert (project / ".harness" / "components.json").is_file()
        assert (project / ".harness" / "state.db").is_file()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
