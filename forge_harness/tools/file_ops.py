from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


class FileOperationError(RuntimeError):
    pass


class FileOps:
    """Project-root-confined reads and transactional, narrowly scoped file changes."""

    def __init__(self, project_root: Path, config: dict[str, Any] | None = None):
        self.root = project_root.expanduser().resolve()
        self.config = config or {}
        security = self.config.get("security", {})
        self.excluded_names = set(security.get("excluded_context_names", [".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"]))
        self.max_file_bytes = int(security.get("max_file_bytes", 2_000_000))
        self.protected_parts = {".git", "node_modules"}

    def resolve(self, file_path: str | Path, *, for_write: bool = False) -> Path:
        input_path = Path(file_path)
        path = input_path.resolve() if input_path.is_absolute() else (self.root / input_path).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise FileOperationError(f"Path escapes the configured project root: {file_path}") from exc
        relative_parts = path.relative_to(self.root).parts
        if any(part in self.protected_parts for part in relative_parts):
            raise FileOperationError(f"Access to protected path is denied: {file_path}")
        if path.name in self.excluded_names or path.name.startswith(".env.") or path.name.endswith(".pem"):
            raise FileOperationError(f"Sensitive file access is denied: {file_path}")
        if for_write:
            if ".harness" in relative_parts:
                raise FileOperationError(f"Writing harness-managed configuration/state is denied: {file_path}")
            if path.name in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}:
                raise FileOperationError(f"Package lockfiles are not direct patch targets: {file_path}")
            if path.name == "package.json" and not self.config.get("execution", {}).get("allow_dependency_install", False):
                raise FileOperationError("package.json changes are disabled. Set execution.allow_dependency_install=true to permit manifest changes and npm install.")
        return path

    def cat_file(self, file_path: str | Path, line_start: int = 1, line_end: int = 500) -> str:
        path = self.resolve(file_path)
        if not path.is_file():
            raise FileOperationError(f"File not found: {file_path}")
        if path.stat().st_size > self.max_file_bytes:
            raise FileOperationError(f"File exceeds configured context limit ({self.max_file_bytes} bytes): {file_path}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(line_start))
        end = min(max(start, int(line_end)), len(lines))
        width = len(str(max(end, 1)))
        return "\n".join(f"{idx:>{width}} | {lines[idx - 1]}" for idx in range(start, end + 1))

    def write_file(self, file_path: str | Path, content: str) -> None:
        """Create a new file only; existing files must be modified by surgical patches."""
        path = self.resolve(file_path, for_write=True)
        if path.exists():
            raise FileOperationError(f"write_file only creates brand-new files; target already exists: {file_path}")
        if len(content.encode("utf-8")) > self.max_file_bytes:
            raise FileOperationError(f"Proposed file exceeds configured size limit: {file_path}")
        self._atomic_write(path, content)

    def str_replace(self, file_path: str | Path, old_str: str, new_str: str) -> dict[str, Any]:
        """Replace one and only one exact anchor in an existing file."""
        path = self.resolve(file_path, for_write=True)
        if not path.is_file():
            raise FileOperationError(f"Patch target does not exist: {file_path}")
        if not old_str:
            raise FileOperationError("old_str cannot be empty")
        original = path.read_text(encoding="utf-8")
        occurrences = original.count(old_str)
        if occurrences != 1:
            raise FileOperationError(f"Expected old_str exactly once in {file_path}; found {occurrences} matches")
        changed = original.replace(old_str, new_str, 1)
        if len(changed.encode("utf-8")) > self.max_file_bytes:
            raise FileOperationError(f"Patched file exceeds configured size limit: {file_path}")
        self._atomic_write(path, changed)
        return {"path": str(path.relative_to(self.root)), "replacements": 1, "bytes_before": len(original.encode()), "bytes_after": len(changed.encode())}

    def grep_code(self, query: str, directory: str | Path = ".", *, max_results: int = 200) -> list[dict[str, Any]]:
        root = self.resolve(directory)
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = (p for p in root.rglob("*") if p.is_file() and not any(part in self.protected_parts for part in p.parts))
        else:
            return []
        matches: list[dict[str, Any]] = []
        pattern = re.compile(query, re.IGNORECASE)
        for path in candidates:
            try:
                path.relative_to(self.root)
            except ValueError:
                continue
            if path.name in self.excluded_names or path.name.startswith(".env.") or path.suffix.lower() not in {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml", ".md", ".css", ".html"}:
                continue
            try:
                if path.stat().st_size > self.max_file_bytes:
                    continue
                for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if pattern.search(line):
                        matches.append({"path": str(path.relative_to(self.root)), "line": number, "text": line.strip()[:500]})
                        if len(matches) >= max_results:
                            return matches
            except (OSError, UnicodeError, re.error):
                continue
        return matches

    def apply_operations(self, operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Validate all patch operations in memory, then atomically write the complete set.

        Supported operations are create_file, replace_text and overwrite_file (explicit whole-file
        rewrite of a file the approved plan names). If validation fails, no changes are written.
        """
        if not isinstance(operations, list) or not operations:
            raise FileOperationError("Patch plan contains no operations")
        original_by_path: dict[Path, str | None] = {}
        working: dict[Path, str] = {}
        reports: list[dict[str, Any]] = []
        for index, operation in enumerate(operations):
            op_type = operation.get("type")
            raw_path = operation.get("path")
            if not raw_path:
                raise FileOperationError(f"Operation {index + 1} has no path")
            path = self.resolve(raw_path, for_write=True)
            if path not in original_by_path:
                if path.exists():
                    if not path.is_file():
                        raise FileOperationError(f"Patch target is not a file: {raw_path}")
                    if path.stat().st_size > self.max_file_bytes:
                        raise FileOperationError(f"Target exceeds size limit: {raw_path}")
                    original_by_path[path] = path.read_text(encoding="utf-8")
                    working[path] = original_by_path[path] or ""
                else:
                    original_by_path[path] = None
            if op_type == "create_file":
                if original_by_path[path] is not None or working.get(path, ""):
                    raise FileOperationError(f"create_file target already exists or is already planned: {raw_path}")
                content = operation.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise FileOperationError(f"create_file requires non-empty content: {raw_path}")
                working[path] = content
                reports.append({"type": op_type, "path": str(path.relative_to(self.root)), "bytes": len(content.encode())})
            elif op_type == "overwrite_file":
                content = operation.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise FileOperationError(f"overwrite_file requires non-empty content: {raw_path}")
                working[path] = content
                reports.append({"type": op_type, "path": str(path.relative_to(self.root)), "bytes": len(content.encode())})
            elif op_type == "replace_text":
                if original_by_path[path] is None:
                    raise FileOperationError(f"replace_text target does not exist: {raw_path}")
                old_text = operation.get("old_text")
                new_text = operation.get("new_text")
                if not isinstance(old_text, str) or not old_text:
                    raise FileOperationError(f"replace_text requires a non-empty old_text anchor: {raw_path}")
                if not isinstance(new_text, str):
                    raise FileOperationError(f"replace_text requires new_text: {raw_path}")
                occurrences = working[path].count(old_text)
                if occurrences != 1:
                    raise FileOperationError(f"Expected anchor exactly once in {raw_path}; found {occurrences} matches")
                working[path] = working[path].replace(old_text, new_text, 1)
                reports.append({"type": op_type, "path": str(path.relative_to(self.root)), "anchor_occurrences": 1})
            else:
                raise FileOperationError(f"Unsupported patch operation type: {op_type!r}")
        for path, content in working.items():
            if len(content.encode("utf-8")) > self.max_file_bytes:
                raise FileOperationError(f"Resulting file exceeds size limit: {path.relative_to(self.root)}")
        written: list[Path] = []
        try:
            for path, content in working.items():
                if original_by_path[path] == content:
                    continue
                self._atomic_write(path, content)
                written.append(path)
        except Exception:
            for path in reversed(written):
                original = original_by_path[path]
                try:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        self._atomic_write(path, original)
                except Exception:
                    pass
            raise
        return reports

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode if path.exists() else None
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if mode is not None:
                os.chmod(temporary, mode)
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
