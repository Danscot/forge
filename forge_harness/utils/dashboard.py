from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text


class ChatMessage:
    """A single chat-style message bubble."""

    def __init__(
        self,
        kind: str,
        content: str,
        agent: str = "",
        timestamp: str = "",
        metadata: dict[str, Any] | None = None,
    ):
        self.kind = kind  # "thinking", "code", "command", "result", "system", "error", "plan"
        self.content = content
        self.agent = agent
        self.timestamp = timestamp
        self.metadata = metadata or {}


class Dashboard:
    """Claude-inspired live chat interface for FORGE agent activity.

    Shows thinking, code generation, shell commands, and validation results
    as distinct message bubbles in a scrollable transcript, with a metrics
    sidebar for tokens, quality, and validation status.
    """

    def __init__(self, run_id: str, mode: str, config: dict[str, Any], console: Console | None = None):
        self.run_id = run_id
        self.mode = mode
        self.config = config
        self.console = console or Console()
        self._lock = threading.RLock()
        self._live: Live | None = None
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
        self.messages: deque[ChatMessage] = deque(maxlen=200)
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
        if self._live:
            return
        self._push_system("FORGE execution started")
        self._live = Live(self._render(), console=self.console, refresh_per_second=12, screen=False, transient=False)
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

    def pause(self) -> None:
        """Temporarily stop the live display so a blocking prompt is fully visible."""
        with self._lock:
            if self._live:
                self._live.stop()
                self._live = None

    def resume(self) -> None:
        """Restart the live display after a blocking prompt."""
        if self._live:
            return
        self._live = Live(self._render(), console=self.console, refresh_per_second=12, screen=False, transient=False)
        self._live.start(refresh=True)

    # ------------------------------------------------------------------ task / validation

    def set_project(self, name: str) -> None:
        with self._lock:
            self.project_name = name
            self._refresh()

    def set_tasks(self, tasks: list[dict[str, Any]]) -> None:
        with self._lock:
            self.tasks = tasks
            self._refresh()

    def set_task(self, index: int, title: str, agent: str | None = None) -> None:
        with self._lock:
            self.task_index = index
            self.task_title = title
            self.current_status = f"Working on {title}"
            self.logger.info(
                "Task selected", extra={"task_index": index, "task_title": title, "agent": agent or self.active_agent}
            )
            if agent:
                self.active_agent = agent
            self._push_system(f"Task {index + 1}/{max(1, len(self.tasks))}: {title}")
            self._refresh()

    def set_validation(self, name: str, status: str) -> None:
        with self._lock:
            self.validation[name] = status.upper()
            self.current_status = f"{name}: {status.upper()}"
            self.logger.info("Validation gate updated", extra={"gate": name, "status": status.upper()})
            self._refresh()

    def set_score(self, score: float | None) -> None:
        with self._lock:
            self.quality_score = score
            self._refresh()

    def activity_message(self, level: str, message: str) -> None:
        with self._lock:
            normalized = level.upper()
            self.current_status = message[:120]
            severity = (
                logging.ERROR
                if normalized == "ERROR"
                else logging.WARNING
                if normalized == "WARN"
                else logging.INFO
            )
            self.logger.log(severity, message, extra={"ui_level": normalized})
            self._push_system(message)
            self._refresh()

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
                    self.context_window_tokens = (
                        int(payload.get("context_window_tokens")) if payload.get("context_window_tokens") else None
                    )
                except (TypeError, ValueError):
                    self.context_window_tokens = None
                self.call_count += 1
                if self.attempt > 1:
                    self.last_prompt_tokens = 0
                    self.last_completion_tokens = 0
                    self.last_thinking_tokens = 0
                self.current_status = f"{agent} is generating"
                self._push_system(
                    f"{agent} · {self.provider}:{self.model} · attempt {self.attempt}/{self.max_attempts}"
                )
                self._push(
                    ChatMessage(
                        kind="system",
                        content=f"{agent} · {self.provider}:{self.model} · attempt {self.attempt}/{self.max_attempts}",
                        agent=agent,
                    )
                )
            elif kind == "llm_retry":
                self.retry_count += 1
                self._push_system(
                    f"{agent}: retry {payload.get('next_attempt')} after {payload.get('delay_s')}s — {payload.get('error', 'transient error')}"
                )
                self._push(
                    ChatMessage(
                        kind="error",
                        content=f"{agent}: retry {payload.get('next_attempt')} after {payload.get('delay_s')}s — {payload.get('error', 'transient error')}",
                        agent=agent,
                    )
                )
            elif kind in ("llm_provider_failed", "provider_failed"):
                self._push_system(f"{agent}: provider candidate failed — {payload.get('error', 'unknown error')}")
                self._push(
                    ChatMessage(
                        kind="error",
                        content=f"{agent}: provider candidate failed — {payload.get('error', 'unknown error')}",
                        agent=agent,
                    )
                )
            elif kind == "llm_text_delta":
                chunk = str(payload.get("text", ""))
                if chunk:
                    self.last_completion_tokens += len(chunk) // 4  # rough estimate
                    self._push(
                        ChatMessage(kind="code", content=chunk, agent=agent, timestamp=time.strftime("%H:%M:%S"))
                    )
            elif kind == "llm_reasoning_delta":
                chunk = str(payload.get("text", ""))
                if chunk:
                    self.last_thinking_tokens += len(chunk) // 4  # rough estimate
                    self.thinking_tokens += len(chunk) // 4
                    self._push(
                        ChatMessage(
                            kind="thinking",
                            content=chunk,
                            agent=agent,
                            timestamp=time.strftime("%H:%M:%S"),
                        )
                    )
            elif kind == "llm_request_done":
                usage = payload.get("usage", {}) or {}
                if usage:
                    self.usage_known = True
                    self.prompt_tokens += int(usage.get("prompt_tokens", 0))
                    self.completion_tokens += int(usage.get("completion_tokens", 0))
                    self.total_tokens += int(usage.get("total_tokens", 0))
                    self.last_prompt_tokens = int(usage.get("prompt_tokens", 0))
                    self.last_completion_tokens = int(usage.get("completion_tokens", 0))
                    self.last_thinking_tokens = int(usage.get("thinking_tokens", self.last_thinking_tokens))
                    if "thinking_tokens" in usage:
                        self.thinking_tokens += int(usage["thinking_tokens"])
                self.last_latency = float(payload.get("duration_s", 0))
                price_key = f"{payload.get('provider', self.provider)}:{payload.get('model', self.model)}"
                price_cfg = (
                    self.config.get("costs", {}).get("models", {}).get(price_key)
                    or self.config.get("costs", {}).get("models", {}).get(str(payload.get("model", self.model)))
                )
                if price_cfg and usage:
                    input_price = float(price_cfg.get("input_per_million", 0))
                    output_price = float(price_cfg.get("output_per_million", 0))
                    self.estimated_cost = (
                        (self.estimated_cost or 0.0)
                        + (
                            int(usage.get("prompt_tokens", 0)) * input_price
                            + int(usage.get("completion_tokens", 0)) * output_price
                        )
                        / 1_000_000
                    )
                self.current_status = "LLM response received"
                finish_reason = payload.get("finish_reason")
                usage_str = f"{usage.get('total_tokens', '?')} tok"
                thinking_str = ""
                if self.last_thinking_tokens:
                    thinking_str = f" · thinking: {self.last_thinking_tokens} tok"
                self._push_system(
                    f"{agent} done · {payload.get('duration_s', 0):.1f}s · {usage_str}{thinking_str} · finish={finish_reason}"
                )
                self._push(
                    ChatMessage(
                        kind="result",
                        content=f"{agent} done · {payload.get('duration_s', 0):.1f}s · {usage_str}{thinking_str} · finish={finish_reason}",
                        agent=agent,
                    )
                )
            elif kind == "image_skipped":
                self._push_system(f"Image skipped: {payload.get('path')} ({payload.get('reason')})")
            elif kind == "parameter_not_mapped":
                self._push_system(f"Provider {payload.get('provider')} does not map {payload.get('parameter')}; parameter omitted")
            self._refresh()

    # ------------------------------------------------------------------ shell / command visibility

    def on_command_start(self, command: list[str] | str, agent: str = "") -> None:
        """Log a shell command starting to run."""
        cmd_text = " ".join(command) if isinstance(command, list) else str(command)
        with self._lock:
            self.current_status = f"Running: {cmd_text[:80]}"
            self._push_system(f"$ {cmd_text}")
            self._push(
                ChatMessage(
                    kind="command",
                    content=f"$ {cmd_text}",
                    agent=agent or self.active_agent,
                    timestamp=time.strftime("%H:%M:%S"),
                )
            )
            self._refresh()

    def on_command_done(self, result: dict[str, Any], agent: str = "") -> None:
        """Log a shell command completion."""
        exit_code = result.get("exit_code", "?")
        duration = result.get("duration_s", 0)
        command = result.get("command", [])
        cmd_text = " ".join(command) if isinstance(command, list) else str(command)
        timed_out = result.get("timed_out", False)
        status_str = "TIMEOUT" if timed_out else f"exit {exit_code}"
        color = "green" if exit_code == 0 and not timed_out else "red"
        with self._lock:
            self.current_status = f"Command: {status_str}"
            self._push_system(f"$ {cmd_text} → {status_str} ({duration:.1f}s)")
            self._push(
                ChatMessage(
                    kind="result",
                    content=f"$ {cmd_text} → [{color}]{status_str}[/{color}] ({duration:.1f}s)",
                    agent=agent or self.active_agent,
                    timestamp=time.strftime("%H:%M:%S"),
                )
            )
            self._refresh()

    # ------------------------------------------------------------------ plan / patch visibility

    def on_plan(self, plan: dict[str, Any], agent: str = "") -> None:
        """Display an approved change plan."""
        summary = plan.get("summary", "No summary")
        risk = str(plan.get("risk", "low")).upper()
        ops = plan.get("operations", [])
        lines = [summary, f"Risk: {risk} · Operations: {len(ops)}"]
        for op in ops[:10]:
            op_type = str(op.get("type", ""))
            op_path = str(op.get("path", ""))
            reason = str(op.get("reason") or op.get("target") or "")[:80]
            lines.append(f"  {op_type} → {op_path}  ({reason})")
        if len(ops) > 10:
            lines.append(f"  ... and {len(ops) - 10} more operation(s)")
        content = "\n".join(lines)
        with self._lock:
            self._push_system(f"Change plan ({risk} risk): {summary}")
            self._push(
                ChatMessage(kind="plan", content=content, agent=agent or self.active_agent, timestamp=time.strftime("%H:%M:%S"))
            )
            self._refresh()

    def on_patch_applied(self, apply_results: list[dict[str, Any]], agent: str = "") -> None:
        """Display patch application results."""
        lines = [f"Applied {len(apply_results)} surgical file operation(s)"]
        for r in apply_results[:10]:
            op_type = r.get("type", "?")
            path = r.get("path", "?")
            lines.append(f"  {op_type} → {path}")
        content = "\n".join(lines)
        with self._lock:
            self._push_system(f"Applied {len(apply_results)} surgical file operation(s)")
            self._push(
                ChatMessage(kind="code", content=content, agent=agent or self.active_agent, timestamp=time.strftime("%H:%M:%S"))
            )
            self._refresh()

    # ------------------------------------------------------------------ rendering

    def update_runtime_totals(self, runtime: Any) -> None:
        with self._lock:
            self.prompt_tokens = int(runtime.total_usage.get("prompt_tokens", self.prompt_tokens))
            self.completion_tokens = int(runtime.total_usage.get("completion_tokens", self.completion_tokens))
            self.total_tokens = int(runtime.total_usage.get("total_tokens", self.total_tokens))
            if any(runtime.total_usage.values()):
                self.usage_known = True
            self.call_count = max(self.call_count, int(runtime.call_count))
            self.retry_count = max(self.retry_count, int(runtime.retry_count))
            self._refresh()

    def _refresh(self) -> None:
        if self._live:
            self._live.update(self._render(), refresh=True)

    def _push_system(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.messages.append(ChatMessage(kind="system", content=f"[{stamp}] {message}", timestamp=stamp))

    def _push(self, message: ChatMessage) -> None:
        self.messages.append(message)

    def _render(self) -> Layout:
        elapsed = int(time.monotonic() - self.started_at)
        mm, ss = divmod(elapsed, 60)

        layout = Layout()
        layout.split_column(Layout(name="header", size=3), Layout(name="main"), Layout(name="footer", size=3))
        layout["main"].split_row(Layout(name="work", ratio=3), Layout(name="sidebar", ratio=1, minimum_size=34))

        # Header
        title = Text()
        title.append(" FORGE ", style="bold white on #5b2ca0")
        title.append("  Autonomous React Native Harness", style="bold")
        title.append(f"  RUN {self.run_id}", style="dim")
        layout["header"].update(Panel(title, border_style="#a56eff", box=box.ROUNDED))

        # Main chat transcript
        chat = Text()
        for msg in list(self.messages):
            if msg.kind == "thinking":
                chat.append(f"[{msg.timestamp}] ", style="dim")
                chat.append("💭 ", style="#bb9cff")
                chat.append(f"{msg.agent} thinking: ", style="italic #bb9cff")
                chat.append(msg.content + "\n", style="#bb9cff")
            elif msg.kind == "code":
                chat.append(f"[{msg.timestamp}] ", style="dim")
                chat.append("🔨 ", style="green")
                chat.append(f"{msg.agent} code: ", style="bold green")
                chat.append(msg.content + "\n", style="white")
            elif msg.kind == "command":
                chat.append(f"[{msg.timestamp}] ", style="dim")
                chat.append("⚡ ", style="yellow")
                chat.append(msg.content + "\n", style="bold yellow")
            elif msg.kind == "result":
                chat.append(f"[{msg.timestamp}] ", style="dim")
                chat.append("✓ ", style="cyan")
                chat.append(msg.content + "\n", style="cyan")
            elif msg.kind == "plan":
                chat.append(f"[{msg.timestamp}] ", style="dim")
                chat.append("📋 ", style="magenta")
                chat.append(f"{msg.agent} plan:\n", style="bold magenta")
                chat.append(msg.content + "\n", style="magenta")
            elif msg.kind == "error":
                chat.append(f"[{msg.timestamp}] ", style="dim")
                chat.append("✗ ", style="bold red")
                chat.append(msg.content + "\n", style="bold red")
            else:  # system
                chat.append(msg.content + "\n", style="dim white")

        if not self.messages:
            chat.append("Waiting for agent activity…", style="dim")

        layout["work"].update(
            Panel(
                Align.left(chat, vertical="top"),
                title=f"Transcript · {self.task_title}",
                border_style="cyan",
                box=box.ROUNDED,
            )
        )

        # Sidebar metrics
        sidebar = Table.grid(expand=True)
        sidebar.add_column()

        sidebar.add_row(Text("MODEL", style="bold cyan"))
        sidebar.add_row(Text(f"{self.provider}", style="white"))
        sidebar.add_row(Text(f"{self.model}", style="white"))

        sidebar.add_row(Text("\nEXECUTION", style="bold cyan"))
        sidebar.add_row(Text(f"Mode: {self.mode}", style="white"))
        sidebar.add_row(Text(f"Agent: {self.active_agent}", style="white"))
        sidebar.add_row(Text(f"Status: {self.current_status}", style="white"))
        sidebar.add_row(Text(f"Attempt: {self.attempt}/{self.max_attempts}", style="white"))
        sidebar.add_row(Text(f"Elapsed: {mm:02d}:{ss:02d}", style="white"))

        sidebar.add_row(Text("\nTOKENS", style="bold cyan"))
        if self.usage_known:
            sidebar.add_row(Text(f"Prompt:   {self.prompt_tokens:,}", style="white"))
            sidebar.add_row(Text(f"Completion: {self.completion_tokens:,}", style="white"))
            sidebar.add_row(Text(f"Thinking:   {self.thinking_tokens:,}", style="#bb9cff"))
            sidebar.add_row(Text(f"Total:      {self.total_tokens:,}", style="bold white"))
            sidebar.add_row(Text(f"Last input: {self.last_prompt_tokens:,}", style="white"))
            sidebar.add_row(Text(f"Last output: {self.last_completion_tokens:,}", style="white"))
            sidebar.add_row(Text(f"Last thinking: {self.last_thinking_tokens:,}", style="#bb9cff"))
        else:
            sidebar.add_row(Text("Provider usage not reported", style="dim"))
        if self.context_window_tokens:
            pct = (self.last_prompt_tokens / self.context_window_tokens * 100) if self.usage_known else 0
            sidebar.add_row(Text(f"Context: {self.last_prompt_tokens:,}/{self.context_window_tokens:,} ({pct:.1f}%)", style="white"))
        else:
            sidebar.add_row(Text("Context window not configured", style="dim"))

        sidebar.add_row(Text("\nREQUESTS", style="bold cyan"))
        cost_text = f"${self.estimated_cost:.5f} est." if self.estimated_cost is not None else "n/a"
        sidebar.add_row(Text(f"Calls: {self.call_count}", style="white"))
        sidebar.add_row(Text(f"Retries: {self.retry_count}", style="white"))
        sidebar.add_row(Text(f"Latency: {self.last_latency:.1f}s", style="white"))
        sidebar.add_row(Text(f"Cost: {cost_text}", style="white"))

        sidebar.add_row(Text("\nVALIDATION", style="bold cyan"))
        status_table = Table.grid(padding=(0, 1))
        for key, value in self.validation.items():
            style = "green" if value in {"PASS", "PASSED", "OK"} else "red" if value in {"FAIL", "FAILED", "ERROR"} else "yellow"
            status_table.add_row(Text(key, style="white"), Text(value, style=f"bold {style}"))
        sidebar.add_row(status_table)
        if self.quality_score is not None:
            sidebar.add_row(Text(f"Quality: {self.quality_score:.1f}/100", style="bold"))

        layout["sidebar"].update(Panel(sidebar, title=self.project_name, border_style="#a56eff"))

        # Footer progress
        progress = Text()
        progress.append(f" {self.task_index + 1}/{max(1, len(self.tasks))} tasks  ", style="bold cyan")
        progress.append("· ")
        progress.append(self.task_title, style="white")
        progress.append("  ·  ")
        progress.append(self.current_status, style="yellow")
        progress.append("  ·  ctrl+c to interrupt safely", style="dim")
        layout["footer"].update(Panel(progress, border_style="#a56eff", box=box.ROUNDED))

        return layout
