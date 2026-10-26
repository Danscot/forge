from __future__ import annotations

import json
import logging
import logging.handlers
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_LOG_CONTEXT = threading.local()


def set_log_context(run_id: str | None = None, task_id: str | None = None) -> None:
    if run_id is not None:
        _LOG_CONTEXT.run_id = run_id
    if task_id is not None or hasattr(_LOG_CONTEXT, "task_id"):
        _LOG_CONTEXT.task_id = task_id


class RunContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = getattr(_LOG_CONTEXT, "run_id", None)
        if not hasattr(record, "task_id"):
            record.task_id = getattr(_LOG_CONTEXT, "task_id", None)
        return True


class JsonLineFormatter(logging.Formatter):
    STANDARD = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
        "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
        "relativeCreated", "thread", "threadName", "processName", "process", "message",
    }

    def __init__(self, redactions: list[str] | None = None):
        super().__init__()
        self.patterns = [re.compile(p) for p in (redactions or [])]

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for pattern in self.patterns:
                value = pattern.sub("[REDACTED]", value)
            value = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,]+", r"\1[REDACTED]", value)
            return value
        if isinstance(value, dict):
            return {k: ("[REDACTED]" if str(k).lower() in {"api_key", "authorization", "token", "secret"} else self._redact(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        return value

    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self.STANDARD and not key.startswith("_"):
                event[key] = value
        if record.exc_info:
            event["exception"] = self.formatException(record.exc_info)
        return json.dumps(self._redact(event), ensure_ascii=False, default=str)


def setup_logging(project_root: Path, run_id: str | None, cfg: dict[str, Any]) -> Path:
    harness_dir = project_root / str(cfg.get("project", {}).get("harness_dir", ".harness"))
    log_dir = harness_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if run_id:
        run_log_dir = harness_dir / "runs" / run_id / "logs"
        run_log_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_log_dir = log_dir
    settings = cfg.get("logging", {})
    level_name = str(settings.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    console_level_name = str(settings.get("console_level", "WARNING")).upper()
    console_level = getattr(logging, console_level_name, logging.WARNING)
    redactions = settings.get("redact_patterns", cfg.get("security", {}).get("redact_patterns", []))
    max_bytes = int(settings.get("max_event_file_mb", 30)) * 1024 * 1024
    backups = int(settings.get("backups", 5))

    logger = logging.getLogger()
    logger.setLevel(level)
    # Replace only handlers installed by FORGE; don't duplicate output when resuming.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    file_handler = logging.handlers.RotatingFileHandler(
        run_log_dir / "events.jsonl", maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(JsonLineFormatter(redactions))
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s · %(message)s", "%H:%M:%S"))
    context_filter = RunContextFilter()
    file_handler.addFilter(context_filter)
    console.addFilter(context_filter)
    logger.addHandler(file_handler)
    logger.addHandler(console)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return run_log_dir / "events.jsonl"


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    logger.log(level, message, extra=fields)
