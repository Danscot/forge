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


def test_generate_patch_repairs_malformed_json_without_replanning(tmp_path: Path):
    from forge_harness.core.state import Task

    class FakeRuntime:
        def __init__(self):
            self.responses = [
                "The patch is below: {not valid JSON}",
                '{"summary":"Create theme","operations":[{"type":"create_file","path":"src/theme.ts","content":"export const colors = {};\\n"}],"registry_changes":[]}',
            ]
            self.calls = []

        def complete(self, agent, messages, **kwargs):
            self.calls.append({"agent": agent, "messages": messages, "kwargs": kwargs})
            return type("Result", (), {"content": self.responses.pop(0)})()

        def _emit(self, *args, **kwargs):
            return None

    runtime = FakeRuntime()
    config = {"project": {"harness_dir": ".harness"}, "llm": {"structured_repair_attempts": 1}}
    agent = CoderAgent(runtime, tmp_path, config)
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add tokens")
    plan = {"summary": "Create theme", "operations": [{"type": "create_file", "path": "src/theme.ts"}]}

    patch = agent.generate_patch(task, plan, None, {}, {}, FileOps(tmp_path))

    assert patch["operations"][0]["path"] == "src/theme.ts"
    assert len(runtime.calls) == 2, "The malformed response should be repaired locally, not replanned"
    assert runtime.calls[0]["kwargs"]["profile_override"]["thinking_enabled"] is False
    assert "Validation error" in runtime.calls[1]["messages"][-1]["content"]


def test_generate_patch_repairs_operation_that_is_not_in_approved_plan(tmp_path: Path):
    from forge_harness.core.state import Task

    class FakeRuntime:
        def __init__(self):
            self.responses = [
                '{"summary":"wrong path","operations":[{"type":"create_file","path":"src/not-approved.ts","content":"export {};"}]}',
                '{"summary":"approved path","operations":[{"type":"create_file","path":"src/theme.ts","content":"export {};"}]}',
            ]
            self.repair_prompt = ""

        def complete(self, agent, messages, **kwargs):
            if len(self.responses) == 1:
                self.repair_prompt = messages[-1]["content"]
            return type("Result", (), {"content": self.responses.pop(0)})()

        def _emit(self, *args, **kwargs):
            return None

    runtime = FakeRuntime()
    agent = CoderAgent(runtime, tmp_path, {"project": {"harness_dir": ".harness"}, "llm": {"structured_repair_attempts": 1}})
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add tokens")
    plan = {"summary": "Create theme", "operations": [{"type": "create_file", "path": "src/theme.ts"}]}

    patch = agent.generate_patch(task, plan, None, {}, {}, FileOps(tmp_path))

    assert patch["operations"][0]["path"] == "src/theme.ts"
    assert "not present" in runtime.repair_prompt


def test_patch_output_budget_scales_with_approved_operation_count(tmp_path: Path):
    from forge_harness.core.state import Task

    class FakeRuntime:
        def __init__(self, response):
            self.response = response
            self.calls = []

        def complete(self, agent, messages, **kwargs):
            self.calls.append({"agent": agent, "messages": messages, "kwargs": kwargs})
            return type("Result", (), {"content": self.response})()

        def _emit(self, *args, **kwargs):
            return None

    plan_ops = [
        {"type": "create_file", "path": "src/theme.ts"},
        {"type": "create_file", "path": "src/components/CourseCard.tsx"},
        {"type": "create_file", "path": "src/components/CategoryChip.tsx"},
    ]
    patch_ops = [
        {"type": "create_file", "path": op["path"], "content": "export {};\n"}
        for op in plan_ops
    ]
    runtime = FakeRuntime(__import__("json").dumps({"operations": patch_ops}))
    config = {
        "project": {"harness_dir": ".harness"},
        "llm": {
            "structured_repair_attempts": 0,
            "agents": {"coder": {"max_output_tokens": 12000}},
        },
    }
    agent = CoderAgent(runtime, tmp_path, config)
    task = Task(id="foundation", type="foundation", title="Create theme and components", description="Add three files")

    agent.generate_patch(task, {"operations": plan_ops}, None, {}, {}, FileOps(tmp_path))

    profile = runtime.calls[0]["kwargs"]["profile_override"]
    assert profile["thinking_enabled"] is False
    assert profile["max_output_tokens"] == 6550  # 1,000 base + 3 × 1,850 per new file


def test_plan_schema_error_is_repaired_without_restarting_task(tmp_path: Path):
    import json
    from forge_harness.core.state import Task

    class FakeRuntime:
        def __init__(self):
            self.responses = [
                {"summary": "empty plan", "risk": "low", "operations": [], "files_to_read": [], "validation_plan": [], "registry_changes": []},
                {"summary": "create theme", "risk": "low", "operations": [{"type": "create_file", "path": "src/theme.ts", "target": "theme", "reason": "Add theme", "content_outline": "Export typed colors"}], "files_to_read": [], "validation_plan": [], "registry_changes": []},
            ]
            self.calls = []
            self.events = []

        def complete(self, agent, messages, **kwargs):
            self.calls.append({"agent": agent, "messages": messages, "kwargs": kwargs})
            return type("Result", (), {"content": json.dumps(self.responses.pop(0))})()

        def _emit(self, kind, **payload):
            self.events.append((kind, payload))

    runtime = FakeRuntime()
    agent = CoderAgent(runtime, tmp_path, {
        "project": {"harness_dir": ".harness"},
        "llm": {"structured_repair_attempts": 1, "effort_policy": {"coder_plan": {"thinking_enabled": False, "max_output_tokens": 1200}}},
    })
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add design tokens")

    plan = agent.plan_changes(task, "Build a small app", None, {}, {}, FileOps(tmp_path))

    assert plan["operations"][0]["path"] == "src/theme.ts"
    assert len(runtime.calls) == 2, "A schema-invalid plan should be repaired locally before retrying the whole task"
    assert any(kind == "structured_output_repair" and payload.get("stage") == "planning_changes" for kind, payload in runtime.events)
    assert "non-empty operations array" in runtime.calls[1]["messages"][-1]["content"]


def test_generate_patch_skips_earlier_empty_operations_object(tmp_path: Path):
    """A provider response may contain an earlier decodable stub before its actual patch."""
    import json
    from forge_harness.core.state import Task

    valid_patch = {
        "summary": "Create theme",
        "operations": [{"type": "create_file", "path": "src/theme.ts", "content": "export const colors = {};\n"}],
        "registry_changes": [],
    }

    class FakeRuntime:
        def __init__(self):
            self.calls = 0

        def complete(self, agent, messages, **kwargs):
            self.calls += 1
            # The first candidate is valid JSON but not a patch. The second is.
            content = '{"summary":"intermediate stub","operations":[]}\nFinal patch:\n' + json.dumps(valid_patch)
            return type("Result", (), {"content": content})()

        def _emit(self, *args, **kwargs):
            return None

    agent = CoderAgent(FakeRuntime(), tmp_path, {
        "project": {"harness_dir": ".harness"},
        "llm": {"structured_repair_attempts": 1},
    })
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add tokens")
    plan = {"summary": "Create theme", "operations": [{"type": "create_file", "path": "src/theme.ts"}]}

    patch = agent.generate_patch(task, plan, None, {}, {}, FileOps(tmp_path))

    assert patch["operations"][0]["path"] == "src/theme.ts"
    assert agent.runtime.calls == 1, "A later valid object should be selected without another LLM call"


def test_generate_patch_unwraps_provider_patch_object(tmp_path: Path):
    """Accept a common wrapper without bypassing validation against the approved plan."""
    import json
    from forge_harness.core.state import Task

    class FakeRuntime:
        def complete(self, agent, messages, **kwargs):
            return type("Result", (), {"content": json.dumps({
                "summary": "wrapped patch",
                "patch": {"operations": [{"type": "create_file", "path": "src/theme.ts", "content": "export {};\n"}]},
            })})()

        def _emit(self, *args, **kwargs):
            return None

    agent = CoderAgent(FakeRuntime(), tmp_path, {
        "project": {"harness_dir": ".harness"},
        "llm": {"structured_repair_attempts": 0},
    })
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add tokens")
    plan = {"summary": "Create theme", "operations": [{"type": "create_file", "path": "src/theme.ts"}]}

    patch = agent.generate_patch(task, plan, None, {}, {}, FileOps(tmp_path))

    assert patch["operations"][0]["path"] == "src/theme.ts"


def test_plan_changes_selects_later_nonempty_plan_object(tmp_path: Path):
    """Do not spend a structured-repair call when a valid plan follows an empty stub."""
    from forge_harness.core.state import Task

    class FakeRuntime:
        def __init__(self):
            self.calls = 0

        def complete(self, agent, messages, **kwargs):
            self.calls += 1
            return type("Result", (), {"content": (
                '{"summary":"intermediate stub","risk":"low","operations":[]}\n'
                '{"summary":"actual plan","risk":"low","operations":[{"type":"create_file",'
                '"path":"src/theme.ts","target":"theme","reason":"Add tokens",'
                '"content_outline":"Export colors"}],"files_to_read":[],"validation_plan":[],"registry_changes":[]}'
            )})()

        def _emit(self, *args, **kwargs):
            return None

    runtime = FakeRuntime()
    agent = CoderAgent(runtime, tmp_path, {
        "project": {"harness_dir": ".harness"},
        "llm": {"structured_repair_attempts": 1},
    })
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add tokens")

    plan = agent.plan_changes(task, "Build a small app", None, {}, {}, FileOps(tmp_path))

    assert plan["summary"] == "actual plan"
    assert plan["operations"][0]["path"] == "src/theme.ts"
    assert runtime.calls == 1


def test_generate_patch_tries_later_candidate_that_matches_approved_plan(tmp_path: Path):
    """A non-empty but unapproved JSON object must not hide a later valid patch."""
    import json
    from forge_harness.core.state import Task

    valid_patch = {
        "summary": "approved patch",
        "operations": [{"type": "create_file", "path": "src/theme.ts", "content": "export {};\n"}],
    }
    response = (
        json.dumps({"summary": "intermediate wrong path", "operations": [
            {"type": "create_file", "path": "src/not-approved.ts", "content": "export {};"}
        ]})
        + "\nFinal patch:\n"
        + json.dumps(valid_patch)
    )

    class FakeRuntime:
        calls = 0

        def complete(self, agent, messages, **kwargs):
            self.calls += 1
            return type("Result", (), {"content": response})()

        def _emit(self, *args, **kwargs):
            return None

    runtime = FakeRuntime()
    agent = CoderAgent(runtime, tmp_path, {
        "project": {"harness_dir": ".harness"},
        "llm": {"structured_repair_attempts": 1},
    })
    task = Task(id="foundation", type="foundation", title="Create theme", description="Add tokens")
    plan = {"summary": "Create theme", "operations": [{"type": "create_file", "path": "src/theme.ts"}]}

    patch = agent.generate_patch(task, plan, None, {}, {}, FileOps(tmp_path))

    assert patch["summary"] == "approved patch"
    assert patch["operations"][0]["path"] == "src/theme.ts"
    assert runtime.calls == 1
