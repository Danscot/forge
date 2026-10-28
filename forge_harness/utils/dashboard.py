from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


class ChatMessage:
    """A single durable transcript message."""

    def __init__(
        self,
        kind: str,
        content: str,
        agent: str = "",
        timestamp: str = "",
        metadata: dict[str, Any] | None = None,
    ):
        self.kind = kind
        self.content = content
        self.agent = agent
        self.timestamp = timestamp
        self.metadata = metadata or {}


class Dashboard:
    """Append-only chat transcript with a compact live status line.

    The previous full-screen-style Rich Live layout redrew the entire transcript
    for every streaming token. It also stored each token fragment as a separate
    message and clipped history to the visible panel. This implementation writes
    complete messages to normal terminal output (so terminal scrollback works)
    and reserves Live for a single updating status line.
    """

    def __init__(self, run_id: str, mode: str, config: dict[str, Any], console: Console | None = None):
        self.run_id = run_id
        self.mode = mode
        self.config = config
        self.console = console or Console()
        self._lock = threading.RLock()
        self._live: Live | None = None
        self._started = False
        self._last_live_refresh = 0.0
        self.started_at = time.monotonic()
        self.project_name = "project"
        self.tasks: list[dict[str, Any]] = []
        self.task_index = 0
        self.task_title = "Initializing"
        self.active_agent = "orchestrator"
        self.provider = "-"
        self.model = "-"
        self.attempt = 0
        self.max_attempts = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.thinking_tokens = 0
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_thinking_tokens = 0
        self.usage_known = False
        self.context_window_tokens: int | None = None
        self.estimated_cost: float | None = None
        self.last_latency = 0.0
        self.retry_count = 0
        self.call_count = 0
        self.current_status = "Starting"
        # This is an in-memory index only; visible output is append-only and remains
        # in the user's terminal scrollback even when this bounded deque rolls over.
        self.messages: deque[ChatMessage] = deque(maxlen=1000)
        self._stream_buffers: dict[str, list[str]] = {}
        self._reasoning_buffers: dict[str, list[str]] = {}
        self._stream_chars: dict[str, int] = {}
        self.validation: dict[str, str] = {
            "TypeScript": "PENDING",
            "Runtime": "PENDING",
            "Functional": "PENDING",
            "Visual critic": "PENDING",
        }
        self.quality_score: float | None = None
        self.logger = logging.getLogger("forge.dashboard")

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        with self._lock:
            if self._live:
                return
            if not self._started:
                heading = Text()
                heading.append(" FORGE ", style="bold white on #5b2ca0")
                heading.append("Autonomous React Native Harness", style="bold")
                heading.append(f"   RUN {self.run_id}", style="dim")
                self.console.print(Panel(heading, subtitle=f"{self.project_name} · {self.mode} mode", border_style="#a56eff", box=box.ROUNDED))
                self.console.print(Text("Messages remain in terminal scrollback; use Shift+PgUp / your terminal's scroll wheel to review earlier output.", style="dim"))
                self._started = True
            self._live = Live(
                self._render(), console=self.console, refresh_per_second=4, screen=False, transient=False
            )
            self._live.start(refresh=True)

    def stop(self, final_status: str | None = None) -> None:
        with self._lock:
            if final_status:
                self.current_status = final_status
            self._push_system(f"Run {self.current_status.lower()}")
            if self._live:
                self._live.update(self._render(), refresh=True)
                self._live.stop()
                self._live = None
            elapsed = int(time.monotonic() - self.started_at)
            mm, ss = divmod(elapsed, 60)
            summary = Text()
            summary.append(f"Status: {self.current_status}\n", style="bold")
            summary.append(f"Elapsed: {mm:02d}:{ss:02d} · Requests: {self.call_count} · Retries: {self.retry_count}\n")
            if self.usage_known:
                summary.append(f"Tokens: {self.total_tokens:,} total ({self.prompt_tokens:,} input / {self.completion_tokens:,} output)\n")
            if self.quality_score is not None:
                summary.append(f"Visual quality: {self.quality_score:.1f}/100\n")
            gates = ", ".join(f"{name}: {status}" for name, status in self.validation.items())
            summary.append(f"Validation: {gates}")
            self.console.print(Panel(summary, title="FORGE run summary", border_style="#a56eff", box=box.ROUNDED))

    def pause(self) -> None:
        """Hide the live status line around a blocking approval prompt."""
        with self._lock:
            if self._live:
                self._live.stop()
                self._live = None

    def resume(self) -> None:
        """Restore the compact status line after a blocking approval prompt."""
        with self._lock:
            if not self._live:
                self._live = Live(self._render(), console=self.console, refresh_per_second=4, screen=False, transient=False)
                self._live.start(refresh=True)

    # ------------------------------------------------------------------ task / validation

    def set_project(self, name: str) -> None:
        with self._lock:
            self.project_name = name
            self._refresh(force=True)

    def set_tasks(self, tasks: list[dict[str, Any]]) -> None:
        with self._lock:
            self.tasks = tasks
            self._refresh(force=True)

    def set_task(self, index: int, title: str, agent: str | None = None) -> None:
        with self._lock:
            self.task_index = index
            self.task_title = title
            self.current_status = f"Working on {title}"
            self.logger.info("Task selected", extra={"task_index": index, "task_title": title, "agent": agent or self.active_agent})
            if agent:
                self.active_agent = agent
            self._push_system(f"Task {index + 1}/{max(1, len(self.tasks))}: {title}")
            self._refresh(force=True)

    def set_validation(self, name: str, status: str) -> None:
        with self._lock:
            self.validation[name] = status.upper()
            self.current_status = f"{name}: {status.upper()}"
            self.logger.info("Validation gate updated", extra={"gate": name, "status": status.upper()})
            style = "green" if status.upper() in {"PASS", "PASSED", "OK"} else "red" if status.upper() in {"FAIL", "FAILED", "ERROR"} else "yellow"
            self._push(ChatMessage("result", f"{name}: {status.upper()}", agent="validator", timestamp=time.strftime("%H:%M:%S"), metadata={"style": style}))
            self._refresh(force=True)

    def set_score(self, score: float | None) -> None:
        with self._lock:
            self.quality_score = score
            self._refresh(force=True)

    def activity_message(self, level: str, message: str) -> None:
        with self._lock:
            normalized = level.upper()
            self.current_status = message[:120]
            severity = logging.ERROR if normalized == "ERROR" else logging.WARNING if normalized in {"WARN", "WARNING"} else logging.INFO
            self.logger.log(severity, message, extra={"ui_level": normalized})
            style = "bold red" if normalized == "ERROR" else "yellow" if normalized in {"WARN", "WARNING"} else "green" if normalized in {"PASS", "SUCCESS"} else "white"
            self._push(ChatMessage("system", message, agent=self.active_agent, timestamp=time.strftime("%H:%M:%S"), metadata={"style": style, "level": normalized}))
            self._refresh(force=True)

    # ------------------------------------------------------------------ LLM events

    def on_llm_event(self, kind: str, payload: dict[str, Any]) -> None:
        with self._lock:
            agent = str(payload.get("agent", self.active_agent))
            if kind == "llm_request_start":
                self.active_agent = agent
                self.provider = str(payload.get("provider", self.provider))
                self.model = str(payload.get("model", self.model))
                self.attempt = int(payload.get("attempt", 1))
                self.max_attempts = int(payload.get("max_attempts", 1))
                try:
                    self.context_window_tokens = int(payload.get("context_window_tokens")) if payload.get("context_window_tokens") else None
                except (TypeError, ValueError):
                    self.context_window_tokens = None
                self.call_count += 1
                self.last_prompt_tokens = 0
                self.last_completion_tokens = 0
                self.last_thinking_tokens = 0
                self._stream_buffers[agent] = []
                self._reasoning_buffers[agent] = []
                self._stream_chars[agent] = 0
                self.current_status = f"{agent} is generating"
                self._push(ChatMessage("system", f"{agent} · {self.provider}:{self.model} · request {self.attempt}/{self.max_attempts}", agent=agent, timestamp=time.strftime("%H:%M:%S")))
            elif kind == "llm_retry":
                self.retry_count += 1
                self._stream_buffers[agent] = []
                self._reasoning_buffers[agent] = []
                content = f"{agent}: provider retry {payload.get('next_attempt')} after {payload.get('delay_s')}s — {payload.get('error', 'transient error')}"
                self._push(ChatMessage("error", content, agent=agent, timestamp=time.strftime("%H:%M:%S")))
                self.current_status = f"Retrying {agent} request"
            elif kind in ("llm_provider_failed", "provider_failed", "llm_candidate_exhausted"):
                content = f"{agent}: provider failed — {payload.get('error', 'unknown error')}"
                self._push(ChatMessage("error", content, agent=agent, timestamp=time.strftime("%H:%M:%S")))
                self.current_status = f"{agent} provider failed"
            elif kind == "llm_text_delta":
                chunk = str(payload.get("text", ""))
                if chunk:
                    self._stream_buffers.setdefault(agent, []).append(chunk)
                    self._stream_chars[agent] = self._stream_chars.get(agent, 0) + len(chunk)
                    self.last_completion_tokens += max(0, len(chunk) // 4)
                    self.current_status = f"{agent} streaming response · {self._stream_chars[agent]:,} chars received"
                    # Refresh the one-line status occasionally, not a full transcript per token.
                    self._refresh()
                    return
            elif kind == "llm_reasoning_delta":
                chunk = str(payload.get("text", ""))
                if chunk:
                    self._reasoning_buffers.setdefault(agent, []).append(chunk)
                    self.last_thinking_tokens += max(0, len(chunk) // 4)
                    self.thinking_tokens += max(0, len(chunk) // 4)
                    self.current_status = f"{agent} reasoning metadata · {sum(map(len, self._reasoning_buffers[agent])):,} chars"
                    self._refresh()
                    return
            elif kind == "llm_request_done":
                usage = payload.get("usage", {}) or {}
                if usage:
                    self.usage_known = True
                    self.prompt_tokens += int(usage.get("prompt_tokens", 0))
                    self.completion_tokens += int(usage.get("completion_tokens", 0))
                    self.total_tokens += int(usage.get("total_tokens", 0))
                    self.last_prompt_tokens = int(usage.get("prompt_tokens", 0))
                    self.last_completion_tokens = int(usage.get("completion_tokens", self.last_completion_tokens))
                    self.last_thinking_tokens = int(usage.get("thinking_tokens", self.last_thinking_tokens))
                    if "thinking_tokens" in usage:
                        self.thinking_tokens += int(usage["thinking_tokens"])
                self.last_latency = float(payload.get("duration_s", 0))
                price_key = f"{payload.get('provider', self.provider)}:{payload.get('model', self.model)}"
                price_cfg = self.config.get("costs", {}).get("models", {}).get(price_key) or self.config.get("costs", {}).get("models", {}).get(str(payload.get("model", self.model)))
                if price_cfg and usage:
                    input_price = float(price_cfg.get("input_per_million", 0))
                    output_price = float(price_cfg.get("output_per_million", 0))
                    self.estimated_cost = (self.estimated_cost or 0.0) + (
                        int(usage.get("prompt_tokens", 0)) * input_price + int(usage.get("completion_tokens", 0)) * output_price
                    ) / 1_000_000
                duration = float(payload.get("duration_s", 0))
                finish_reason = payload.get("finish_reason") or "unknown"
                usage_str = f"{usage.get('total_tokens', '?')} tokens"
                reasoning = "".join(self._reasoning_buffers.get(agent, []))
                response = "".join(self._stream_buffers.get(agent, []))
                self.current_status = f"{agent} response received · {duration:.1f}s"
                self._push(ChatMessage("result", f"Completed in {duration:.1f}s · {usage_str} · finish={finish_reason}", agent=agent, timestamp=time.strftime("%H:%M:%S")))
                if response.strip():
                    self._push(ChatMessage("assistant_output", response, agent=agent, timestamp=time.strftime("%H:%M:%S")))
                if reasoning.strip() and self.config.get("llm", {}).get("display_reasoning_stream", False):
                    self._push(ChatMessage("thinking", reasoning, agent=agent, timestamp=time.strftime("%H:%M:%S")))
            elif kind == "structured_output_repair":
                self._push(ChatMessage("system", f"Structured response repair {payload.get('attempt', '?')}: {payload.get('error', 'invalid output')}", agent=agent, timestamp=time.strftime("%H:%M:%S")))
            elif kind == "image_skipped":
                self._push_system(f"Image skipped: {payload.get('path')} ({payload.get('reason')})")
            elif kind == "parameter_not_mapped":
                self._push_system(f"Provider {payload.get('provider')} does not map {payload.get('parameter')}; parameter omitted")
            else:
                return
            self._refresh(force=True)

    # ------------------------------------------------------------------ shell / command visibility

    def on_command_start(self, command: list[str] | str, agent: str = "") -> None:
        cmd_text = " ".join(command) if isinstance(command, list) else str(command)
        with self._lock:
            self.current_status = f"Running: {cmd_text[:80]}"
            self._push(ChatMessage("command", f"$ {cmd_text}", agent=agent or self.active_agent, timestamp=time.strftime("%H:%M:%S")))
            self._refresh(force=True)

    def on_command_done(self, result: dict[str, Any], agent: str = "") -> None:
        exit_code = result.get("exit_code", "?")
        duration = result.get("duration_s", 0)
        command = result.get("command", [])
        cmd_text = " ".join(command) if isinstance(command, list) else str(command)
        timed_out = result.get("timed_out", False)
        status_str = "TIMEOUT" if timed_out else f"exit {exit_code}"
        ok = exit_code == 0 and not timed_out
        with self._lock:
            self.current_status = f"Command: {status_str}"
            self._push(ChatMessage("result", f"$ {cmd_text} → {status_str} ({duration:.1f}s)", agent=agent or self.active_agent, timestamp=time.strftime("%H:%M:%S"), metadata={"style": "green" if ok else "bold red"}))
            self._refresh(force=True)

    # ------------------------------------------------------------------ plan / patch visibility

    def on_plan(self, plan: dict[str, Any], agent: str = "") -> None:
        summary = plan.get("summary", "No summary")
        risk = str(plan.get("risk", "low")).upper()
        ops = plan.get("operations", [])
        lines = [summary, f"Risk: {risk} · Operations: {len(ops)}"]
        for op in ops[:10]:
            op_type = str(op.get("type", ""))
            op_path = str(op.get("path", ""))
            reason = str(op.get("reason") or op.get("target") or "")[:120]
            lines.append(f"  {op_type} → {op_path}  ({reason})")
        if len(ops) > 10:
            lines.append(f"  ... and {len(ops) - 10} more operation(s)")
        with self._lock:
            self._push(ChatMessage("plan", "\n".join(lines), agent=agent or self.active_agent, timestamp=time.strftime("%H:%M:%S")))
            self.current_status = f"Plan ready · {risk} risk · {len(ops)} operations"
            self._refresh(force=True)

    def on_patch_applied(self, apply_results: list[dict[str, Any]], agent: str = "") -> None:
        lines = [f"Applied {len(apply_results)} surgical file operation(s)"]
        for result in apply_results[:20]:
            lines.append(f"  {result.get('type', '?')} → {result.get('path', '?')}")
        with self._lock:
            self._push(ChatMessage("code", "\n".join(lines), agent=agent or self.active_agent, timestamp=time.strftime("%H:%M:%S")))
            self.current_status = f"Applied {len(apply_results)} file operation(s)"
            self._refresh(force=True)

    # ------------------------------------------------------------------ rendering / history

    def update_runtime_totals(self, runtime: Any) -> None:
        with self._lock:
            self.prompt_tokens = int(runtime.total_usage.get("prompt_tokens", self.prompt_tokens))
            self.completion_tokens = int(runtime.total_usage.get("completion_tokens", self.completion_tokens))
            self.total_tokens = int(runtime.total_usage.get("total_tokens", self.total_tokens))
            if any(runtime.total_usage.values()):
                self.usage_known = True
            self.call_count = max(self.call_count, int(runtime.call_count))
            self.retry_count = max(self.retry_count, int(runtime.retry_count))
            self._refresh(force=True)

    def _refresh(self, force: bool = False) -> None:
        if not self._live:
            return
        now = time.monotonic()
        if not force and now - self._last_live_refresh < 0.25:
            return
        self._last_live_refresh = now
        self._live.update(self._render(), refresh=True)

    def _push_system(self, message: str) -> None:
        self._push(ChatMessage("system", message, timestamp=time.strftime("%H:%M:%S")))

    def _push(self, message: ChatMessage) -> None:
        self.messages.append(message)
        stamp = message.timestamp or time.strftime("%H:%M:%S")
        label = {
            "thinking": "provider reasoning metadata",
            "code": "patch",
            "command": "command",
            "result": "result",
            "plan": "change plan",
            "error": "error",
            "assistant_output": f"{message.agent or 'agent'} response",
            "system": message.metadata.get("level", "event"),
        }.get(message.kind, message.kind)
        style = message.metadata.get("style")
        if style is None:
            style = {
                "thinking": "#bb9cff",
                "code": "green",
                "command": "yellow",
                "result": "cyan",
                "plan": "magenta",
                "error": "bold red",
                "assistant_output": "white",
                "system": "white",
            }.get(message.kind, "white")
        if message.kind in {"assistant_output", "thinking", "plan", "code"}:
            title = f"{message.agent or 'FORGE'} · {label} · {stamp}"
            self.console.print(Panel(Text(message.content, style=style, overflow="fold", no_wrap=False), title=title, border_style=style, box=box.ROUNDED, padding=(0, 1)))
        else:
            line = Text()
            line.append(f"[{stamp}] ", style="dim")
            line.append(f"{label.upper()} ", style=style)
            line.append(message.content, style=style if message.kind in {"error", "system"} else "white")
            self.console.print(line)

    def _render(self) -> Text:
        elapsed = int(time.monotonic() - self.started_at)
        mm, ss = divmod(elapsed, 60)
        task_count = max(1, len(self.tasks))
        status = self.current_status.replace("\n", " ")
        width = max(60, int(self.console.width or 100))
        model_label = f"{self.provider}:{self.model}"
        if len(model_label) > 28:
            model_label = model_label[:25] + "..."
        status_limit = max(16, min(58, width - 61))
        if len(status) > status_limit:
            status = status[: max(1, status_limit - 3)] + "..."
        line = Text()
        line.append(" FORGE ", style="bold white on #5b2ca0")
        line.append(f" {self.task_index + 1}/{task_count} ", style="bold cyan")
        line.append(f"{self.active_agent} ", style="bold")
        line.append(f"{model_label} ", style="dim")
        line.append(f"{mm:02d}:{ss:02d} ", style="dim")
        line.append("· ")
        line.append(status, style="yellow")
        return line
