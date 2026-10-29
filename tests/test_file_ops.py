from pathlib import Path

import pytest

from forge_harness.tools.file_ops import FileOperationError, FileOps


def test_str_replace_requires_unique_anchor(tmp_path: Path):
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "App.tsx"
    target.write_text("const label = 'Home';\nconst x = 'Home';\n", encoding="utf-8")
    ops = FileOps(tmp_path)
    with pytest.raises(FileOperationError, match="exactly once"):
        ops.str_replace("src/App.tsx", "Home", "Dashboard")
    assert "Home" in target.read_text(encoding="utf-8")


def test_create_file_and_replace_are_transactional(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text("const label = 'Home';\n", encoding="utf-8")
    ops = FileOps(tmp_path)
    with pytest.raises(FileOperationError, match="found 0 matches"):
        ops.apply_operations([
            {"type": "create_file", "path": "src/New.tsx", "content": "export const New = 1;"},
            {"type": "replace_text", "path": "src/App.tsx", "old_text": "not here", "new_text": "x"},
        ])
    assert not (tmp_path / "src" / "New.tsx").exists()
    assert "Home" in (tmp_path / "src" / "App.tsx").read_text(encoding="utf-8")


def test_transactional_patch_success(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text("export default 'Home';\n", encoding="utf-8")
    ops = FileOps(tmp_path)
    results = ops.apply_operations([
        {"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home'", "new_text": "'Dashboard'"},
        {"type": "create_file", "path": "src/New.tsx", "content": "export const New = 1;"},
    ])
    assert len(results) == 2
    assert "Dashboard" in (tmp_path / "src" / "App.tsx").read_text(encoding="utf-8")
    assert (tmp_path / "src" / "New.tsx").exists()


def test_project_root_escape_is_denied(tmp_path: Path):
    ops = FileOps(tmp_path)
    with pytest.raises(FileOperationError, match="escapes"):
        ops.resolve("../outside.txt")


def test_sensitive_file_cannot_be_read(tmp_path: Path):
    (tmp_path / ".env").write_text("SECRET=yes", encoding="utf-8")
    ops = FileOps(tmp_path)
    with pytest.raises(FileOperationError, match="Sensitive"):
        ops.cat_file(".env")


def test_coder_patch_must_match_plan_one_to_one(tmp_path: Path):
    from forge_harness.agents.base import AgentOutputError
    from forge_harness.agents.coder_agent import CoderAgent

    target = tmp_path / "src" / "App.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("const title = 'Home';\nconst sub = 'Welcome';\n", encoding="utf-8")
    plan = {"operations": [
        {"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home'"},
        {"type": "replace_text", "path": "src/App.tsx", "old_text": "'Welcome'"},
    ]}
    partial_patch = [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home'", "new_text": "'Dashboard'"}]
    validator = CoderAgent.__new__(CoderAgent)
    with pytest.raises(AgentOutputError, match="omitted approved operation"):
        validator._validate_patch_against_plan(partial_patch, plan, FileOps(tmp_path))


def test_snapshot_restore_removes_unvalidated_created_files(tmp_path: Path):
    import base64
    from forge_harness.core.runner import HarnessRunner

    (tmp_path / "src").mkdir()
    created = tmp_path / "src" / "New.tsx"
    created.write_text("partial", encoding="utf-8")
    existing = tmp_path / "src" / "App.tsx"
    existing.write_text("partially modified", encoding="utf-8")
    runner = HarnessRunner(tmp_path, {"project": {"harness_dir": ".harness"}})

    class _Dashboard:
        def activity_message(self, *_args, **_kwargs):
            return None

    runner.dashboard = _Dashboard()
    runner._restore_snapshot({"paths": {
        "src/New.tsx": {"existed": False},
        "src/App.tsx": {"existed": True, "content_b64": base64.b64encode(b"original").decode("ascii")},
    }})
    assert not created.exists()
    assert existing.read_text(encoding="utf-8") == "original"


def test_text_patch_rejects_fabricated_png_assets(tmp_path: Path):
    ops = FileOps(tmp_path)
    with pytest.raises(FileOperationError, match="cannot create or overwrite binary asset"):
        ops.apply_operations([
            {"type": "create_file", "path": "assets/images/logo.png", "content": "not real png bytes"}
        ])
    assert not (tmp_path / "assets/images/logo.png").exists()


def test_text_patch_rejects_overwriting_existing_binary_assets(tmp_path: Path):
    target = tmp_path / "assets" / "icon.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\noriginal")
    ops = FileOps(tmp_path)
    with pytest.raises(FileOperationError, match="cannot create or overwrite binary asset"):
        ops.apply_operations([
            {"type": "overwrite_file", "path": "assets/icon.png", "content": "replacement text"}
        ])
    assert target.read_bytes().startswith(b"\x89PNG")
