from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer
import yaml
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm

from .config import ConfigurationError, initialize_project, load_config
from .core.runner import HarnessRunError, HarnessRunner
from .core.store import StateStore

app = typer.Typer(
    name="forge",
    help="FORGE · Autonomous React Native / Expo engineering harness",
    no_args_is_help=False,
    add_completion=True,
    invoke_without_command=True,
)
config_app = typer.Typer(help="Inspect and modify runtime configuration", no_args_is_help=True)
app.add_typer(config_app, name="config")
console = Console()


def _project_root(value: Path) -> Path:
    return value.expanduser().resolve()


def _load(project_root: Path, config_path: Optional[Path] = None) -> dict:
    root = _project_root(project_root)
    try:
        from dotenv import load_dotenv
        load_dotenv(root / ".env", override=False)
    except ImportError:
        pass
    return load_config(root, config_path)


def _print_result(result: dict) -> None:
    status = result.get("status", "unknown")
    style = "green" if status == "completed" else "yellow" if status in {"blocked", "cancelled"} else "red"
    console.print(Panel.fit(
        f"[bold {style}]Status: {str(status).upper()}[/bold {style}]\nRun ID: {result.get('run_id', '—')}\nPassed tasks: {result.get('passed_tasks', 0)}/{result.get('total_tasks', len(result.get('tasks', [])))}\nLogs: {result.get('log_file', 'see .harness/logs')}",
        title="FORGE run summary", border_style=style,
    ))


@app.callback()
def main(ctx: typer.Context) -> None:
    """Launch the interactive coding session when no subcommand is supplied."""
    if ctx.invoked_subcommand is None:
        repl(Path.cwd())


@app.command("init")
def init_command(
    project_root: Path = typer.Option(Path("."), "--project-root", "-p", help="Existing Expo project root"),
    overwrite_config: bool = typer.Option(False, "--overwrite-config", help="Replace config.yaml and default prompts"),
) -> None:
    """Initialize FORGE configuration and input folders inside a project."""
    try:
        harness = initialize_project(_project_root(project_root), overwrite_config=overwrite_config)
    except ConfigurationError as exc:
        console.print(f"[red]Initialization failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(Panel.fit(
        f"FORGE initialized at [cyan]{harness}[/cyan]\n\nEdit [bold].harness/config.yaml[/bold] and place mockups in [bold]input/mockups/[/bold].\nSet API keys in your shell or an ignored [bold].env[/bold] file.",
        title="Ready to configure", border_style="green",
    ))


@app.command("new")
def new_project(
    name: str = typer.Argument(..., help="New Expo app directory name"),
    directory: Optional[Path] = typer.Option(None, "--directory", "-d", help="Destination path; defaults to NAME"),
    overwrite_config: bool = typer.Option(False, "--overwrite-config", help="Replace generated FORGE config if already present"),
) -> None:
    """Create a new Expo + TypeScript app, then initialize FORGE inside it."""
    target = _project_root(directory or Path(name))
    if target.exists() and any(target.iterdir()):
        console.print(f"[red]Target directory is not empty:[/red] {target}")
        raise typer.Exit(2)
    target.parent.mkdir(parents=True, exist_ok=True)
    relative_target = os.path.relpath(target, Path.cwd())
    cmd = ["npx", "create-expo-app@latest", relative_target, "--template", "blank-typescript", "--yes"]
    console.print(Panel.fit(f"Creating Expo app\n[cyan]{' '.join(cmd)}[/cyan]", title="FORGE bootstrap"))
    try:
        process = subprocess.run(cmd, check=False, shell=False)
    except OSError as exc:
        console.print(f"[red]Could not launch npx:[/red] {exc}")
        raise typer.Exit(1) from exc
    if process.returncode != 0:
        console.print(f"[red]create-expo-app failed with exit code {process.returncode}[/red]")
        raise typer.Exit(process.returncode)
    try:
        initialize_project(target, overwrite_config=overwrite_config)
    except ConfigurationError as exc:
        console.print(f"[yellow]Expo app created, but FORGE initialization failed:[/yellow] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[green]Created {target}[/green]")
    console.print(f"Next: [bold]cd {target} && forge run[/bold]")


@app.command("run")
def run_command(
    spec: Optional[str] = typer.Option(None, "--spec", help="Application specification text or path to a text file"),
    spec_file: Optional[Path] = typer.Option(None, "--spec-file", help="Read application specification from a file"),
    project_root: Path = typer.Option(Path("."), "--project-root", "-p", help="Target Expo project root"),
    mode: Optional[str] = typer.Option(None, "--mode", help="autonomous, approval, or interactive"),
    config_path: Optional[Path] = typer.Option(None, "--config", help="Alternate YAML configuration path"),
) -> None:
    """Plan, implement, render, inspect, and repair the target application."""
    root = _project_root(project_root)
    try:
        cfg = _load(root, config_path)
        content = None
        if spec_file:
            resolved_spec_file = spec_file.expanduser()
            if not resolved_spec_file.is_absolute():
                resolved_spec_file = root / resolved_spec_file
            content = resolved_spec_file.read_text(encoding="utf-8")
        elif spec:
            possible = Path(spec).expanduser()
            if not possible.is_absolute():
                possible = root / possible
            content = possible.read_text(encoding="utf-8") if possible.is_file() else spec
        result = HarnessRunner(root, cfg, mode=mode, console=console).execute(spec=content)
        if result.get("status") != "completed":
            raise typer.Exit(2)
    except (ConfigurationError, HarnessRunError, OSError, yaml.YAMLError) as exc:
        console.print(f"[red]FORGE could not start:[/red] {exc}")
        raise typer.Exit(1) from exc


@app.command("resume")
def resume_command(
    run_id: str = typer.Argument(..., help="Stored FORGE run ID"),
    project_root: Path = typer.Option(Path("."), "--project-root", "-p", help="Target project root"),
    mode: Optional[str] = typer.Option(None, "--mode", help="Override execution mode for the resumed run"),
    config_path: Optional[Path] = typer.Option(None, "--config", help="Alternate YAML configuration path"),
) -> None:
    """Resume an interrupted or blocked run from the persisted task queue."""
    root = _project_root(project_root)
    try:
        cfg = _load(root, config_path)
        result = HarnessRunner(root, cfg, mode=mode, console=console).execute(resume_id=run_id)
        if result.get("status") != "completed":
            raise typer.Exit(2)
    except (ConfigurationError, HarnessRunError, OSError) as exc:
        console.print(f"[red]Resume failed:[/red] {exc}")
        raise typer.Exit(1) from exc


@app.command("inspect")
def inspect_command(project_root: Path = typer.Option(Path("."), "--project-root", "-p", help="Project root")) -> None:
    """Inspect project setup, registry, and recent run history."""
    root = _project_root(project_root)
    config_path = root / ".harness" / "config.yaml"
    console.print(Panel.fit(f"[bold]{root.name}[/bold]\n{root}", title="Project"))
    if not config_path.is_file():
        console.print("[yellow]FORGE is not initialized. Run `forge init`.[/yellow]")
        raise typer.Exit(1)
    try:
        cfg = _load(root)
        console.print(f"Configuration: [green]OK[/green] · {config_path}")
        console.print(f"Providers configured: {', '.join(cfg.get('llm', {}).get('providers', {}).keys())}")
        console.print(f"Agent profiles: {', '.join(cfg.get('llm', {}).get('agents', {}).keys())}")
    except ConfigurationError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
    registry_path = root / ".harness" / "components.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        components = registry.get("components", {})
        console.print(f"Registry components: [cyan]{len(components)}[/cyan]")
        table = Table(title="Reusable component registry", expand=True)
        table.add_column("Name", style="cyan")
        table.add_column("Path")
        table.add_column("Purpose")
        for name, item in list(components.items())[:30]:
            table.add_row(name, str(item.get("path", "")), str(item.get("purpose", ""))[:100])
        console.print(table)
    except Exception:
        console.print("[yellow]Component registry is missing or invalid.[/yellow]")
    db = root / ".harness" / "state.db"
    if db.exists():
        store = StateStore(db)
        runs = store.list_runs(10)
        table = Table(title="Recent runs", expand=True)
        table.add_column("Run ID", style="cyan")
        table.add_column("Status")
        table.add_column("Mode")
        table.add_column("Current task")
        table.add_column("Updated")
        for run in runs:
            status_style = "green" if run["status"] == "completed" else "yellow" if run["status"] in {"blocked", "cancelled"} else "white"
            table.add_row(run["run_id"], f"[{status_style}]{run['status']}[/{status_style}]", run["mode"], str(run["current_index"]), run["updated_at"][:19])
        console.print(table)


@app.command("logs")
def logs_command(
    project_root: Path = typer.Option(Path("."), "--project-root", "-p", help="Project root"),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Show logs for a specific run"),
    tail: int = typer.Option(80, "--tail", min=1, max=2000, help="Number of log lines to display"),
    raw: bool = typer.Option(False, "--raw", help="Print raw JSONL rather than a summarized view"),
) -> None:
    """Read structured logs for diagnosing provider, patch, or validation failures."""
    root = _project_root(project_root)
    candidate_paths = []
    if run_id:
        candidate_paths.append(root / ".harness" / "runs" / run_id / "logs" / "events.jsonl")
    else:
        run_root = root / ".harness" / "runs"
        if run_root.exists():
            candidate_paths = sorted(run_root.glob("*/logs/events.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        candidate_paths.append(root / ".harness" / "logs" / "events.jsonl")
    path = next((p for p in candidate_paths if p.is_file()), None)
    if not path:
        console.print("[yellow]No event logs found. Start a run first.[/yellow]")
        raise typer.Exit(1)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
    if raw:
        console.print("\n".join(lines))
        return
    for line in lines:
        try:
            event = json.loads(line)
            stamp = str(event.get("timestamp", ""))[11:19]
            level = event.get("level", "INFO")
            message = event.get("message", "")
            extra = {k: v for k, v in event.items() if k not in {"timestamp", "level", "logger", "message"}}
            style = "red" if level == "ERROR" else "yellow" if level == "WARNING" else "dim" if level == "DEBUG" else "white"
            console.print(f"[{style}]{stamp} {level:<7}[/{style}] {message}")
            if extra:
                console.print(f"    [dim]{json.dumps(extra, ensure_ascii=False)[:500]}[/dim]")
        except Exception:
            console.print(line)
    console.print(f"\n[dim]Source: {path}[/dim]")


@config_app.command("show")
def config_show(
    project_root: Path = typer.Option(Path("."), "--project-root", "-p"),
    section: Optional[str] = typer.Option(None, "--section", help="Optional top-level section, e.g. llm"),
) -> None:
    """Print effective YAML configuration (API key values are loaded from the environment only)."""
    root = _project_root(project_root)
    try:
        cfg = _load(root)
    except ConfigurationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if section:
        if section not in cfg:
            console.print(f"[red]Unknown section: {section}[/red]")
            raise typer.Exit(2)
        cfg = {section: cfg[section]}
    console.print(cfg.__repr__() if False else yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Dotted YAML key, e.g. llm.agents.coder.temperature"),
    value: str = typer.Argument(..., help="YAML scalar or object value"),
    project_root: Path = typer.Option(Path("."), "--project-root", "-p"),
) -> None:
    """Set a YAML config value without editing agent code."""
    root = _project_root(project_root)
    config_path = root / ".harness" / "config.yaml"
    if not config_path.is_file():
        console.print("[red]Config missing. Run `forge init` first.[/red]")
        raise typer.Exit(1)
    original_text = config_path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(original_text) or {}
        parsed_value = yaml.safe_load(value)
        pieces = key.split(".")
        cursor = data
        for piece in pieces[:-1]:
            if piece not in cursor or not isinstance(cursor[piece], dict):
                cursor[piece] = {}
            cursor = cursor[piece]
        cursor[pieces[-1]] = parsed_value
        with config_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)
        _load(root)  # Validate after write; restore the old config on invalid edits.
    except Exception as exc:
        config_path.write_text(original_text, encoding="utf-8")
        console.print(f"[red]Config update failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[green]Updated[/green] {key} = {parsed_value!r}")


@config_app.command("validate")
def config_validate(project_root: Path = typer.Option(Path("."), "--project-root", "-p")) -> None:
    """Validate config structure and report missing provider environment variables."""
    root = _project_root(project_root)
    try:
        cfg = _load(root)
    except ConfigurationError as exc:
        console.print(f"[red]Invalid config:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print("[green]Configuration schema basics: valid[/green]")
    for name, provider in cfg.get("llm", {}).get("providers", {}).items():
        env_name = provider.get("api_key_env")
        status = "set" if env_name and os.environ.get(env_name) else "not set"
        console.print(f"{name:<14} {provider.get('base_url')} · key env {env_name}: [{ 'green' if status == 'set' else 'yellow' }]{status}[/]")
    for agent, profile in cfg.get("llm", {}).get("agents", {}).items():
        console.print(f"agent {agent:<12} → {profile.get('model', '(model missing)')}")


def repl(initial_root: Path) -> None:
    root = initial_root.expanduser().resolve()
    mode = "interactive"
    console.print(Panel(
        "[bold #b18aff]FORGE[/bold #b18aff] · Autonomous React Native engineering\n"
        "Streaming model output · task state · component registry · screenshots · JSONL diagnostics\n\n"
        "Type [cyan]help[/cyan] for commands, or [cyan]run[/cyan] to build from input/app_spec.md.",
        title="Interactive session", border_style="#a56eff", box=box.ROUNDED,
    ))
    while True:
        try:
            command_line = Prompt.ask(f"[bold #b18aff]forge[/bold #b18aff] [dim]{root.name} · {mode}[/dim]", console=console).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Leaving FORGE session.[/dim]")
            return
        if not command_line:
            continue
        parts = command_line.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1] if len(parts) > 1 else ""
        if command in {"exit", "quit", "/exit", "/quit"}:
            return
        if command in {"help", "/help", "?"}:
            console.print("""[bold]Commands[/bold]
  run [prompt or spec-file]  Plan and execute the app-building workflow
  resume <run_id>            Resume a saved run
  mode <autonomous|approval|interactive>
  root <project-path>        Switch target project
  inspect                    Inspect registry and run history
  logs [run_id]              Show recent structured logs
  config show                Print current config
  config set <dotted-key> <value>
  help                       Show this help
  exit                       Quit the REPL""")
        elif command == "mode":
            if argument in {"autonomous", "approval", "interactive"}:
                mode = argument
                console.print(f"Execution mode: [cyan]{mode}[/cyan]")
            else:
                console.print("Mode must be autonomous, approval, or interactive")
        elif command == "root":
            candidate = Path(argument).expanduser()
            root = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
            console.print(f"Target project: [cyan]{root}[/cyan]")
        elif command == "run":
            try:
                cfg = _load(root)
                content = None
                if argument:
                    candidate = Path(argument).expanduser()
                    if not candidate.is_absolute():
                        candidate = root / candidate
                    content = candidate.read_text(encoding="utf-8") if candidate.is_file() else argument
                result = HarnessRunner(root, cfg, mode=mode, console=console).execute(spec=content)
                _print_result(result)
            except Exception as exc:
                console.print(f"[red]Run could not start:[/red] {exc}")
        elif command == "resume":
            try:
                cfg = _load(root)
                result = HarnessRunner(root, cfg, mode=mode, console=console).execute(resume_id=argument.strip())
                _print_result(result)
            except Exception as exc:
                console.print(f"[red]Resume failed:[/red] {exc}")
        elif command == "inspect":
            ctx = typer.Context(inspect_command)
            inspect_command(project_root=root)
        elif command == "logs":
            logs_command(project_root=root, run_id=argument.strip() or None, tail=80, raw=False)
        elif command == "config show":
            config_show(project_root=root, section=None)
        elif command.startswith("config set "):
            rest = command_line[len("config set "):].split(maxsplit=1)
            if len(rest) != 2:
                console.print("Usage: config set <dotted-key> <value>")
            else:
                config_set(key=rest[0], value=rest[1], project_root=root)
        else:
            console.print("Unknown command. Type [cyan]help[/cyan].")


if __name__ == "__main__":
    app()
