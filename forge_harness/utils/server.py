from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class ExpoServerError(RuntimeError):
    pass


class ExpoServer:
    def __init__(self, project_root: Path, config: dict[str, Any], log_path: Path, logger: logging.Logger | None = None):
        self.project_root = project_root
        self.config = config
        self.log_path = log_path
        self.logger = logger or logging.getLogger("forge.expo")
        self.process: subprocess.Popen[str] | None = None
        self._log_handle = None
        self.url = str(config.get("project", {}).get("navigation_url", "http://127.0.0.1:8081/"))
        self.owned = False

    @staticmethod
    def _port_open(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            return False

    def start(self) -> str:
        project_config = self.config.get("project", {})
        package_json = self.project_root / "package.json"
        if not package_json.exists():
            raise ExpoServerError(f"No package.json at {self.project_root}; initialize or select an Expo project first")
        try:
            import json
            package = json.loads(package_json.read_text(encoding="utf-8"))
            deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
            if "expo" not in deps:
                raise ExpoServerError("This project does not declare Expo in package.json")
        except ExpoServerError:
            raise
        except Exception as exc:
            raise ExpoServerError(f"Could not inspect package.json: {exc}") from exc
        if self._is_ready(self.url, timeout=1.0):
            self.logger.info("Reusing existing Expo web server", extra={"url": self.url})
            return self.url
        if self._port_open("127.0.0.1", self._port_from_url(self.url)):
            raise ExpoServerError(f"Port for {self.url} is occupied but the Expo server did not respond")
        command = project_config.get("expo_command", ["npx", "expo", "start", "--web", "--port", str(self._port_from_url(self.url)), "--non-interactive"])
        # `--non-interactive` is not a valid expo flag in recent versions; use CI=1 instead.
        command = [arg for arg in command if arg != "--non-interactive"]
        env = dict(os.environ)
        env["CI"] = "1"
        timeout = float(project_config.get("server_start_timeout_s", 90))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("a", encoding="utf-8")
        self._log_handle.write(f"\n--- Expo server started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        self._log_handle.flush()
        try:
            self.process = subprocess.Popen(command, cwd=self.project_root, env=env, stdout=self._log_handle, stderr=subprocess.STDOUT, text=True, shell=False)
        except Exception as exc:
            raise ExpoServerError(f"Could not launch Expo server: {exc}") from exc
        self.owned = True
        self.logger.info("Starting Expo web server", extra={"command": command, "url": self.url})
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self.process.poll() is not None:
                tail = self._tail_log()
                raise ExpoServerError(f"Expo exited early with code {self.process.returncode}. Log tail:\n{tail}")
            if self._is_ready(self.url, timeout=1.0):
                self.logger.info("Expo web server ready", extra={"url": self.url, "startup_s": round(time.monotonic() - start, 2)})
                return self.url
            time.sleep(0.5)
        raise ExpoServerError(f"Expo server did not become ready within {timeout}s. Log tail:\n{self._tail_log()}")

    def stop(self) -> None:
        if self.process and self.process.poll() is None and self.owned:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=4)
            self.logger.info("Stopped FORGE-owned Expo server")
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None

    @staticmethod
    def _port_from_url(url: str) -> int:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.port or (443 if parsed.scheme == "https" else 80)

    @staticmethod
    def _is_ready(url: str, timeout: float = 1.0) -> bool:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "FORGE-Harness/0.1"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return 200 <= response.status < 500
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _tail_log(self, lines: int = 35) -> str:
        try:
            content = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(content[-lines:])
        except OSError:
            return "(Expo log unavailable)"
