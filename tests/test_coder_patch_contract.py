from pathlib import Path

import pytest

from forge_harness.agents.base import AgentOutputError
from forge_harness.agents.coder_agent import CoderAgent
from forge_harness.tools.file_ops import FileOps


def _validator() -> CoderAgent:
    # This unit exercises the deterministic validator without invoking an LLM.
    return CoderAgent.__new__(CoderAgent)


def test_patch_must_include_each_approved_operation(tmp_path: Path):
    target = tmp_path / "src" / "App.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("const title = 'Home';\nconst sub = 'Welcome';\n", encoding="utf-8")
    plan = {
        "operations": [
            {"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home'"},
            {"type": "replace_text", "path": "src/App.tsx", "old_text": "'Welcome'"},
        ]
    }
    patch = [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home'", "new_text": "'Dashboard'"}]
    with pytest.raises(AgentOutputError, match="omitted approved operation"):
        _validator()._validate_patch_against_plan(patch, plan, FileOps(tmp_path))


def test_patch_cannot_add_unplanned_operation(tmp_path: Path):
    target = tmp_path / "src" / "App.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("const title = 'Home';\n", encoding="utf-8")
    plan = {"operations": [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home'"}]}
    patch = [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Other'", "new_text": "'Dashboard'"}]
    with pytest.raises(AgentOutputError, match="not present"):
        _validator()._validate_patch_against_plan(patch, plan, FileOps(tmp_path))


def test_anchor_matches_with_trailing_whitespace_drift(tmp_path: Path):
    """LLM plan and patch calls may emit different trailing whitespace in old_text."""
    target = tmp_path / "src" / "App.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("const title = 'Home';\nconst sub = 'Welcome';\n", encoding="utf-8")
    # Plan anchor exists exactly once in-file; patch anchor differs by trailing whitespace.
    plan = {"operations": [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home';"}]}
    patch = [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home';  ", "new_text": "'Dashboard'"}]
    # Should NOT raise: anchors match after whitespace normalization, and plan anchor is unique in-file.
    _validator()._validate_patch_against_plan(patch, plan, FileOps(tmp_path))


def test_anchor_does_not_match_different_content(tmp_path: Path):
    target = tmp_path / "src" / "App.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("const title = 'Home';\n", encoding="utf-8")
    plan = {"operations": [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Home'"}]}
    patch = [{"type": "replace_text", "path": "src/App.tsx", "old_text": "'Goodbye'", "new_text": "'Dashboard'"}]
    with pytest.raises(AgentOutputError, match="not present"):
        _validator()._validate_patch_against_plan(patch, plan, FileOps(tmp_path))


def test_anchors_match_normalizes_whitespace():
    validator = CoderAgent.__new__(CoderAgent)
    assert validator._anchors_match("const x = 1;\n", "const x = 1;  \n") is True
    assert validator._anchors_match("const x = 1;\r\n", "const x = 1;\n") is True
    assert validator._anchors_match("const x = 1;\n", "const y = 1;\n") is False
    assert validator._anchors_match(None, None) is True
    assert validator._anchors_match("abc", None) is False
