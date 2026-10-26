from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class ComponentRegistry:
    def __init__(self, path: Path, project_root: Path):
        self.path = path
        self.project_root = project_root
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"version": 1, "components": {}})
        self.data = self._read()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Registry root must be an object")
            data.setdefault("version", 1)
            data.setdefault("components", {})
            return data
        except Exception as exc:
            backup = self.path.with_suffix(".corrupt.json")
            try:
                backup.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                pass
            data = {"version": 1, "components": {}, "recovery_note": f"Registry could not be parsed: {exc}"}
            self._write(data)
            return data

    def _write(self, data: dict[str, Any]) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.data))

    def refresh(self, updates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        components = self.data.setdefault("components", {})
        for update in updates or []:
            name = str(update.get("name", "")).strip()
            path = str(update.get("path", "")).strip()
            if not name or not path:
                continue
            full_path = (self.project_root / path).resolve()
            try:
                full_path.relative_to(self.project_root.resolve())
            except ValueError:
                continue
            if not full_path.is_file():
                continue
            existing = components.get(name, {})
            components[name] = {
                "name": name,
                "path": path,
                "purpose": str(update.get("purpose") or existing.get("purpose") or "Custom reusable component"),
                "props": update.get("props") if isinstance(update.get("props"), dict) else existing.get("props", {}),
                "exported": True,
                "updated_from": "agent_update",
            }
        # Index conventional shared-component folders, without adding screens/helpers.
        component_roots = [self.project_root / "src" / "components", self.project_root / "components"]
        export_patterns = [
            re.compile(r"export\s+(?:default\s+)?function\s+([A-Z][A-Za-z0-9_]*)"),
            re.compile(r"export\s+(?:const|class)\s+([A-Z][A-Za-z0-9_]*)"),
        ]
        for root in component_roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix not in {".tsx", ".ts", ".jsx", ".js"} or "node_modules" in path.parts:
                    continue
                try:
                    relative = str(path.relative_to(self.project_root.resolve()))
                    text = path.read_text(encoding="utf-8", errors="replace")[:300000]
                except Exception:
                    continue
                names = []
                for pattern in export_patterns:
                    names.extend(pattern.findall(text))
                for name in set(names):
                    current = components.get(name, {})
                    components[name] = {
                        "name": name,
                        "path": relative,
                        "purpose": current.get("purpose", "Shared component (indexed from source)"),
                        "props": current.get("props", {}),
                        "exported": True,
                        "updated_from": current.get("updated_from", "source_scan"),
                    }
        self.data["version"] = 1
        self.data["components"] = dict(sorted(components.items()))
        self._write(self.data)
        return self.snapshot()

    def find(self, query: str) -> list[dict[str, Any]]:
        terms = set(query.lower().split())
        ranked = []
        for name, item in self.data.get("components", {}).items():
            haystack = " ".join([name, str(item.get("purpose", "")), str(item.get("path", "")), json.dumps(item.get("props", {}))]).lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda item: (-item[0], item[1].get("name", "")))
        return [item for _, item in ranked]
