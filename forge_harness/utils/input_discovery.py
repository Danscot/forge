from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image
        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return None, None


def discover_inputs(project_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    project_cfg = config.get("project", {})
    input_dir = (project_root / str(project_cfg.get("input_dir", "input"))).resolve()
    mockup_dir = (project_root / str(project_cfg.get("mockups_dir", "input/mockups"))).resolve()
    assets_dir = (project_root / str(project_cfg.get("assets_dir", "input/assets"))).resolve()
    spec_path = input_dir / "app_spec.md"
    spec_text = spec_path.read_text(encoding="utf-8") if spec_path.exists() else ""
    mockups = []
    assets = []
    max_images = max(1, int(project_cfg.get("max_mockup_images", 8)))
    if mockup_dir.exists():
        for path in sorted(mockup_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                width, height = _dimensions(path)
                item = {
                    "path": str(path.relative_to(project_root)), "name": path.name,
                    "sha256": _sha256(path), "bytes": path.stat().st_size,
                    "width": width, "height": height,
                    "inferred_screen": path.stem.replace("-", " ").replace("_", " ").title(),
                }
                if len(mockups) < max_images:
                    mockups.append(item)
    if assets_dir.exists():
        for path in sorted(assets_dir.rglob("*")):
            if path.is_file():
                assets.append({"path": str(path.relative_to(project_root)), "name": path.name, "suffix": path.suffix.lower(), "bytes": path.stat().st_size})
    return {
        "spec_path": str(spec_path.relative_to(project_root)) if spec_path.exists() else None,
        "spec_text": spec_text,
        "mockups": mockups,
        "assets": assets[:500],
        "manifest": {"input_dir": str(input_dir), "mockups_dir": str(mockup_dir), "assets_dir": str(assets_dir)},
    }
