from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse


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
        configured_url = self.url
        configured_port = self._port_from_url(configured_url)
        if self._is_ready(configured_url, timeout=1.0):
            if self._server_belongs_to_project(configured_port):
                # Reusing a server left behind by an earlier run can serve a stale
                # in-memory Metro bundle even though the files on disk are current.
                # Replace only a verified server for this project; never kill an
                # unrelated process that happens to occupy the configured port.
                self.logger.info(
                    "Replacing existing Expo web server for this project to avoid a stale bundle",
                    extra={"url": configured_url, "project_root": str(self.project_root)},
                )
                self._stop_project_server(configured_port)
                if self._is_ready(configured_url, timeout=1.0):
                    new_port = self._find_available_port(configured_port + 1)
                    self.url = self._url_with_port(configured_url, new_port)
                    self.logger.warning(
                        "Old Expo server is still listening; starting a fresh server on an isolated port",
                        extra={"existing_url": configured_url, "url": self.url},
                    )
            else:
                # A healthy HTTP response only proves that something is listening. It does not
                # prove that Metro is serving this project. Reusing another project's server was
                # causing screenshots to keep showing an unrelated/stale app after source edits.
                new_port = self._find_available_port(configured_port + 1)
                self.url = self._url_with_port(configured_url, new_port)
                self.logger.warning(
                    "Existing Expo server could not be verified as this project; using an isolated port",
                    extra={"existing_url": configured_url, "url": self.url, "project_root": str(self.project_root)},
                )
        elif self._port_open("127.0.0.1", configured_port):
            # Don't abort just because another local service occupies the preferred port.
            # Start our own Metro server on a verified free port instead.
            new_port = self._find_available_port(configured_port + 1)
            self.url = self._url_with_port(configured_url, new_port)
            self.logger.warning(
                "Preferred Expo port is occupied by an unverified service; using an isolated port",
                extra={"existing_url": configured_url, "url": self.url},
            )
        server_port = self._port_from_url(self.url)
        command = list(project_config.get("expo_command", ["npx", "expo", "start", "--web", "--port", str(server_port), "--non-interactive"]))
        # `--non-interactive` is not a valid expo flag in recent versions; use CI=1 instead.
        command = [arg for arg in command if arg != "--non-interactive"]
        command = self._command_with_port(command, server_port)
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

    def restart(self) -> str:
        """Restart the Expo server so Metro re-bundles with the latest source changes.

        Metro caches the bundle in memory; without a restart, file patches applied by
        the coder are not reflected in the rendered output, causing functional checks
        to fail even though type-check passes. start() also replaces a verified
        project-owned server left behind by an earlier run, so the new process is
        never served from the old server's module cache.
        """
        self.stop()
        self.process = None
        self._log_handle = None
        self.owned = False
        # Do not kill foreign listeners on the configured port. start() only replaces
        # a listener whose working directory is this project; otherwise it selects a
        # verified free port.
        return self.start()

    def _stop_project_server(self, port: int) -> None:
        """Stop the verified Expo listener for this project so a fresh bundle is served.

        The process working directory is checked before signalling because another
        project or unrelated service may legitimately occupy the configured port.
        """
        expected = self.project_root.resolve()
        for attempt in range(2):
            pids = [pid for pid in self._listening_pids(port) if self._process_cwd(pid) == expected]
            if not pids:
                return
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    continue
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                if not self._port_open("127.0.0.1", port):
                    return
                time.sleep(0.15)
            # A wedged Metro process may ignore SIGTERM. Re-check ownership before
            # escalating so a recycled PID cannot target an unrelated process.
            pids = [pid for pid in self._listening_pids(port) if self._process_cwd(pid) == expected]
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    continue
            time.sleep(0.2)
        self.logger.warning(
            "Verified project Expo server is still listening after restart attempt",
            extra={"port": port, "project_root": str(self.project_root)},
        )

    def _kill_processes_on_port(self, port: int) -> None:
        """Kill any process listening on the given TCP port (best effort)."""
        try:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            for line in result.stdout.splitlines():
                try:
                    pid = int(line.strip())
                    os.kill(pid, 9)
                except (ValueError, ProcessLookupError, PermissionError, OSError):
                    continue
        except Exception:
            pass

    @staticmethod
    def _url_with_port(url: str, port: int) -> str:
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        netloc = f"{host}:{port}"
        return urlunparse(parsed._replace(netloc=netloc))

    @staticmethod
    def _command_with_port(command: list[str], port: int) -> list[str]:
        result = list(command)
        for index, item in enumerate(result):
            if item == "--port":
                if index + 1 < len(result):
                    result[index + 1] = str(port)
                else:
                    result.append(str(port))
                return result
            if item.startswith("--port="):
                result[index] = f"--port={port}"
                return result
        result.extend(["--port", str(port)])
        return result

    @staticmethod
    def _find_available_port(start_port: int, attempts: int = 50) -> int:
        for port in range(max(1024, start_port), min(65536, start_port + attempts)):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
        raise ExpoServerError(f"No free TCP port found in range {start_port}-{min(65535, start_port + attempts - 1)}")

    @staticmethod
    def _listening_pids(port: int) -> list[int]:
        """Best-effort PID lookup for the process serving a local TCP port."""
        try:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            if result.returncode != 0:
                return []
            pids: list[int] = []
            for line in result.stdout.splitlines():
                try:
                    pids.append(int(line.strip()))
                except ValueError:
                    continue
            return pids
        except (OSError, subprocess.SubprocessError):
            return []

    @staticmethod
    def _process_cwd(pid: int) -> Path | None:
        """Return a process working directory on Linux; None means identity is unknown."""
        try:
            return Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
        except (OSError, RuntimeError):
            return None

    def _server_belongs_to_project(self, port: int) -> bool:
        expected = self.project_root.resolve()
        pids = self._listening_pids(port)
        return bool(pids) and any(self._process_cwd(pid) == expected for pid in pids)

    @staticmethod
    def _port_from_url(url: str) -> int:
        parsed = urlparse(url)
        return parsed.port or (443 if parsed.scheme == "https" else 80)

    @staticmethod
    def _is_ready(url: str, timeout: float = 1.0) -> bool:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "FORGE-Harness/0.1"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return 200 <= response.status < 400
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _tail_log(self, lines: int = 35) -> str:
        try:
            content = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(content[-lines:])
        except OSError:
            return "(Expo log unavailable)"
