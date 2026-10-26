from __future__ import annotations

import logging
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence


class ShellExecutionError(RuntimeError):
    pass


def run_shell(
    command: Sequence[str] | str,
    *,
    cwd: Path,
    timeout: int = 60,
    config: dict[str, Any] | None = None,
    logger: logging.Logger | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run an argument-vector command without a shell and under an executable allowlist."""
    cfg = config or {}
    security = cfg.get("security", {})
    logger = logger or logging.getLogger("forge.shell")
    args = shlex.split(command) if isinstance(command, str) else [str(part) for part in command]
    if not args:
        raise ShellExecutionError("Empty shell command")
    executable = Path(args[0]).name
    permitted = set(security.get("permitted_shell_executables", ["npx", "npm", "node", "python", "python3", "pytest", "git"]))
    if executable not in permitted:
        raise ShellExecutionError(f"Executable '{executable}' is not allowed. Configure security.permitted_shell_executables to permit it.")
    command_text = " ".join(args).lower()
    blocked = security.get("blocked_commands", ["rm -rf /", "sudo", "mkfs", "shutdown", "reboot"])
    if any(str(bad).lower() in command_text for bad in blocked):
        raise ShellExecutionError("Command rejected by security policy")
    started = time.monotonic()
    try:
        process = subprocess.run(
            args, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=max(1, int(timeout)),
            check=False, shell=False,
        )
        result = {
            "exit_code": process.returncode,
            "stdout": process.stdout[-60000:],
            "stderr": process.stderr[-60000:],
            "command": args,
            "duration_s": round(time.monotonic() - started, 3),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        result = {
            "exit_code": 124,
            "stdout": (exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or "")[-60000:],
            "stderr": (exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or "")[-60000:],
            "command": args,
            "duration_s": round(time.monotonic() - started, 3),
            "timed_out": True,
        }
    logger.log(logging.INFO if result["exit_code"] == 0 else logging.WARNING, "Shell command finished", extra={
        "command": args, "exit_code": result["exit_code"], "duration_s": result["duration_s"], "timed_out": result["timed_out"],
    })
    return result
