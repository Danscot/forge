from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent
CONFIG_TEMPLATE = PACKAGE_ROOT / "config.example.yaml"
PROMPT_NAMES = ("planner.md", "vision.md", "coder_plan.md", "coder_patch.md", "critic.md", "diagnostic.md")


class ConfigurationError(RuntimeError):
    pass


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Could not read configuration {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigurationError(f"Configuration root must be a YAML object: {path}")
    return loaded


def load_config(project_root: str | Path = ".", config_path: str | Path | None = None) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    path = Path(config_path).expanduser() if config_path else root / ".harness" / "config.yaml"
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        raise ConfigurationError(
            f"No FORGE config found at {path}. Run `forge init --project-root {root}` first."
        )
    cfg = read_yaml(path)
    cfg.setdefault("project", {})["resolved_root"] = str(root)
    cfg.setdefault("project", {}).setdefault("harness_dir", ".harness")
    cfg.setdefault("llm", {})
    cfg["project"]["input_dir"] = str(cfg["project"].get("input_dir", "input"))
    providers = cfg["llm"].get("providers", {})
    if not isinstance(providers, dict):
        raise ConfigurationError("llm.providers must be a mapping")
    for name, provider in providers.items():
        if not isinstance(provider, dict) or not provider.get("base_url"):
            raise ConfigurationError(f"llm.providers.{name} requires a base_url")
    agents = cfg["llm"].get("agents", {})
    if not isinstance(agents, dict):
        raise ConfigurationError("llm.agents must be a mapping")
    if not agents:
        raise ConfigurationError("At least one llm.agents profile must be configured")
    return cfg


def initialize_project(project_root: str | Path = ".", overwrite_config: bool = False) -> Path:
    root = Path(project_root).expanduser().resolve()
    if not root.exists():
        raise ConfigurationError(f"Project root does not exist: {root}")
    harness = root / ".harness"
    harness.mkdir(parents=True, exist_ok=True)
    config_file = harness / "config.yaml"
    if config_file.exists() and not overwrite_config:
        raise ConfigurationError(f"Config already exists: {config_file}; use --overwrite-config to replace it")
    shutil.copy2(CONFIG_TEMPLATE, config_file)
    input_dir = root / "input"
    (input_dir / "mockups").mkdir(parents=True, exist_ok=True)
    (input_dir / "assets").mkdir(parents=True, exist_ok=True)
    (harness / "runs").mkdir(parents=True, exist_ok=True)
    (harness / "logs").mkdir(parents=True, exist_ok=True)
    (harness / "prompts").mkdir(parents=True, exist_ok=True)
    for name in PROMPT_NAMES:
        source = PACKAGE_ROOT / "prompts" / name
        destination = harness / "prompts" / name
        if source.exists() and (overwrite_config or not destination.exists()):
            shutil.copy2(source, destination)
    registry = harness / "components.json"
    if not registry.exists():
        registry.write_text('{\n  "version": 1,\n  "components": {}\n}\n', encoding="utf-8")
    example_spec = input_dir / "app_spec.md"
    if not example_spec.exists():
        example_spec.write_text(
            "# Application specification\n\nDescribe the application, target users, screens, key interactions, and visual direction here.\n"
            "Place reference screenshots in input/mockups/ and optional images/icons in input/assets/.\n",
            encoding="utf-8",
        )
    env_example = root / ".env.example"
    if not env_example.exists():
        env_example.write_text(
            "# Copy to .env and fill in the keys for providers you use. Never commit .env.\n"
            "NVIDIA_API_KEY=\nGOOGLE_API_KEY=\nOPENROUTER_API_KEY=\nOPENAI_API_KEY=\nZ_API_KEY=\n",
            encoding="utf-8",
        )
    gitignore = root / ".gitignore"
    ignore_lines = [".env", ".env.*", "!.env.example", ".harness/state.db*", ".harness/runs/", ".harness/logs/"]
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    missing = [line for line in ignore_lines if line not in existing.splitlines()]
    if missing:
        with gitignore.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write("\n# FORGE local secrets and run artifacts\n" + "\n".join(missing) + "\n")
    return harness


def resolve_project_path(cfg: dict[str, Any], relative: str | Path) -> Path:
    root = Path(cfg["project"]["resolved_root"]).resolve()
    path = Path(relative)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def get_agent_profile(cfg: dict[str, Any], agent: str) -> dict[str, Any]:
    profiles = cfg.get("llm", {}).get("agents", {})
    profile = profiles.get(agent)
    if not profile:
        raise ConfigurationError(f"No llm.agents.{agent} profile is configured")
    return dict(profile)


def env_value(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value
