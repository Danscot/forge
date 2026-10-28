from __future__ import annotations

import json
from pathlib import Path

from forge_harness.utils.server import ExpoServer


class _FakeProcess:
    returncode = None

    def poll(self):
        return None


def _project(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "test-project", "dependencies": {"expo": "1.0.0"}}), encoding="utf-8"
    )
    return tmp_path


def test_ready_server_for_unknown_project_is_not_reused(monkeypatch, tmp_path: Path):
    project = _project(tmp_path)
    config = {"project": {"navigation_url": "http://127.0.0.1:8081/", "server_start_timeout_s": 1}}
    server = ExpoServer(project, config, tmp_path / ".harness" / "expo.log")
    commands = []

    monkeypatch.setattr(server, "_is_ready", lambda url, timeout=1.0: True)
    monkeypatch.setattr(server, "_server_belongs_to_project", lambda port: False)
    monkeypatch.setattr(server, "_find_available_port", lambda start_port, attempts=50: 8082)
    monkeypatch.setattr(
        "forge_harness.utils.server.subprocess.Popen",
        lambda command, **kwargs: (commands.append(list(command)) or _FakeProcess()),
    )

    assert server.start() == "http://127.0.0.1:8082/"
    assert commands and commands[0][commands[0].index("--port") + 1] == "8082"
    assert server.owned is True


def test_ready_server_for_same_project_is_replaced(monkeypatch, tmp_path: Path):
    project = _project(tmp_path)
    config = {"project": {"navigation_url": "http://127.0.0.1:8081/"}}
    server = ExpoServer(project, config, tmp_path / ".harness" / "expo.log")
    ready = iter([True, False, True])
    commands = []
    stopped_ports = []
    monkeypatch.setattr(server, "_is_ready", lambda url, timeout=1.0: next(ready))
    monkeypatch.setattr(server, "_server_belongs_to_project", lambda port: True)
    monkeypatch.setattr(server, "_stop_project_server", lambda port: stopped_ports.append(port))
    monkeypatch.setattr(
        "forge_harness.utils.server.subprocess.Popen",
        lambda command, **kwargs: (commands.append(list(command)) or _FakeProcess()),
    )

    assert server.start() == "http://127.0.0.1:8081/"
    assert stopped_ports == [8081]
    assert commands and commands[0][commands[0].index("--port") + 1] == "8081"
    assert server.owned is True


def test_command_port_is_replaced_not_duplicated():
    command = ["npx", "expo", "start", "--web", "--port", "8081"]
    assert ExpoServer._command_with_port(command, 8091) == ["npx", "expo", "start", "--web", "--port", "8091"]
    assert ExpoServer._command_with_port(["expo", "start", "--port=8081"], 8091) == ["expo", "start", "--port=8091"]
