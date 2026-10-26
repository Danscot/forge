from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text


class Dashboard:
    """OpenCode-inspired live TUI for agent activity, reasoning streams, model, and usage."""

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
        self.last_prompt_tokens = 0
        self.usage_known = False
        self.context_window_tokens: int | None = None
        self.estimated_cost: float | None = None
        self.last_latency = 0.0
        self.retry_count = 0
        self.call_count = 0
        self.current_status = "Starting"
        self.activity: deque[tuple[str, str]] = deque(maxlen=60)
        self.output_lines: deque[str] = deque(maxlen=60)
        self.thinking_lines: deque[str] = deque(maxlen=40)
        self.validation: dict[str, str] = {"TypeScript": "PENDING", "Runtime": "PENDING", "Functional": "PENDING", "Visual critic": "PENDING"}
        self.quality_score: float | None = None
        self.current_output = ""
        self.current_thinking = ""
        self.logger = logging.getLogger("forge.dashboard")

    def start(self) -> None:
        if self._live:
            return
        self._push("INFO", "FORGE execution started")
        self._live = Live(self._render(), console=self.console, refresh_per_second=12, screen=False, transient=False)
        self._live.start(refresh=True)

    def stop(self, final_status: str | None = None) -> None:
        with self._lock:
            if final_status:
                self.current_status = final_status
            self._push("INFO", f"Run {self.current_status.lower()}")
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
            self.logger.info("Task selected", extra={"task_index": index, "task_title": title, "agent": agent or self.active_agent})
            if agent:
                self.active_agent = agent
            self.current_output = ""
            self.current_thinking = ""
            self._push("TASK", f"Task {index + 1}/{max(1, len(self.tasks))}: {title}")
            self._refresh()

    def set_validation(self, name: str, status: str) -> None:
        with self._lock:
            self.validation[name] = status.upper()
            self.current_status = f"{name}: {status.upper()}"
            self.logger.info("Validation gate updated", extra={"gate": name, "status": status.upper()})
            self._push("CHECK", f"{name}: {status.upper()}")
            self._refresh()

    def set_score(self, score: float | None) -> None:
        with self._lock:
            self.quality_score = score
            self._refresh()

    def activity_message(self, level: str, message: str) -> None:
        with self._lock:
            normalized = level.upper()
            self._push(normalized, message)
            self.current_status = message[:120]
            severity = logging.ERROR if normalized == "ERROR" else logging.WARNING if normalized == "WARN" else logging.INFO
            self.logger.log(severity, message, extra={"ui_level": normalized})
            self._refresh()

    def _push(self, level: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.activity.append((level, f"[{stamp}] {message}"))

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
                if self.attempt > 1:
                    self.current_output = ""
                    self.current_thinking = ""
                    self.output_lines.clear()
                    self.thinking_lines.clear()
                self.current_status = f"{agent} is generating"
                self._push("LLM", f"{agent}: {self.provider}:{self.model} · attempt {self.attempt}/{self.max_attempts}")
                self.console.print(f"\n[bold cyan]▶ {agent}[/bold cyan] · {self.provider}:{self.model} · attempt {self.attempt}/{self.max_attempts}")
            elif kind == "llm_retry":
                self.retry_count += 1
                self._push("WARN", f"{agent}: retry {payload.get('next_attempt')} after {payload.get('delay_s')}s — {payload.get('error', 'transient error')}")
            elif kind == "llm_provider_failed" or kind == "provider_failed":
                self._push("ERROR", f"{agent}: provider candidate failed — {payload.get('error', 'unknown error')}")
            elif kind == "llm_text_delta":
                chunk = str(payload.get("text", ""))
                if chunk:
                    self.current_output += chunk
                    if len(self.current_output) > 12000:
                        self.current_output = self.current_output[-12000:]
                    self.output_lines = deque(self.current_output.splitlines()[-60:] or [self.current_output[-1200:]], maxlen=60)
                    # Stream generated output into terminal scrollback so it remains inspectable.
                    self.console.print(chunk, end="")
            elif kind == "llm_reasoning_delta":
                chunk = str(payload.get("text", ""))
                if chunk:
                    self.current_thinking += chunk
                    if len(self.current_thinking) > 8000:
                        self.current_thinking = self.current_thinking[-8000:]
                    self.thinking_lines = deque(self.current_thinking.splitlines()[-40:] or [self.current_thinking[-800:]], maxlen=40)
                    # Stream reasoning into scrollback (dimmed) so thoughts are inspectable.
                    self.console.print(f"[dim]{chunk}[/dim]", end="")
            elif kind == "llm_request_done":
                usage = payload.get("usage", {}) or {}
                if usage:
                    self.usage_known = True
                self.prompt_tokens += int(usage.get("prompt_tokens", 0))
                self.completion_tokens += int(usage.get("completion_tokens", 0))
                self.total_tokens += int(usage.get("total_tokens", 0))
                if "prompt_tokens" in usage:
                    self.last_prompt_tokens = int(usage.get("prompt_tokens", 0))
                self.last_latency = float(payload.get("duration_s", 0))
                price_key = f"{payload.get('provider', self.provider)}:{payload.get('model', self.model)}"
                price_cfg = self.config.get("costs", {}).get("models", {}).get(price_key) or self.config.get("costs", {}).get("models", {}).get(str(payload.get("model", self.model)))
                if price_cfg and usage:
                    input_price = float(price_cfg.get("input_per_million", 0))
                    output_price = float(price_cfg.get("output_per_million", 0))
                    self.estimated_cost = (self.estimated_cost or 0.0) + (int(usage.get("prompt_tokens", 0)) * input_price + int(usage.get("completion_tokens", 0)) * output_price) / 1_000_000
                self.current_status = "LLM response received"
                # Emit a scrollable summary line once the full response is available.
                finish_reason = payload.get("finish_reason")
                usage_str = f"{usage.get('total_tokens', '?')} tok" if usage else "tok n/a"
                self.console.print(
                    f"\n[bold green]✓ {agent} done[/bold green] · {payload.get('duration_s', 0):.1f}s · {usage_str} · finish={finish_reason}\n"
                )
                self._push("LLM", f"{agent}: completed in {self.last_latency:.1f}s · {usage.get('total_tokens', 'usage n/a')} tokens")
            elif kind == "image_skipped":
                self._push("WARN", f"Image skipped: {payload.get('path')} ({payload.get('reason')})")
            elif kind == "parameter_not_mapped":
                self._push("WARN", f"Provider {payload.get('provider')} does not map {payload.get('parameter')}; parameter omitted")
            self._refresh()

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

    def _render(self) -> Layout:
        elapsed = int(time.monotonic() - self.started_at)
        mm, ss = divmod(elapsed, 60)
        layout = Layout()
        layout.split_column(Layout(name="header", size=3), Layout(name="main"), Layout(name="footer", size=3))
        layout["main"].split_row(Layout(name="work", ratio=4), Layout(name="sidebar", ratio=1, minimum_size=30))
        layout["work"].split_column(Layout(name="activity", ratio=3), Layout(name="thinking", ratio=1, minimum_size=6), Layout(name="output", ratio=1, minimum_size=6))

        title = Text()
        title.append(" FORGE ", style="bold white on #5b2ca0")
        title.append("  Autonomous React Native Harness", style="bold")
        title.append(f"                                     RUN {self.run_id}", style="dim")
        layout["header"].update(Panel(title, border_style="#a56eff", box=box.ROUNDED))

        activity_text = Text()
        for level, message in list(self.activity):
            style = {"ERROR": "bold red", "WARN": "yellow", "CHECK": "cyan", "TASK": "bold magenta", "LLM": "green", "INFO": "dim"}.get(level, "white")
            activity_text.append(message + "\n", style=style)
        if not self.activity:
            activity_text.append("Waiting for agent activity…", style="dim")
        layout["activity"].update(Panel(activity_text, title=f"Activity · {self.task_title}", border_style="cyan"))

        reasoning = Text()
        if self.thinking_lines:
            reasoning.append("\n".join(self.thinking_lines), style="italic #bb9cff")
        else:
            reasoning.append("No provider reasoning stream received yet. Provider/model must expose it and the profile must enable it.", style="dim")
        layout["thinking"].update(Panel(reasoning, title="Thinking / reasoning stream", border_style="#7851a9"))

        output = Text()
        if self.output_lines:
            output.append("\n".join(self.output_lines), style="white")
        else:
            output.append("Streaming output will appear here…", style="dim")
        layout["output"].update(Panel(output, title="Current model output", border_style="green"))

        sidebar = Table.grid(expand=True)
        sidebar.add_column()
        sidebar.add_row(Text("MODEL", style="bold cyan"))
        sidebar.add_row(Text(f"{self.provider}\n{self.model}", style="white"))
        sidebar.add_row(Text("\nEXECUTION", style="bold cyan"))
        sidebar.add_row(Text(f"Mode: {self.mode}\nAgent: {self.active_agent}\nStatus: {self.current_status}\nElapsed: {mm:02d}:{ss:02d}", style="white"))
        sidebar.add_row(Text("\nTOKEN CONSUMPTION", style="bold cyan"))
        if self.usage_known:
            usage_text = f"Input:      {self.prompt_tokens:,}\nOutput:     {self.completion_tokens:,}\nTotal:      {self.total_tokens:,}\nLast input: {self.last_prompt_tokens:,}"
        else:
            usage_text = "Input:      n/a\nOutput:     n/a\nTotal:      n/a\nProvider usage not reported"
        if self.context_window_tokens:
            pct = (self.last_prompt_tokens / self.context_window_tokens * 100) if self.usage_known else 0
            usage_text += f"\nContext:    {self.last_prompt_tokens:,}/{self.context_window_tokens:,} ({pct:.1f}%)"
        else:
            usage_text += "\nContext:    window not configured"
        sidebar.add_row(Text(usage_text, style="white"))
        sidebar.add_row(Text("\nREQUESTS", style="bold cyan"))
        cost_text = f"${self.estimated_cost:.5f} est." if self.estimated_cost is not None else "n/a (price not configured)"
        sidebar.add_row(Text(f"Calls: {self.call_count}\nRetries: {self.retry_count}\nLast latency: {self.last_latency:.1f}s\nCost: {cost_text}", style="white"))
        sidebar.add_row(Text("\nVALIDATION", style="bold cyan"))
        status_table = Table.grid(padding=(0, 1))
        for key, value in self.validation.items():
            style = "green" if value in {"PASS", "PASSED", "OK"} else "red" if value in {"FAIL", "FAILED", "ERROR"} else "yellow"
            status_table.add_row(Text(key, style="white"), Text(value, style=f"bold {style}"))
        if self.quality_score is not None:
            status_table.add_row(Text("Quality", style="white"), Text(f"{self.quality_score:.1f}/100", style="bold"))
        sidebar.add_row(status_table)
        layout["sidebar"].update(Panel(sidebar, title=self.project_name, border_style="#a56eff"))

        progress = Text()
        progress.append(f" {self.task_index + 1}/{max(1, len(self.tasks))} tasks  ", style="bold cyan")
        progress.append("· ")
        progress.append(self.task_title, style="white")
        progress.append("  ·  ")
        progress.append(self.current_status, style="yellow")
        progress.append("  ·  ctrl+c to interrupt safely", style="dim")
        layout["footer"].update(Panel(progress, border_style="#a56eff", box=box.ROUNDED))
        return layout
